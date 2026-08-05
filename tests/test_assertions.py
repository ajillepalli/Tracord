import json
import os
from dataclasses import fields, replace
from pathlib import Path

import pytest

import tracord.assertions as assertions_module
from tracord.assertions import (
    AssertionFailure,
    AssertionRunError,
    ExpectationValidationError,
    MAX_ARTIFACT_BYTES,
    MAX_NEEDLE_BYTES,
    MAX_TOTAL_ARTIFACT_BYTES,
    MAX_TRACE_BYTES,
    READ_CHUNK_BYTES,
    TAIL_BYTES,
    TraceExpectations,
    evaluate_run,
    validate_expectations,
)
from tracord.paths import (
    IdentityComparison,
    OpenedFile,
    SafePathError,
    compare_identity,
    compare_snapshot,
)


RUN_ID = "run-1"


def valid_trace(tmp_path: Path, *, run_id: str = RUN_ID) -> dict[str, object]:
    return {
        "schema_version": "tracord.trace.v0",
        "run_id": run_id,
        "kind": "command",
        "name": None,
        "status": "passed",
        "command": ["python", "--version"],
        "cwd": str(tmp_path),
        "pid": 123,
        "started_at": "2026-08-05T00:00:00.000Z",
        "finished_at": "2026-08-05T00:00:00.100Z",
        "duration_ms": 100,
        "timeout_seconds": None,
        "exit_code": 0,
        "timed_out": False,
        "redacted": True,
        "decode_replacement": {"stdout": "none", "stderr": "none"},
        "store_identity_verified": True,
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
        "events": [
            {
                "type": "command.finished",
                "at": "2026-08-05T00:00:00.100Z",
                "data": {},
            }
        ],
    }


def _write_run(
    root: Path,
    *,
    run_id: str = RUN_ID,
    stdout: bytes = b"hello from tracord\n",
    stderr: bytes = b"",
) -> tuple[dict[str, object], Path]:
    trace_dir = root / "runs" / run_id
    trace_dir.mkdir(parents=True)
    trace = valid_trace(root, run_id=run_id)
    (trace_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    (trace_dir / "stdout.log").write_bytes(stdout)
    (trace_dir / "stderr.log").write_bytes(stderr)
    return trace, trace_dir


def _expect(**overrides: object) -> TraceExpectations:
    values: dict[str, object] = {"status": "passed"}
    values.update(overrides)
    return TraceExpectations(**values)


def _failures(
    root: Path,
    expectations: TraceExpectations,
    *,
    run_id: str = RUN_ID,
) -> list[AssertionFailure]:
    on_disk_run_id, failures = evaluate_run(root, run_id, expectations)
    assert on_disk_run_id == run_id
    return failures


def _run_error(
    root: Path,
    expectations: TraceExpectations,
    *,
    run_id: str = RUN_ID,
) -> str:
    with pytest.raises(AssertionRunError) as exc_info:
        evaluate_run(root, run_id, expectations)
    return exc_info.value.code


def test_scanner_constants_are_frozen():
    assert MAX_TRACE_BYTES == 16 * 1024 * 1024
    assert MAX_ARTIFACT_BYTES == 10 * 1024 * 1024
    assert MAX_TOTAL_ARTIFACT_BYTES == 16 * 1024 * 1024
    assert READ_CHUNK_BYTES == 1024 * 1024
    assert MAX_NEEDLE_BYTES == 65_536
    assert TAIL_BYTES == 65_535


@pytest.mark.parametrize(
    ("replacement", "timed_out", "expected_code"),
    [
        (None, True, "artifact_decode_unknown"),
        ("present", False, "artifact_decode_replaced"),
    ],
)
def test_content_assertions_are_indeterminate_when_decode_provenance_is_lossy_or_unknown(
    tmp_path: Path,
    replacement: str | None,
    timed_out: bool,
    expected_code: str,
):
    root = tmp_path / ".tracord"
    trace, trace_dir = _write_run(root, stdout=b"needle\n")
    if replacement is None:
        trace.pop("decode_replacement")
        trace["status"] = "timeout"
        trace["exit_code"] = None
        trace["timed_out"] = timed_out
    else:
        trace["decode_replacement"] = {"stdout": replacement, "stderr": "none"}
    (trace_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")

    assert _failures(root, TraceExpectations(stdout_contains="needle")) == [
        AssertionFailure(expected_code, "stdout_contains")
    ]


def test_legacy_completed_content_assertion_uses_strict_decode_semantics(
    tmp_path: Path,
):
    root = tmp_path / ".tracord"
    trace, trace_dir = _write_run(root, stdout=b"needle\n")
    trace.pop("decode_replacement")
    (trace_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")

    assert _failures(root, TraceExpectations(stdout_contains="needle")) == []


def test_snapshot_comparison_is_tri_state_and_nonidentity_first(tmp_path: Path):
    path = tmp_path / "snapshot.log"
    path.write_text("content", encoding="utf-8")
    original = path.stat()

    class StatOverride:
        def __init__(self, info: os.stat_result, **values: int):
            self._info = info
            self._values = values

        def __getattr__(self, name: str):
            return self._values.get(name, getattr(self._info, name))

    unknown = StatOverride(original, st_ino=0)
    changed_size = StatOverride(original, st_ino=0, st_size=original.st_size + 1)
    changed_links = StatOverride(original, st_nlink=original.st_nlink + 1)
    changed_ctime = StatOverride(original, st_ctime_ns=original.st_ctime_ns + 1)

    assert compare_identity(original, unknown) is IdentityComparison.UNAVAILABLE
    assert compare_snapshot(original, unknown) is IdentityComparison.UNAVAILABLE
    assert compare_snapshot(original, changed_size) is IdentityComparison.DIFFERENT
    assert compare_snapshot(original, changed_links) is IdentityComparison.DIFFERENT
    assert compare_snapshot(original, changed_ctime) is IdentityComparison.DIFFERENT


def test_evaluate_run_checks_trace_and_artifact_content(tmp_path: Path):
    root = tmp_path / "store"
    _write_run(root)

    failures = _failures(
        root,
        TraceExpectations(
            status="passed",
            exit_code=0,
            stdout_contains="tracord",
            max_duration_ms=200,
            no_timeout=True,
        ),
    )

    assert failures == []


def test_evaluate_run_reports_closed_failures_without_expected_values(tmp_path: Path):
    root = tmp_path / "store"
    trace, trace_dir = _write_run(root, stdout=b"different text\n")
    trace["status"] = "failed"
    trace["exit_code"] = 1
    trace["duration_ms"] = 300
    trace["timed_out"] = True
    (trace_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    secret = "expected-sensitive-value"

    failures = _failures(
        root,
        TraceExpectations(
            status="passed",
            exit_code=0,
            stdout_contains=secret,
            max_duration_ms=100,
            no_timeout=True,
        ),
    )

    assert failures == [
        AssertionFailure("assertion_mismatch", "status"),
        AssertionFailure("assertion_mismatch", "exit_code"),
        AssertionFailure("assertion_mismatch", "stdout_contains"),
        AssertionFailure("assertion_mismatch", "max_duration_ms"),
        AssertionFailure("assertion_mismatch", "no_timeout"),
    ]
    assert secret not in repr(failures)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({}, "assertion_no_expectations"),
        ({"status": "unknown"}, "assertion_value_invalid"),
        ({"exit_code": True}, "assertion_value_invalid"),
        ({"max_duration_ms": -1}, "assertion_value_invalid"),
        ({"stdout_contains": ""}, "assertion_value_invalid"),
        ({"stderr_contains": "\ud800"}, "assertion_value_invalid"),
        ({"stdout_contains": "x" * (MAX_NEEDLE_BYTES + 1)}, "assertion_value_invalid"),
    ],
)
def test_expectations_are_validated_and_preencoded(kwargs: dict[str, object], code: str):
    with pytest.raises(ExpectationValidationError) as exc_info:
        validate_expectations(TraceExpectations(**kwargs))

    assert exc_info.value.code == code


def test_expectation_bytes_are_encoded_once():
    expectations = TraceExpectations(stdout_contains="snowman-\u2603")
    validated = validate_expectations(expectations)

    assert validated.stdout_needle == "snowman-\u2603".encode()


def test_public_expectation_shape_is_frozen_to_six_fields():
    assert [item.name for item in fields(TraceExpectations)] == [
        "status",
        "exit_code",
        "stdout_contains",
        "stderr_contains",
        "max_duration_ms",
        "no_timeout",
    ]


@pytest.mark.parametrize(
    ("run_id", "code"),
    [
        ("../outside", "invalid_run_id"),
        ("missing", "run_not_found"),
        ("RUN-1", "run_identity_mismatch"),
    ],
)
def test_run_lookup_is_portable_exact_and_closed(
    tmp_path: Path, run_id: str, code: str
):
    root = tmp_path / "store"
    _write_run(root)

    assert _run_error(root, _expect(), run_id=run_id) == code


def test_trace_run_id_must_match_exact_directory_entry(tmp_path: Path):
    root = tmp_path / "store"
    trace, trace_dir = _write_run(root)
    trace["run_id"] = "other-run"
    (trace_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")

    assert _run_error(root, _expect()) == "run_identity_mismatch"


def test_trace_duplicate_keys_are_invalid(tmp_path: Path):
    root = tmp_path / "store"
    trace, trace_dir = _write_run(root)
    encoded = json.dumps(trace).replace(
        '"run_id": "run-1"',
        '"run_id": "run-1", "run_id": "run-1"',
        1,
    )
    (trace_dir / "trace.json").write_text(encoded, encoding="utf-8")

    assert _run_error(root, _expect()) == "trace_invalid"


def test_exact_run_is_rejected_when_casefold_alias_also_exists(tmp_path: Path):
    root = tmp_path / "store"
    _write_run(root)
    alias = root / "runs" / "RUN-1"
    try:
        alias.mkdir()
    except FileExistsError:
        pytest.skip("filesystem does not permit case-distinct aliases")

    assert _run_error(root, _expect()) == "run_identity_mismatch"


def test_symlinked_store_root_is_rejected(tmp_path: Path):
    real_root = tmp_path / "real-store"
    _write_run(real_root)
    linked_root = tmp_path / "linked-store"
    try:
        os.symlink(real_root, linked_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    assert _run_error(linked_root, _expect()) == "run_identity_mismatch"


def test_trace_read_accepts_exact_limit_and_rejects_one_byte_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "store"
    _trace, trace_dir = _write_run(root)
    trace_size = (trace_dir / "trace.json").stat().st_size
    monkeypatch.setattr(assertions_module, "MAX_TRACE_BYTES", trace_size)

    assert _failures(root, _expect()) == []

    with (trace_dir / "trace.json").open("ab") as stream:
        stream.write(b" ")
    assert _run_error(root, _expect()) == "trace_unreadable"


def test_trace_hardlink_is_rejected(tmp_path: Path):
    root = tmp_path / "store"
    _trace, trace_dir = _write_run(root)
    try:
        os.link(trace_dir / "trace.json", tmp_path / "trace-alias.json")
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    assert _run_error(root, _expect()) == "trace_unreadable"


def test_artifact_hardlink_is_rejected(tmp_path: Path):
    root = tmp_path / "store"
    _trace, trace_dir = _write_run(root)
    try:
        os.link(trace_dir / "stdout.log", tmp_path / "stdout-alias.log")
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    assert _failures(root, TraceExpectations(stdout_contains="tracord")) == [
        AssertionFailure("artifact_unreadable", "stdout_contains")
    ]


def test_link_parent_is_rejected_without_following_it(tmp_path: Path):
    root = tmp_path / "store"
    trace, trace_dir = _write_run(root)
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.log").write_text("needle", encoding="utf-8")
    try:
        os.symlink(external, trace_dir / "nested", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    trace["artifacts"]["stdout"] = "nested/secret.log"
    (trace_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")

    assert _failures(root, TraceExpectations(stdout_contains="needle")) == [
        AssertionFailure("artifact_unreadable", "stdout_contains")
    ]


def test_match_spanning_read_chunks_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "store"
    _write_run(root, stdout=b"abcTARGETxyz")
    monkeypatch.setattr(assertions_module, "READ_CHUNK_BYTES", 5)

    assert _failures(root, TraceExpectations(stdout_contains="TARGET")) == []


def test_invalid_utf8_after_early_match_takes_precedence(tmp_path: Path):
    root = tmp_path / "store"
    _write_run(root, stdout=b"needle then invalid \xff")

    assert _failures(root, TraceExpectations(stdout_contains="needle")) == [
        AssertionFailure("artifact_invalid_utf8", "stdout_contains")
    ]


def test_early_match_does_not_hide_post_read_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "store"
    _write_run(root, stdout=b"needle" + b"x" * 100)
    real_verify = assertions_module.verify_opened_file

    def race_after_artifact(opened: OpenedFile):
        if opened.prepared.relative_path == "stdout.log":
            raise SafePathError("changed")
        return real_verify(opened)

    monkeypatch.setattr(assertions_module, "verify_opened_file", race_after_artifact)

    assert _failures(root, TraceExpectations(stdout_contains="needle")) == [
        AssertionFailure("artifact_changed", "stdout_contains")
    ]


def test_zero_inode_artifact_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "store"
    _write_run(root)
    real_prepare = assertions_module.prepare_regular_file

    class ZeroInode:
        def __init__(self, info: os.stat_result):
            self._info = info
            self.st_ino = 0

        def __getattr__(self, name: str):
            return getattr(self._info, name)

    def zero_stdout(root_path: Path, relative_path: str, **kwargs: object):
        prepared = real_prepare(root_path, relative_path, **kwargs)
        if relative_path == "stdout.log":
            return replace(prepared, initial=ZeroInode(prepared.initial))
        return prepared

    monkeypatch.setattr(assertions_module, "prepare_regular_file", zero_stdout)

    assert _failures(root, TraceExpectations(stdout_contains="tracord")) == [
        AssertionFailure("artifact_unreadable", "stdout_contains")
    ]


def test_repeated_short_reads_are_consumed_to_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "store"
    _write_run(root, stdout=b"prefix-needle-suffix")
    real_open = assertions_module.open_prepared_file

    class ShortStream:
        def __init__(self, stream: object):
            self._stream = stream

        def read(self, size: int) -> bytes:
            return self._stream.read(min(size, 2))

        def fileno(self) -> int:
            return self._stream.fileno()

        def close(self) -> None:
            self._stream.close()

    def short_open(prepared: object) -> OpenedFile:
        opened = real_open(prepared)
        if prepared.relative_path == "stdout.log":
            opened.stream = ShortStream(opened.stream)
        return opened

    monkeypatch.setattr(assertions_module, "open_prepared_file", short_open)

    assert _failures(root, TraceExpectations(stdout_contains="needle")) == []


def test_premature_eof_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "store"
    _write_run(root, stdout=b"needle and more")
    real_open = assertions_module.open_prepared_file

    class EarlyEofStream:
        def __init__(self, stream: object):
            self._stream = stream
            self._read = False

        def read(self, size: int) -> bytes:
            if self._read:
                return b""
            self._read = True
            return self._stream.read(2)

        def fileno(self) -> int:
            return self._stream.fileno()

        def close(self) -> None:
            self._stream.close()

    def early_eof_open(prepared: object) -> OpenedFile:
        opened = real_open(prepared)
        if prepared.relative_path == "stdout.log":
            opened.stream = EarlyEofStream(opened.stream)
        return opened

    monkeypatch.setattr(assertions_module, "open_prepared_file", early_eof_open)

    assert _failures(root, TraceExpectations(stdout_contains="needle")) == [
        AssertionFailure("artifact_unreadable", "stdout_contains")
    ]


def test_per_file_cap_boundary_and_match_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "store"
    _trace, trace_dir = _write_run(root, stdout=b"12345678")
    monkeypatch.setattr(assertions_module, "MAX_ARTIFACT_BYTES", 8)

    assert _failures(root, TraceExpectations(stdout_contains="78")) == []

    (trace_dir / "stdout.log").write_bytes(b"needle---beyond")
    assert _failures(root, TraceExpectations(stdout_contains="needle")) == []
    assert _failures(root, TraceExpectations(stdout_contains="absent")) == [
        AssertionFailure("scan_incomplete", "stdout_contains")
    ]


def test_aggregate_limit_is_consumed_stdout_then_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "store"
    _write_run(root, stdout=b"stdout-123", stderr=b"err-ok")
    monkeypatch.setattr(assertions_module, "MAX_ARTIFACT_BYTES", 10)
    monkeypatch.setattr(assertions_module, "MAX_TOTAL_ARTIFACT_BYTES", 16)

    assert _failures(
        root,
        TraceExpectations(
            stdout_contains="stdout",
            stderr_contains="ok",
        ),
    ) == []

    (root / "runs" / RUN_ID / "stderr.log").write_bytes(b"xxxxxxneedle")
    assert _failures(
        root,
        TraceExpectations(
            stdout_contains="stdout",
            stderr_contains="needle",
        ),
    ) == [AssertionFailure("scan_incomplete", "stderr_contains")]
