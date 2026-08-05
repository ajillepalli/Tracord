from tracord.schema import validate_trace
from tracord.result_codes import MAX_PROCESS_EXIT_CODE, MAX_SAFE_JSON_INTEGER


def valid_trace(**overrides: object) -> dict[str, object]:
    trace: dict[str, object] = {
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
        "decode_replacement": {"stdout": "none", "stderr": "none"},
        "store_identity_verified": True,
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
        "events": [
            {
                "type": "command.started",
                "at": "2026-08-05T00:00:00.000Z",
                "data": {},
            }
        ],
    }
    trace.update(overrides)
    return trace


def test_validate_trace_accepts_command_trace():
    errors = validate_trace(valid_trace())

    assert errors == []


def test_validate_trace_rejects_missing_required_fields():
    errors = validate_trace({"schema_version": "tracord.trace.v0"})

    assert "missing required field: run_id" in errors
    assert "missing required field: events" in errors


def test_validate_trace_rejects_bool_and_noninteroperable_integer_fields():
    assert "duration_ms must be a non-negative JSON-safe integer" in validate_trace(
        valid_trace(duration_ms=True)
    )
    assert "duration_ms must be a non-negative JSON-safe integer" in validate_trace(
        valid_trace(duration_ms=MAX_SAFE_JSON_INTEGER + 1)
    )
    assert "exit_code must be a supported process integer or null" in validate_trace(
        valid_trace(exit_code=True)
    )
    assert "exit_code must be a supported process integer or null" in validate_trace(
        valid_trace(exit_code=MAX_PROCESS_EXIT_CODE + 1)
    )


def test_validate_trace_rejects_invalid_decode_metadata():
    errors = validate_trace(
        valid_trace(decode_replacement={"stdout": "maybe", "stderr": "none"})
    )
    assert "decode_replacement must contain approved stdout and stderr states" in errors
