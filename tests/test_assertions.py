from pathlib import Path

from tracord.assertions import TraceExpectations, evaluate_trace


def valid_trace(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": "tracord.trace.v0",
        "run_id": "run-1",
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
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
        "events": [
            {
                "type": "command.finished",
                "at": "2026-08-05T00:00:00.100Z",
                "data": {},
            }
        ],
    }


def test_evaluate_trace_checks_artifact_content(tmp_path: Path):
    (tmp_path / "stdout.log").write_text("hello from tracord\n", encoding="utf-8")
    (tmp_path / "stderr.log").write_text("", encoding="utf-8")

    failures = evaluate_trace(
        valid_trace(tmp_path),
        trace_dir=tmp_path,
        expectations=TraceExpectations(
            status="passed",
            exit_code=0,
            stdout_contains="tracord",
            max_duration_ms=200,
            no_timeout=True,
        ),
    )

    assert failures == []


def test_evaluate_trace_reports_failures(tmp_path: Path):
    (tmp_path / "stdout.log").write_text("different text\n", encoding="utf-8")
    (tmp_path / "stderr.log").write_text("", encoding="utf-8")
    trace = valid_trace(tmp_path)
    trace["status"] = "failed"
    trace["exit_code"] = 1
    trace["duration_ms"] = 300

    failures = evaluate_trace(
        trace,
        trace_dir=tmp_path,
        expectations=TraceExpectations(
            status="passed",
            exit_code=0,
            stdout_contains="hello",
            max_duration_ms=100,
        ),
    )

    assert "expected status passed, got failed" in failures
    assert "expected exit_code 0, got 1" in failures
    assert "expected stdout to contain 'hello'" in failures
    assert "expected duration_ms <= 100, got 300" in failures