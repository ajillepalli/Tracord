import json
import io
import sys
from pathlib import Path

import pytest

from tracord.cli import main, write_json_stdout
from tracord.recorder import record_command
from tracord.storage import run_dir


def _record(store: Path) -> tuple[str, Path]:
    trace = record_command([sys.executable, "-c", "print('safe')"], root=store)
    run_id = str(trace["run_id"])
    return run_id, run_dir(store, run_id)


def test_json_preview_is_machine_readable_and_writes_no_bundle(
    tmp_path: Path, capsys, monkeypatch
):
    store = tmp_path / "store"
    run_id, _trace_directory = _record(store)
    output = tmp_path / f"{run_id}.tracord.zip"
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        ["export", "--store", str(store), "--preview", "--json", run_id]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["preview_version"] == "tracord.export-preview.v0"
    assert captured.err == ""
    assert not output.exists()


def test_json_writer_uses_lf_bytes(monkeypatch):
    class BinaryStdout:
        def __init__(self):
            self.buffer = io.BytesIO()

        def flush(self):
            pass

    stdout = BinaryStdout()
    monkeypatch.setattr(sys, "stdout", stdout)

    write_json_stdout({"status": "ok"})

    assert stdout.buffer.getvalue() == b'{"status":"ok"}\n'


def test_human_preview_writes_utf8_when_stdout_is_redirected(tmp_path: Path, monkeypatch):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    trace_path = trace_directory / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["artifacts"]["unicode"] = "snowman-\u2603.log"
    trace_path.write_text(json.dumps(trace), encoding="utf-8")

    class BinaryStdout:
        def __init__(self):
            self.buffer = io.BytesIO()

        def flush(self):
            pass

    stdout = BinaryStdout()
    monkeypatch.setattr(sys, "stdout", stdout)

    exit_code = main(["export", "--store", str(store), "--preview", run_id])

    assert exit_code == 0
    assert "snowman-\u2603.log" in stdout.buffer.getvalue().decode("utf-8")


def test_strict_gate_uses_exit_three_and_never_prints_secret(tmp_path: Path, capsys):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    secret = "cli-sensitive-value"
    (trace_directory / "stdout.log").write_text("token=" + secret, encoding="utf-8")

    exit_code = main(
        [
            "export",
            "--store",
            str(store),
            "--preview",
            "--json",
            "--fail-on-findings",
            run_id,
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 3
    assert payload["fail_reasons"] == ["gating_findings"]
    assert payload["gate_enforced"] is True
    assert secret not in captured.out
    assert secret not in captured.err


def test_incomplete_scan_fails_unless_explicitly_allowed(tmp_path: Path, capsys):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stdout.log").write_bytes(b"binary\x00content")

    denied = main(
        ["export", "--store", str(store), "--preview", "--fail-on-findings", run_id]
    )
    capsys.readouterr()
    allowed = main(
        [
            "export",
            "--store",
            str(store),
            "--preview",
            "--fail-on-findings",
            "--allow-incomplete-scan",
            run_id,
        ]
    )

    assert denied == 3
    assert allowed == 0


@pytest.mark.parametrize(
    "options",
    [
        ["--json"],
        ["--fail-on-findings"],
        ["--allow-incomplete-scan"],
        ["--max-scan-bytes", "100"],
    ],
)
def test_preview_only_options_require_preview(tmp_path: Path, capsys, monkeypatch, options):
    monkeypatch.chdir(tmp_path)
    exit_code = main(["export", *options, "run-id"])

    assert exit_code == 2
    assert "require --preview" in capsys.readouterr().err


@pytest.mark.parametrize("option", [["--output", "bundle.zip"], ["--overwrite"]])
def test_preview_rejects_write_options(tmp_path: Path, capsys, monkeypatch, option):
    monkeypatch.chdir(tmp_path)
    exit_code = main(["export", "--preview", *option, "run-id"])

    assert exit_code == 2
    assert "cannot be used" in capsys.readouterr().err


def test_preview_operational_error_uses_exit_one(tmp_path: Path, capsys):
    store = tmp_path / "missing-store"

    exit_code = main(
        ["export", "--store", str(store), "--preview", "--json", "missing"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    error_payload = json.loads(captured.out)
    assert error_payload == {
        "preview_version": "tracord.export-preview.v0",
        "trace_valid": None,
        "error": "run_not_found",
    }
    assert captured.err.endswith("run_not_found\n")
    assert not store.exists()


def test_json_reports_gate_reasons_even_when_gate_is_not_enforced(tmp_path: Path, capsys):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stdout.log").write_text(
        "token=unenforced-sensitive-value", encoding="utf-8"
    )

    exit_code = main(
        ["export", "--store", str(store), "--preview", "--json", run_id]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["fail_reasons"] == ["gating_findings"]
    assert payload["gate_enforced"] is False


def test_cli_max_scan_bytes_reaches_preview(tmp_path: Path, capsys):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    scan_limit = (trace_directory / "trace.json").stat().st_size
    (trace_directory / "stdout.log").write_text("x" * (scan_limit * 2), encoding="utf-8")

    exit_code = main(
        [
            "export",
            "--store",
            str(store),
            "--preview",
            "--json",
            "--max-scan-bytes",
            str(scan_limit),
            run_id,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["scan"]["max_scan_bytes"] == scan_limit
    assert any(file["status"] == "truncated" for file in payload["files"])


def test_invalid_cli_scan_limit_is_usage_error_with_json(tmp_path: Path, capsys):
    store = tmp_path / "store"
    run_id, _trace_directory = _record(store)

    exit_code = main(
        [
            "export",
            "--store",
            str(store),
            "--preview",
            "--json",
            "--max-scan-bytes",
            str(20 * 1024 * 1024),
            run_id,
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out)["error"] == "invalid_scan_limit"


@pytest.mark.parametrize(
    "run_id", [".", "stream:name", "trailing.", "NUL", ".tracord-reserved.lock"]
)
def test_invalid_run_id_is_usage_error_with_json(tmp_path: Path, capsys, run_id: str):
    exit_code = main(["export", "--preview", "--json", run_id])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out)["error"] == "invalid_run_id"


def test_human_output_says_would_fail_when_gate_is_not_enforced(tmp_path: Path, capsys):
    store = tmp_path / "store"
    run_id, trace_directory = _record(store)
    (trace_directory / "stdout.log").write_text(
        "token=human-sensitive-value", encoding="utf-8"
    )

    exit_code = main(["export", "--store", str(store), "--preview", run_id])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "gate would fail: gating_findings" in captured.out
    assert "gate failed:" not in captured.out
