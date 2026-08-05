import sys
from pathlib import Path

from tracord.redaction import REDACTION
from tracord.recorder import record_command
from tracord.schema import validate_trace


def test_record_command_writes_trace(tmp_path: Path):
    trace = record_command(
        [sys.executable, "-c", "print('hello')"],
        root=tmp_path / ".tracord",
        name="unit-test",
    )

    assert trace["status"] == "passed"
    assert trace["exit_code"] == 0
    assert trace["kind"] == "command"
    assert trace["artifacts"] == {"stdout": "stdout.log", "stderr": "stderr.log"}
    assert [event["type"] for event in trace["events"]] == [
        "command.started",
        "command.finished",
    ]
    assert validate_trace(trace) == []
    trace_path = tmp_path / ".tracord" / "runs" / str(trace["run_id"]) / "trace.json"
    stdout_path = tmp_path / ".tracord" / "runs" / str(trace["run_id"]) / "stdout.log"
    assert trace_path.exists()
    assert stdout_path.read_text(encoding="utf-8") == "hello\n"


def test_record_command_redacts_stdout_and_stderr(tmp_path: Path):
    secret = "fixture-sensitive-value"
    trace = record_command(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"print('token={secret}'); "
                f"print('password={secret}', file=sys.stderr)"
            ),
        ],
        root=tmp_path / ".tracord",
    )

    run_path = tmp_path / ".tracord" / "runs" / str(trace["run_id"])
    stdout = (run_path / "stdout.log").read_text(encoding="utf-8")
    stderr = (run_path / "stderr.log").read_text(encoding="utf-8")
    assert stdout == f"token={REDACTION}\n"
    assert stderr == f"password={REDACTION}\n"
    assert secret not in stdout
    assert secret not in stderr
