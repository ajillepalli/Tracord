import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import tracord.export_preview as preview_module
from tracord.bundle import build_manifest, export_run, import_bundle
from tracord.export_preview import ExportPreviewError, gate_reasons, preview_export
from tracord.recorder import record_command
from tracord.storage import read_json, run_dir, write_json


def _record(store: Path) -> tuple[str, Path]:
    trace = record_command(
        [sys.executable, "-c", "print('preview fixture')"],
        root=store,
    )
    run_id = str(trace["run_id"])
    return run_id, run_dir(store, run_id)


def _add_artifact(trace_directory: Path, name: str, relative_path: str) -> None:
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    trace["artifacts"][name] = relative_path
    write_json(trace_path, trace)


def test_preview_scans_raw_trace_and_artifacts_without_exposing_values(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    trace_secret = "trace-sensitive-value"
    artifact_secret = "artifact-sensitive-value"
    trace["name"] = "token=" + trace_secret
    write_json(trace_path, trace)
    (trace_directory / "stdout.log").write_text(
        "password=" + artifact_secret, encoding="utf-8"
    )

    preview = preview_export(root=store, run_id=run_id)
    serialized = json.dumps(preview, sort_keys=True)

    assert preview["scan"]["complete"] is True
    assert preview["findings"]["gating_total"] == 2
    assert gate_reasons(preview) == ["gating_findings"]
    assert trace_secret not in serialized
    assert artifact_secret not in serialized


def test_already_redacted_assignments_do_not_gate(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stdout.log").write_text(
        "token=[REDACTED]", encoding="utf-8"
    )

    preview = preview_export(root=store, run_id=run_id)

    assert preview["findings"]["gating_total"] == 0
    assert preview["findings"]["already_redacted_total"] == 1
    assert gate_reasons(preview) == []


def test_encoded_candidate_is_advisory(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stdout.log").write_text("a" * 40, encoding="utf-8")

    preview = preview_export(root=store, run_id=run_id)
    stdout_file = next(file for file in preview["files"] if file["path"] == "stdout.log")

    assert stdout_file["findings"]["advisory_total"] == 1
    assert stdout_file["findings"]["gating_total"] == 0
    assert gate_reasons(preview) == []


def test_binary_artifact_is_incomplete_and_can_be_explicitly_allowed(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stdout.log").write_bytes(b"token=hidden\x00binary")

    preview = preview_export(root=store, run_id=run_id)
    stdout_file = next(file for file in preview["files"] if file["path"] == "stdout.log")

    assert stdout_file["status"] == "skipped_binary"
    assert stdout_file["scanned_bytes"] == 0
    assert preview["scan"]["complete"] is False
    assert gate_reasons(preview) == ["incomplete_scan"]
    assert gate_reasons(preview, allow_incomplete_scan=True) == []


def test_text_scan_is_bounded_before_reading_the_whole_file(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    secret = "bounded-sensitive-value"
    scan_limit = (trace_directory / "trace.json").stat().st_size
    (trace_directory / "stdout.log").write_text(
        "token=" + secret + "x" * (scan_limit * 2), encoding="utf-8"
    )

    preview = preview_export(root=store, run_id=run_id, max_scan_bytes=scan_limit)
    stdout_file = next(file for file in preview["files"] if file["path"] == "stdout.log")

    assert stdout_file["status"] == "truncated"
    assert stdout_file["scanned_bytes"] == scan_limit
    assert preview["scan"]["bytes_read"] < stdout_file["size_bytes"] + 2048
    assert preview["findings"]["gating_total"] == 1
    assert secret not in json.dumps(preview)


@pytest.mark.parametrize(
    ("relative_path", "expected_status"),
    [("missing.log", "missing"), ("../outside.log", "unsafe_path")],
)
def test_missing_and_unsafe_artifacts_block_export_prediction(
    tmp_path: Path, relative_path: str, expected_status: str
):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    _add_artifact(trace_directory, "extra", relative_path)

    preview = preview_export(root=store, run_id=run_id)

    assert any(file["status"] == expected_status for file in preview["files"])
    assert preview["export_would_succeed"] is False
    assert preview["scan"]["complete"] is False


def test_directory_artifact_is_rejected(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "artifact-dir").mkdir()
    _add_artifact(trace_directory, "extra", "artifact-dir")

    preview = preview_export(root=store, run_id=run_id)
    artifact = next(file for file in preview["files"] if file["path"] == "artifact-dir")

    assert artifact["status"] == "unreadable"
    assert artifact["reason"] == "not_regular_file"
    with pytest.raises(ValueError, match="regular file"):
        export_run(root=store, run_id=run_id, output_path=tmp_path / "run.zip")


def test_symlink_artifact_is_rejected(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    target = trace_directory / "target.log"
    target.write_text("safe", encoding="utf-8")
    link = trace_directory / "linked.log"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    _add_artifact(trace_directory, "extra", "linked.log")

    preview = preview_export(root=store, run_id=run_id)
    artifact = next(file for file in preview["files"] if file["path"] == "linked.log")

    assert artifact["status"] == "unreadable"
    assert artifact["reason"] == "symlink"


def test_file_replacement_race_is_reported_with_fixed_reason(tmp_path: Path, monkeypatch):
    store = tmp_path / "store"
    _run_id, trace_directory = _record(store)
    monkeypatch.setattr(preview_module, "_same_snapshot", lambda _first, _second: False)

    result = preview_module._scan_file(
        source_dir=trace_directory,
        relative_path="stdout.log",
        file_id="artifact:stdout.log",
        max_scan_bytes=preview_module.DEFAULT_MAX_SCAN_BYTES,
        remaining_bytes=preview_module.MAX_PREVIEW_TOTAL_BYTES,
    )

    assert result.payload["id"] == "artifact:stdout.log"
    assert result.payload["status"] == "unreadable"
    assert result.payload["reason"] == "changed_during_scan"
    assert result.payload["scanned_bytes"] == 0


def test_file_and_aggregate_limits_are_explicit(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    _add_artifact(trace_directory, "third", "third.log")
    (trace_directory / "third.log").write_text("third", encoding="utf-8")
    trace_size = (trace_directory / "trace.json").stat().st_size

    file_limited = preview_export(root=store, run_id=run_id, max_artifacts=1)
    aggregate_limited = preview_export(
        root=store,
        run_id=run_id,
        max_scan_bytes=trace_size,
        max_total_scan_bytes=trace_size,
    )

    assert any(file["status"] == "file_limit" for file in file_limited["files"])
    assert file_limited["scan"]["complete"] is False
    assert file_limited["export_preflight"] == "unknown"
    assert file_limited["export_would_succeed"] is None
    assert gate_reasons(file_limited, allow_incomplete_scan=True) == ["export_blocked"]
    assert file_limited["files_total_is_lower_bound"] is True
    assert any(
        file["status"] == "aggregate_limit" for file in aggregate_limited["files"]
    )
    assert aggregate_limited["scan"]["bytes_read"] == trace_size


def test_preview_is_deterministic_and_does_not_modify_store(tmp_path: Path):
    store = tmp_path / "store"
    run_id, _trace_directory = _record(store)

    before = {
        path.relative_to(store).as_posix(): (
            "file" if path.is_file() else "directory",
            path.read_bytes() if path.is_file() else None,
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in store.rglob("*")
    }
    first = preview_export(root=store, run_id=run_id)
    second = preview_export(root=store, run_id=run_id)
    after = {
        path.relative_to(store).as_posix(): (
            "file" if path.is_file() else "directory",
            path.read_bytes() if path.is_file() else None,
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in store.rglob("*")
    }

    assert first == second
    assert before == after


def test_missing_store_is_not_created(tmp_path: Path):
    store = tmp_path / "does-not-exist"

    with pytest.raises(ExportPreviewError) as exc_info:
        preview_export(root=store, run_id="missing")

    assert exc_info.value.code == "run_not_found"
    assert not store.exists()


def test_invalid_trace_is_an_operational_error(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "trace.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ExportPreviewError) as exc_info:
        preview_export(root=store, run_id=run_id)

    assert exc_info.value.code == "invalid_trace"


def test_invalid_run_id_is_rejected_without_echoing_it(tmp_path: Path):
    with pytest.raises(ExportPreviewError) as exc_info:
        preview_export(root=tmp_path, run_id="../private")

    assert exc_info.value.code == "invalid_run_id"


def test_displayed_run_id_and_safe_artifact_path_are_redacted(tmp_path: Path):
    store = tmp_path / "store"
    original_run_id, original_directory = _record(store)
    secret = "display-sensitive-value"
    requested_run_id = "token=" + secret
    requested_directory = run_dir(store, requested_run_id)
    shutil.copytree(original_directory, requested_directory)
    requested_trace_path = requested_directory / "trace.json"
    requested_trace = read_json(requested_trace_path)
    requested_trace["run_id"] = requested_run_id
    write_json(requested_trace_path, requested_trace)
    artifact_name = "password=" + secret + ".log"
    (requested_directory / artifact_name).write_text("safe", encoding="utf-8")
    _add_artifact(requested_directory, "extra", artifact_name)

    preview = preview_export(root=store, run_id=requested_run_id)
    serialized = json.dumps(preview)

    assert preview["run_id"] is None
    assert preview["run_id_display"] == "token=[REDACTED]"
    assert any(file["path"] == "password=[REDACTED]" for file in preview["files"])
    assert secret not in serialized
    assert original_run_id not in serialized


def test_control_character_run_id_uses_only_safe_display(tmp_path: Path):
    store = tmp_path / "store"
    _original_run_id, original_directory = _record(store)
    run_id = "line\u2028run"
    trace_directory = run_dir(store, run_id)
    shutil.copytree(original_directory, trace_directory)
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    trace["run_id"] = run_id
    write_json(trace_path, trace)

    preview = preview_export(root=store, run_id=run_id)

    assert preview["run_id"] is None
    assert preview["run_id_display"] == "line\\u2028run"


def test_adjacent_secret_command_flag_is_gating_without_exposing_value(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    secret = "command-sensitive-value"
    trace["command"] = ["tool", "--token", secret]
    write_json(trace_path, trace)

    preview = preview_export(root=store, run_id=run_id)
    serialized = json.dumps(preview)

    assert any(
        rule["rule"] == "secret_cli_flag_value"
        for rule in preview["findings"]["by_rule"]
    )
    assert preview["findings"]["gating_total"] == 1
    assert secret not in serialized


def test_secret_in_event_command_copy_is_gating(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    secret = "event-command-sensitive-value"
    trace["command"] = ["tool", "--token", "[REDACTED]"]
    trace["events"][0]["data"]["command"] = ["tool", "--token", secret]
    write_json(trace_path, trace)

    preview = preview_export(root=store, run_id=run_id)

    assert preview["findings"]["gating_total"] >= 1
    assert secret not in json.dumps(preview)


def test_broader_secret_flag_suffix_is_gating(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    trace["command"] = ["tool", "--access-token", "sensitive-value"]
    write_json(trace_path, trace)

    preview = preview_export(root=store, run_id=run_id)

    assert preview["findings"]["gating_total"] >= 1


def test_projected_manifest_is_scanned_and_counted(tmp_path: Path):
    store = tmp_path / "store"
    _original_run_id, original_directory = _record(store)
    secret = "manifest-sensitive-value"
    run_id = "token=" + secret
    trace_directory = run_dir(store, run_id)
    shutil.copytree(original_directory, trace_directory)
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    trace["run_id"] = run_id
    write_json(trace_path, trace)

    preview = preview_export(root=store, run_id=run_id)
    manifest = next(file for file in preview["files"] if file["id"] == "manifest")

    assert manifest["findings"]["gating_total"] == 1
    assert preview["scan"]["files_total"] == 4
    assert secret not in json.dumps(preview)


def test_projected_and_real_manifest_keys_stay_in_sync():
    trace = {"schema_version": "tracord.trace.v0"}
    projected = build_manifest(run_id="run", trace=trace, files=["trace.json"])
    real = build_manifest(
        run_id="run",
        trace=trace,
        files=["trace.json"],
        created_at="2026-01-01T00:00:00Z",
    )

    assert set(projected) == set(real) - {"created_at"}


def test_trace_run_id_must_match_requested_directory(tmp_path: Path):
    store = tmp_path / "store"
    _run_id, original_directory = _record(store)
    copied_directory = run_dir(store, "copied-run")
    shutil.copytree(original_directory, copied_directory)

    with pytest.raises(ExportPreviewError) as preview_error:
        preview_export(root=store, run_id="copied-run")
    with pytest.raises(ValueError, match="does not match"):
        export_run(root=store, run_id="copied-run", output_path=tmp_path / "copy.zip")

    assert preview_error.value.code == "trace_run_id_mismatch"


def test_numeric_trace_run_id_is_invalid_in_preview_and_export(tmp_path: Path):
    store = tmp_path / "store"
    _run_id, original_directory = _record(store)
    numeric_directory = run_dir(store, "123")
    original_directory.rename(numeric_directory)
    trace_path = numeric_directory / "trace.json"
    trace = read_json(trace_path)
    trace["run_id"] = 123
    write_json(trace_path, trace)

    with pytest.raises(ExportPreviewError) as preview_error:
        preview_export(root=store, run_id="123")
    with pytest.raises(ValueError, match="trace is invalid"):
        export_run(root=store, run_id="123", output_path=tmp_path / "run.zip")

    assert preview_error.value.code == "invalid_trace"


def test_symlinked_run_directory_is_rejected(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    external = tmp_path / "external-run"
    trace_directory.rename(external)
    try:
        os.symlink(external, trace_directory, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(ExportPreviewError) as preview_error:
        preview_export(root=store, run_id=run_id)
    with pytest.raises(ValueError, match="real directory"):
        export_run(root=store, run_id=run_id, output_path=tmp_path / "run.zip")

    assert preview_error.value.code == "run_directory_unsafe"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_junction_run_directory_is_rejected_on_python_311(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    external = tmp_path / "junction-target"
    trace_directory.rename(external)
    created = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(trace_directory), str(external)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")
    try:
        with pytest.raises(ExportPreviewError) as preview_error:
            preview_export(root=store, run_id=run_id)
        with pytest.raises(ValueError, match="real directory"):
            export_run(root=store, run_id=run_id, output_path=tmp_path / "run.zip")
        assert preview_error.value.code == "run_directory_unsafe"
    finally:
        trace_directory.rmdir()


def test_invalid_utf8_trace_matches_export_failure(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace_path.write_bytes(trace_path.read_bytes().replace(b"preview", b"\xffreview", 1))

    with pytest.raises(ExportPreviewError) as preview_error:
        preview_export(root=store, run_id=run_id)
    with pytest.raises(ValueError, match="trace is not valid JSON"):
        export_run(root=store, run_id=run_id, output_path=tmp_path / "run.zip")

    assert preview_error.value.code == "invalid_trace_json"


@pytest.mark.parametrize("run_id", [".", "double//segment", "nul\x00segment"])
def test_raw_run_id_segments_are_rejected(tmp_path: Path, run_id: str):
    with pytest.raises(ExportPreviewError) as exc_info:
        preview_export(root=tmp_path, run_id=run_id)

    assert exc_info.value.code == "invalid_run_id"


def test_control_characters_in_artifact_labels_are_escaped(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    unsafe_label = "missing\n\x1b[31m.log"
    _add_artifact(trace_directory, "extra", unsafe_label)

    preview = preview_export(root=store, run_id=run_id)
    artifact = next(file for file in preview["files"] if file["id"].startswith("artifact:"))
    serialized = json.dumps(preview)

    assert "\n" not in artifact["path"]
    assert "\x1b" not in artifact["path"]
    assert "\\u000a" in artifact["path"]
    assert "\\u001b" in artifact["path"]
    assert unsafe_label not in serialized


def test_unicode_line_separator_in_artifact_label_is_escaped(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    _add_artifact(trace_directory, "extra", "missing\u2028line.log")

    preview = preview_export(root=store, run_id=run_id)
    artifact = next(file for file in preview["files"] if file["id"].startswith("artifact:"))

    assert "\u2028" not in artifact["path"]
    assert "\\u2028" in artifact["path"]


def test_artifact_ids_are_opaque_unique_and_do_not_disclose_unsafe_paths(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    _add_artifact(trace_directory, "absolute", "C:/Users/Alice/private.log")
    _add_artifact(trace_directory, "first", "token=first-sensitive.log")
    _add_artifact(trace_directory, "second", "token=second-sensitive.log")

    preview = preview_export(root=store, run_id=run_id)
    artifacts = [file for file in preview["files"] if file["id"].startswith("artifact:")]
    serialized = json.dumps(preview)

    assert len({file["id"] for file in artifacts}) == len(artifacts)
    assert all(file["id"].removeprefix("artifact:").isdigit() for file in artifacts)
    assert "Alice" not in serialized
    assert "first-sensitive" not in serialized
    assert "second-sensitive" not in serialized


def test_export_blocker_cannot_be_allowed_as_incomplete(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    _add_artifact(trace_directory, "extra", "../outside.log")

    preview = preview_export(root=store, run_id=run_id)

    assert gate_reasons(preview, allow_incomplete_scan=True) == ["export_blocked"]


@pytest.mark.parametrize(
    "options",
    [
        {"max_scan_bytes": preview_module.DEFAULT_MAX_SCAN_BYTES + 1},
        {"max_artifacts": preview_module.MAX_PREVIEW_ARTIFACTS + 1},
        {"max_total_scan_bytes": preview_module.MAX_PREVIEW_TOTAL_BYTES + 1},
        {"max_scan_bytes": 2, "max_total_scan_bytes": 1},
        {"max_scan_bytes": 1.5},
        {"max_artifacts": True},
    ],
)
def test_hard_limits_cannot_be_expanded(tmp_path: Path, options):
    with pytest.raises(ExportPreviewError) as exc_info:
        preview_export(root=tmp_path, run_id="run", **options)

    assert exc_info.value.code == "invalid_scan_limit"


def test_trace_json_artifact_is_reported_as_reserved_without_duplicate_scan(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    _add_artifact(trace_directory, "duplicate", "trace.json")

    preview = preview_export(root=store, run_id=run_id)

    assert [file["id"] for file in preview["files"]].count("trace") == 1
    assert preview["scan"]["files_total"] == 5
    assert preview["export_preflight"] == "blocked"
    assert any(
        file.get("reason") == "invalid_artifact_namespace" for file in preview["files"]
    )


def test_bounded_reader_receives_the_exact_file_limit(tmp_path: Path, monkeypatch):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    scan_limit = (trace_directory / "trace.json").stat().st_size
    (trace_directory / "stdout.log").write_text("x" * (scan_limit * 2), encoding="utf-8")
    original_fdopen = preview_module.os.fdopen
    read_limits: list[int] = []

    class TrackingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.stream.close()

        def read(self, size):
            read_limits.append(size)
            return self.stream.read(size)

        def fileno(self):
            return self.stream.fileno()

    def tracking_fdopen(*args, **kwargs):
        return TrackingStream(original_fdopen(*args, **kwargs))

    monkeypatch.setattr(preview_module.os, "fdopen", tracking_fdopen)

    preview_export(root=store, run_id=run_id, max_scan_bytes=scan_limit)

    assert scan_limit in read_limits
    assert all(size <= scan_limit for size in read_limits)


def test_partial_aggregate_read_is_labelled_aggregate_limit(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stderr.log").write_text("x" * 100, encoding="utf-8")
    trace_size = (trace_directory / "trace.json").stat().st_size
    baseline = preview_export(root=store, run_id=run_id)
    manifest_size = next(
        file["scanned_bytes"] for file in baseline["files"] if file["id"] == "manifest"
    )
    total_limit = trace_size + manifest_size + 5

    preview = preview_export(
        root=store,
        run_id=run_id,
        max_scan_bytes=total_limit,
        max_total_scan_bytes=total_limit,
    )
    partial = next(file for file in preview["files"] if file["status"] == "aggregate_limit")

    assert partial["scanned_bytes"] == 5
    assert preview["scan"]["bytes_read"] == trace_size + 5


def test_binary_preview_and_real_export_agree_on_exportability(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stdout.log").write_bytes(b"binary\x00content")

    preview = preview_export(root=store, run_id=run_id)
    bundle = export_run(root=store, run_id=run_id, output_path=tmp_path / "run.zip")

    assert preview["export_would_succeed"] is True
    assert preview["scan"]["complete"] is False
    assert bundle.exists()


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"not-json", "invalid_trace_json"),
        (b"{}\x00", "trace_scan_incomplete"),
        (b"9" * 5000, "invalid_trace_json"),
        (b"[" * 2000 + b"]" * 2000, "invalid_trace"),
    ],
)
def test_trace_failure_codes_are_explicit(tmp_path: Path, content: bytes, code: str):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "trace.json").write_bytes(content)

    with pytest.raises(ExportPreviewError) as exc_info:
        preview_export(root=store, run_id=run_id)

    assert exc_info.value.code == code


def test_deeply_nested_event_data_returns_fixed_error_code(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["events"] = [
        {
            "type": "nested",
            "at": trace["started_at"],
            "data": {"payload": "NESTED_PAYLOAD"},
        }
    ]
    encoded = json.dumps(trace, separators=(",", ":"))
    nested = "[" * 1200 + '"leaf"' + "]" * 1200
    trace_path.write_text(
        encoded.replace('"NESTED_PAYLOAD"', nested), encoding="utf-8"
    )

    with pytest.raises(ExportPreviewError) as exc_info:
        preview_export(root=store, run_id=run_id)

    assert exc_info.value.code == "invalid_trace"


def test_shared_nesting_limit_aligns_preview_export_and_import(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["events"] = [
        {
            "type": "nested",
            "at": trace["started_at"],
            "data": {"payload": "NESTED_PAYLOAD"},
        }
    ]
    encoded = json.dumps(trace, separators=(",", ":"))
    nested = "[" * 300 + '"leaf"' + "]" * 300
    trace_bytes = encoded.replace('"NESTED_PAYLOAD"', nested).encode("utf-8")
    trace_path.write_bytes(trace_bytes)

    with pytest.raises(ExportPreviewError) as preview_error:
        preview_export(root=store, run_id=run_id)
    with pytest.raises(ValueError, match="trace nesting"):
        export_run(root=store, run_id=run_id, output_path=tmp_path / "export.zip")

    bundle = tmp_path / "import.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", trace_bytes)
        archive.writestr("stdout.log", "")
        archive.writestr("stderr.log", "")
    with pytest.raises(ValueError, match="trace nesting"):
        import_bundle(root=tmp_path / "target", bundle_path=bundle)

    assert preview_error.value.code == "invalid_trace"


def test_partial_manifest_scan_handles_unicode_artifact_path(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["artifacts"]["unicode"] = "snowman-\u2603.log"
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    trace_size = trace_path.stat().st_size

    preview = preview_export(
        root=store,
        run_id=run_id,
        max_scan_bytes=trace_size,
        max_total_scan_bytes=trace_size + 8,
    )

    manifest = next(file for file in preview["files"] if file["id"] == "manifest")
    assert manifest["status"] == "aggregate_limit"
    assert manifest["scanned_bytes"] == 8


def test_trace_uses_aggregate_ceiling_independently_of_artifact_cap(tmp_path: Path):
    store = tmp_path / "store"
    run_id, _trace_directory = _record(store)

    preview = preview_export(root=store, run_id=run_id, max_scan_bytes=1)

    assert preview["trace_valid"] is True
    assert preview["scan"]["complete"] is False
    assert "incomplete_scan" in preview["fail_reasons"]


def test_unavailable_file_identity_is_not_certified(tmp_path: Path, monkeypatch):
    store = tmp_path / "store"
    _run_id, trace_directory = _record(store)
    original_lstat = Path.lstat

    class ZeroInode:
        def __init__(self, info):
            self._info = info
            self.st_ino = 0

        def __getattr__(self, name):
            return getattr(self._info, name)

    def zero_inode(path: Path):
        return ZeroInode(original_lstat(path))

    monkeypatch.setattr(Path, "lstat", zero_inode)

    result = preview_module._scan_file(
        source_dir=trace_directory,
        relative_path="stdout.log",
        file_id="artifact:stdout.log",
        max_scan_bytes=preview_module.DEFAULT_MAX_SCAN_BYTES,
        remaining_bytes=preview_module.MAX_PREVIEW_TOTAL_BYTES,
    )

    assert result.payload["status"] == "identity_unverified"
    assert result.payload["reason"] == "identity_unavailable"
    run_id = trace_directory.name
    bundle = export_run(root=store, run_id=run_id, output_path=tmp_path / "run.zip")
    assert bundle.exists()


def test_nested_artifact_and_parent_failures_are_explicit(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    nested = trace_directory / "nested" / "artifact.log"
    nested.parent.mkdir()
    nested.write_text("nested", encoding="utf-8")
    _add_artifact(trace_directory, "nested", "nested/artifact.log")

    preview = preview_export(root=store, run_id=run_id)

    assert any(file["path"] == "nested/artifact.log" for file in preview["files"])

    _add_artifact(trace_directory, "missing_parent", "absent/file.log")
    preview = preview_export(root=store, run_id=run_id)
    missing_parent = next(
        file for file in preview["files"] if file.get("reason") == "missing_parent"
    )
    assert missing_parent["status"] == "unreadable"


def test_trace_larger_than_artifact_cap_can_be_previewed(tmp_path: Path):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = read_json(trace_path)
    trace["name"] = "x" * (preview_module.MAX_SCAN_BYTES + 1024)
    write_json(trace_path, trace)

    preview = preview_export(root=store, run_id=run_id)

    assert preview["trace_valid"] is True
    assert next(file for file in preview["files"] if file["id"] == "trace")["status"] == "scanned"
