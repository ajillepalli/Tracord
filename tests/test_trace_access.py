from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracord.schema import SCHEMA_VERSION
from tracord.storage import StoreSafetyError
from tracord import trace_access
from tracord.paths import IdentityComparison, SafePathError
from tracord.trace_access import TraceAccessError, load_trace, parse_trace, read_trace, resolve_run_directory


def _write(root: Path, directory_id: str, *, trace_id: str | None = None) -> None:
    run = root / "runs" / directory_id
    run.mkdir(parents=True)
    trace = {
        "schema_version": SCHEMA_VERSION,
        "run_id": trace_id or directory_id,
        "kind": "command",
        "status": "passed",
        "command": ["echo", "private"],
        "cwd": "C:/private",
        "started_at": "start",
        "finished_at": "finish",
        "duration_ms": 1,
        "exit_code": 0,
        "timed_out": False,
        "redacted": True,
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
        "events": [],
    }
    (run / "trace.json").write_text(json.dumps(trace), encoding="utf-8")


@pytest.mark.parametrize(
    ("run_id", "code"),
    [("../escape", "invalid_run_id"), ("missing", "run_not_found")],
)
def test_load_trace_returns_fixed_path_free_errors(
    tmp_path: Path, run_id: str, code: str
) -> None:
    with pytest.raises(TraceAccessError) as exc_info:
        load_trace(tmp_path / "store", run_id)

    assert exc_info.value.code == code
    assert str(tmp_path) not in str(exc_info.value)


def test_trace_id_must_exactly_match_directory(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _write(store, "run-a", trace_id="run-b")

    with pytest.raises(TraceAccessError) as exc_info:
        load_trace(store, "run-a")

    assert exc_info.value.code == "run_identity_mismatch"


def test_case_alias_does_not_select_a_run(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _write(store, "run-a")

    with pytest.raises(TraceAccessError) as exc_info:
        load_trace(store, "RUN-A")

    assert exc_info.value.code == "run_identity_mismatch"


def test_duplicate_keys_and_nonfinite_numbers_are_invalid(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _write(store, "run-a")
    path = store / "runs" / "run-a" / "trace.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(original[:-1] + ',"run_id":"run-a"}', encoding="utf-8")

    with pytest.raises(TraceAccessError) as duplicate:
        load_trace(store, "run-a")
    assert duplicate.value.code == "trace_invalid"

    path.write_text(original.replace('"duration_ms": 1', '"duration_ms": NaN'), encoding="utf-8")
    with pytest.raises(TraceAccessError) as nonfinite:
        load_trace(store, "run-a")
    assert nonfinite.value.code == "trace_invalid"


def test_missing_trace_is_distinct(tmp_path: Path) -> None:
    run = tmp_path / "store" / "runs" / "run-a"
    run.mkdir(parents=True)

    with pytest.raises(TraceAccessError) as exc_info:
        load_trace(tmp_path / "store", "run-a")

    assert exc_info.value.code == "trace_missing"


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("identity_unverifiable", "run_identity_unverifiable"),
        ("changed", "run_identity_mismatch"),
        ("redirected", "run_identity_mismatch"),
    ],
)
def test_store_identity_failures_remain_distinguishable(
    tmp_path: Path,
    reason: str,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_root: Path) -> object:
        raise StoreSafetyError(reason)

    monkeypatch.setattr(trace_access, "prepare_store_for_read", fail)

    with pytest.raises(TraceAccessError) as exc_info:
        load_trace(tmp_path / "store", "run-a")

    assert exc_info.value.code == code


def test_preparse_nesting_limit_counts_root_and_ignores_quoted_delimiters(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    _write(store, "run-a")
    path = store / "runs" / "run-a" / "trace.json"
    base = path.read_text(encoding="utf-8")[:-1]

    accepted = base + ',"extra":' + "[" * 255 + "0" + "]" * 255 + "}"
    assert parse_trace(accepted.encode("utf-8"))["run_id"] == "run-a"

    rejected = base + ',"extra":' + "[" * 256 + "0" + "]" * 256 + "}"
    with pytest.raises(TraceAccessError) as exc_info:
        parse_trace(rejected.encode("utf-8"))
    assert exc_info.value.code == "trace_invalid"

    quoted_value = "[{" * 300 + '\\"' * 300 + "]}" * 300
    quoted = base + ',"extra":' + json.dumps(quoted_value) + "}"
    assert parse_trace(quoted.encode("utf-8"))["run_id"] == "run-a"


def test_trace_snapshot_change_and_unavailable_descriptor_identity_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    _write(store, "run-a")
    run = resolve_run_directory(store, "run-a")

    def changed(_prepared: object) -> object:
        raise SafePathError("changed")

    monkeypatch.setattr(trace_access, "open_prepared_file", changed)
    with pytest.raises(TraceAccessError) as changed_error:
        read_trace(run)
    assert changed_error.value.code == "run_identity_mismatch"

    monkeypatch.undo()
    run = resolve_run_directory(store, "run-a")
    monkeypatch.setattr(
        trace_access,
        "verify_opened_file",
        lambda _opened: IdentityComparison.UNAVAILABLE,
    )
    with pytest.raises(TraceAccessError) as unavailable_error:
        read_trace(run)
    assert unavailable_error.value.code == "run_identity_unverifiable"


@pytest.mark.parametrize("short_read", [True, False])
def test_trace_shrink_or_growth_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_read: bool
) -> None:
    store = tmp_path / "store"
    _write(store, "run-a")
    run = resolve_run_directory(store, "run-a")
    if short_read:
        monkeypatch.setattr(trace_access, "_read_exact", lambda _stream, _size: (b"", True))
    else:
        real_open = trace_access.open_prepared_file

        class GrowingStream:
            def __init__(self, stream: object):
                self._stream = stream

            def read(self, size: int) -> bytes:
                data = self._stream.read(size)
                return data if data else b"x"

            def fileno(self) -> int:
                return self._stream.fileno()

            def close(self) -> None:
                self._stream.close()

        def growing(prepared: object) -> object:
            opened = real_open(prepared)
            opened.stream = GrowingStream(opened.stream)
            return opened

        monkeypatch.setattr(trace_access, "open_prepared_file", growing)

    with pytest.raises(TraceAccessError) as exc_info:
        read_trace(run)
    assert exc_info.value.code == "trace_unreadable"


def test_post_read_run_replacement_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    _write(store, "run-a")
    run = resolve_run_directory(store, "run-a")

    def fail(_run: object) -> None:
        raise TraceAccessError("run_identity_mismatch")

    monkeypatch.setattr(trace_access, "verify_run_directory", fail)
    with pytest.raises(TraceAccessError) as exc_info:
        read_trace(run)
    assert exc_info.value.code == "run_identity_mismatch"
