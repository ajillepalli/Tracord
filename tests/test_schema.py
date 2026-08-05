from tracord.schema import validate_trace


def test_validate_trace_accepts_command_trace():
    errors = validate_trace(
        {
            "schema_version": "tracord.trace.v0",
            "run_id": "run-1",
            "kind": "command",
            "name": None,
            "status": "passed",
            "command": ["python", "--version"],
            "cwd": "/repo",
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
                    "type": "command.started",
                    "at": "2026-08-05T00:00:00.000Z",
                    "data": {},
                }
            ],
        }
    )

    assert errors == []


def test_validate_trace_rejects_missing_required_fields():
    errors = validate_trace({"schema_version": "tracord.trace.v0"})

    assert "missing required field: run_id" in errors
    assert "missing required field: events" in errors