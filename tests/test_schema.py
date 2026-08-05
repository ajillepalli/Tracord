from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from tracord.schema import validate_trace
from tracord.result_codes import MAX_PROCESS_EXIT_CODE, MAX_SAFE_JSON_INTEGER


SCHEMA = json.loads(
    (Path(__file__).parents[1] / "schemas" / "trace-v0.schema.json").read_text(
        encoding="utf-8"
    )
)
SCHEMA_VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


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


def tool_started(
    call_id: str = "call-1",
    *,
    capture: str = "captured",
    value: object = None,
    include_value: bool = True,
) -> dict[str, object]:
    input_data: dict[str, object] = {"capture": capture}
    if include_value:
        input_data["value"] = {} if value is None else value
    return {
        "type": "tool.call.started",
        "at": "2026-08-05T00:00:00.000Z",
        "data": {"call_id": call_id, "name": "example.tool", "input": input_data},
    }


def tool_finished(
    call_id: str = "call-1",
    *,
    outcome: str = "succeeded",
    capture: str = "captured",
    value: object = None,
    include_value: bool = True,
    duration_ms: object = 10,
) -> dict[str, object]:
    output: dict[str, object] = {"capture": capture}
    if include_value:
        output["value"] = value
    data: dict[str, object] = {
        "call_id": call_id,
        "outcome": outcome,
        "duration_ms": duration_ms,
        "output": output,
    }
    if outcome == "failed":
        data["error_type"] = "Tool.ExecutionFailed"
    return {
        "type": "tool.call.finished",
        "at": "2026-08-04T23:59:59.000Z",
        "data": data,
    }


def trace_for_event(event: dict[str, object]) -> dict[str, object]:
    events = [event]
    if event["type"] == "tool.call.finished":
        data = event.get("data")
        call_id = data.get("call_id") if isinstance(data, dict) else None
        valid_call_id = (
            isinstance(call_id, str)
            and 1 <= len(call_id) <= 512
            and not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in call_id)
        )
        events.insert(0, tool_started(call_id if valid_call_id else "call-1"))
    return valid_trace(events=events)


def schema_errors(trace: dict[str, object]) -> list[object]:
    return list(SCHEMA_VALIDATOR.iter_errors(trace))


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


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (None, "events[0] must be an object"),
        ({"type": "", "at": "valid", "data": {}}, ".type must be a non-empty string"),
        ({"type": [], "at": "valid", "data": {}}, ".type must be a non-empty string"),
        ({"type": "custom", "at": "", "data": {}}, ".at must be a non-empty string"),
        ({"type": "custom", "at": [], "data": {}}, ".at must be a non-empty string"),
        ({"type": "custom", "at": "valid", "data": []}, ".data must be an object"),
    ],
)
def test_validate_trace_rejects_malformed_event_envelopes(event, expected):
    errors = validate_trace(valid_trace(events=[event]))

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    ("capture", "include_value"),
    [("captured", True), ("redacted", True), ("omitted", False)],
)
def test_validate_trace_accepts_tool_input_capture_states(capture, include_value):
    assert validate_trace(
        valid_trace(events=[tool_started(capture=capture, include_value=include_value)])
    ) == []


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_validate_trace_accepts_falsey_tool_outputs(value):
    events = [tool_started(), tool_finished(value=value)]

    assert validate_trace(valid_trace(events=events)) == []


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled", "timeout"])
def test_validate_trace_accepts_tool_outcomes(outcome):
    events = [tool_started(), tool_finished(outcome=outcome)]

    assert validate_trace(valid_trace(events=events)) == []


def test_validate_trace_accepts_interrupted_tool_call():
    assert validate_trace(valid_trace(events=[tool_started()])) == []


def test_validate_trace_accepts_interleaved_tool_calls_and_nonmonotonic_timestamps():
    events = [
        tool_started("call-1"),
        tool_started("call-2"),
        tool_finished("call-2"),
        tool_finished("call-1"),
    ]

    assert validate_trace(valid_trace(status="failed", events=events)) == []


def invalid_tool_events():
    cases = []

    event = tool_started(capture="unknown")
    cases.append((event, ".input.capture must be one of"))
    event = tool_started()
    event["data"]["input"]["capture"] = []
    cases.append((event, ".input.capture must be one of"))
    event = tool_started(include_value=False)
    cases.append((event, ".input.value is required"))
    event = tool_started(capture="omitted", include_value=True)
    cases.append((event, ".input.value is forbidden"))
    event = tool_started(value=[])
    cases.append((event, ".input.value must be an object"))
    event = tool_started()
    del event["data"]["input"]
    cases.append((event, ".input must be an object"))
    event = tool_started()
    event["data"]["input"] = []
    cases.append((event, ".input must be an object"))
    event = tool_started()
    event["data"]["input"]["extra"] = "secret"
    cases.append((event, ".input must contain only approved capture fields"))
    event = tool_started()
    event["data"]["extra"] = "secret"
    cases.append((event, "only approved tool.call.started fields"))
    event = tool_started()
    event["data"]["call_id"] = ""
    cases.append((event, ".call_id must be a 1-512 character control-free string"))
    event = tool_started("A" * 513)
    cases.append((event, ".call_id must be a 1-512 character control-free string"))
    event = tool_started("unsafe\nidentifier")
    cases.append((event, ".call_id must be a 1-512 character control-free string"))
    event = tool_started()
    event["data"]["name"] = ""
    cases.append((event, ".name must be a 1-512 character control-free string"))
    event = tool_started()
    event["data"]["name"] = "A" * 513
    cases.append((event, ".name must be a 1-512 character control-free string"))
    event = tool_started()
    event["data"]["name"] = "unsafe\tname"
    cases.append((event, ".name must be a 1-512 character control-free string"))

    event = tool_finished(outcome="unknown")
    cases.append((event, ".outcome must be one of"))
    event = tool_finished()
    del event["data"]["call_id"]
    cases.append((event, ".call_id must be a 1-512 character control-free string"))
    event = tool_finished()
    event["data"]["call_id"] = []
    cases.append((event, ".call_id must be a 1-512 character control-free string"))
    event = tool_finished()
    event["data"]["outcome"] = []
    cases.append((event, ".outcome must be one of"))
    for duration in (
        True,
        -1,
        0.5,
        MAX_SAFE_JSON_INTEGER + 1,
        float("nan"),
        float("inf"),
    ):
        event = tool_finished(duration_ms=duration)
        cases.append((event, ".duration_ms must be a non-negative JSON-safe integer"))
    event = tool_finished()
    del event["data"]["output"]
    cases.append((event, ".output must be an object"))
    event = tool_finished()
    event["data"]["output"] = []
    cases.append((event, ".output must be an object"))
    event = tool_finished(capture="omitted", include_value=True)
    cases.append((event, ".output.value is forbidden"))
    event = tool_finished(include_value=False)
    cases.append((event, ".output.value is required"))
    event = tool_finished(outcome="failed")
    del event["data"]["error_type"]
    cases.append((event, ".error_type must be an approved failure classification"))
    event = tool_finished()
    event["data"]["error_type"] = "NotAllowed"
    cases.append((event, ".error_type is allowed only for failed tool calls"))
    event = tool_finished(outcome="failed")
    event["data"]["error_type"] = "raw exception text!"
    cases.append((event, ".error_type must be an approved failure classification"))
    event = tool_finished(outcome="failed")
    event["data"]["error_type"] = "Safe\n"
    cases.append((event, ".error_type must be an approved failure classification"))
    event = tool_finished()
    event["data"]["extra"] = "secret"
    cases.append((event, "only approved tool.call.finished fields"))
    return cases


@pytest.mark.parametrize(("event", "expected"), invalid_tool_events())
def test_validate_trace_rejects_invalid_tool_event_structure(event, expected):
    errors = validate_trace(trace_for_event(event))

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([tool_started(), tool_started()], "duplicates an earlier tool-call start"),
        ([tool_finished()], "must reference an earlier tool-call start"),
        (
            [tool_finished(), tool_started()],
            "must reference an earlier tool-call start",
        ),
        (
            [tool_started(), tool_finished(), tool_finished()],
            "duplicates an earlier tool-call finish",
        ),
        (
            [tool_started(), tool_finished(), tool_started()],
            "duplicates an earlier tool-call start",
        ),
    ],
)
def test_validate_trace_rejects_invalid_tool_lifecycle(events, expected):
    assert any(expected in error for error in validate_trace(valid_trace(events=events)))


def test_validate_trace_tool_errors_do_not_echo_untrusted_values():
    secret = "super-secret-tool-value"
    event = tool_started(secret)

    errors = validate_trace(valid_trace(events=[event, deepcopy(event)]))

    assert any("duplicates an earlier tool-call start" in error for error in errors)
    assert all(secret not in error for error in errors)


def test_validate_trace_structural_errors_do_not_echo_untrusted_values():
    secret = "super-secret-tool-value"
    invalid_name = tool_started()
    invalid_name["data"]["name"] = secret + "\n"
    invalid_input = tool_started()
    invalid_input["data"]["input"]["value"] = [secret]
    invalid_error = tool_finished(outcome="failed")
    invalid_error["data"]["error_type"] = secret + "!"

    for event in (invalid_name, invalid_input, invalid_error):
        errors = validate_trace(trace_for_event(event))
        assert errors
        assert all(secret not in error for error in errors)


def test_validate_trace_defers_lifecycle_until_all_event_structure_is_valid():
    events = [
        {"type": "custom", "at": "", "data": {}},
        tool_started(),
        tool_started(),
    ]

    errors = validate_trace(valid_trace(events=events))

    assert "events[0].at must be a non-empty string" in errors
    assert not any("duplicates an earlier tool-call start" in error for error in errors)


@pytest.mark.parametrize("duration", [0, 1.0, MAX_SAFE_JSON_INTEGER])
def test_validate_trace_accepts_tool_duration_boundaries(duration):
    events = [tool_started(), tool_finished(duration_ms=duration)]

    assert validate_trace(valid_trace(events=events)) == []


@pytest.mark.parametrize(
    "value",
    [MAX_SAFE_JSON_INTEGER + 1, float("inf"), {"nested": [float("nan")]}],
)
def test_validate_trace_rejects_unsafe_captured_output_numbers(value):
    errors = validate_trace(
        valid_trace(events=[tool_started(), tool_finished(value=value)])
    )

    assert "trace numbers must be finite JSON-safe values" in errors


def test_validate_trace_rejects_excessive_nesting_in_captured_output():
    value: object = None
    for _ in range(256):
        value = {"nested": value}

    errors = validate_trace(
        valid_trace(events=[tool_started(), tool_finished(value=value)])
    )

    assert "trace nesting must not exceed 256" in errors


def structural_parity_events():
    valid_started = tool_started()
    valid_finished = tool_finished()
    valid_omitted = tool_finished(capture="omitted", include_value=False)
    valid_null = tool_finished(value=None)
    valid_integral_float = tool_finished(duration_ms=1.0)
    valid_failed = tool_finished(outcome="failed")
    valid_max_error_type = tool_finished(outcome="failed")
    valid_max_error_type["data"]["error_type"] = "A" + ("x" * 63)
    valid_max_identifiers = tool_started("A" * 512)
    valid_max_identifiers["data"]["name"] = "N" * 512

    invalid_started = tool_started(include_value=False)
    invalid_missing_started_data = tool_started()
    del invalid_missing_started_data["data"]
    invalid_input = tool_started(value=[])
    invalid_missing_input = tool_started()
    del invalid_missing_input["data"]["input"]
    invalid_nonobject_input = tool_started()
    invalid_nonobject_input["data"]["input"] = []
    invalid_empty_started_call_id = tool_started("")
    invalid_long_started_call_id = tool_started("A" * 513)
    invalid_control_started_call_id = tool_started("unsafe\nidentifier")
    invalid_empty_tool_name = tool_started()
    invalid_empty_tool_name["data"]["name"] = ""
    invalid_long_tool_name = tool_started()
    invalid_long_tool_name["data"]["name"] = "N" * 513
    invalid_control_tool_name = tool_started()
    invalid_control_tool_name["data"]["name"] = "unsafe\tname"
    invalid_capture = tool_started(capture="invalid")
    invalid_omitted_value = tool_started(capture="omitted", include_value=True)
    invalid_capture_extra = tool_started()
    invalid_capture_extra["data"]["input"]["extra"] = True
    invalid_finished = tool_finished(include_value=False)
    invalid_missing_finished_data = tool_finished()
    del invalid_missing_finished_data["data"]
    invalid_missing_output = tool_finished()
    del invalid_missing_output["data"]["output"]
    invalid_nonobject_output = tool_finished()
    invalid_nonobject_output["data"]["output"] = []
    invalid_empty_finished_call_id = tool_finished("")
    invalid_output_capture = tool_finished(capture="invalid")
    invalid_omitted_output_value = tool_finished(capture="omitted", include_value=True)
    invalid_output_extra = tool_finished()
    invalid_output_extra["data"]["output"]["extra"] = True
    invalid_failed = tool_finished(outcome="failed")
    del invalid_failed["data"]["error_type"]
    invalid_duration = tool_finished(duration_ms=True)
    invalid_negative_duration = tool_finished(duration_ms=-1)
    invalid_fractional_duration = tool_finished(duration_ms=0.5)
    invalid_oversized_duration = tool_finished(duration_ms=MAX_SAFE_JSON_INTEGER + 1)
    invalid_outcome = tool_finished(outcome="invalid")
    invalid_nonfailed_error = tool_finished()
    invalid_nonfailed_error["data"]["error_type"] = "Unexpected"
    invalid_newline_error = tool_finished(outcome="failed")
    invalid_newline_error["data"]["error_type"] = "Safe\n"
    invalid_long_error = tool_finished(outcome="failed")
    invalid_long_error["data"]["error_type"] = "A" + ("x" * 64)
    invalid_extra = tool_finished()
    invalid_extra["data"]["extra"] = "not-allowed"

    return [
        (valid_started, True),
        (valid_finished, True),
        (valid_omitted, True),
        (valid_null, True),
        (valid_integral_float, True),
        (valid_failed, True),
        (valid_max_error_type, True),
        (valid_max_identifiers, True),
        (invalid_started, False),
        (invalid_missing_started_data, False),
        (invalid_input, False),
        (invalid_missing_input, False),
        (invalid_nonobject_input, False),
        (invalid_empty_started_call_id, False),
        (invalid_long_started_call_id, False),
        (invalid_control_started_call_id, False),
        (invalid_empty_tool_name, False),
        (invalid_long_tool_name, False),
        (invalid_control_tool_name, False),
        (invalid_capture, False),
        (invalid_omitted_value, False),
        (invalid_capture_extra, False),
        (invalid_finished, False),
        (invalid_missing_finished_data, False),
        (invalid_missing_output, False),
        (invalid_nonobject_output, False),
        (invalid_empty_finished_call_id, False),
        (invalid_output_capture, False),
        (invalid_omitted_output_value, False),
        (invalid_output_extra, False),
        (invalid_failed, False),
        (invalid_duration, False),
        (invalid_negative_duration, False),
        (invalid_fractional_duration, False),
        (invalid_oversized_duration, False),
        (invalid_outcome, False),
        (invalid_nonfailed_error, False),
        (invalid_newline_error, False),
        (invalid_long_error, False),
        (invalid_extra, False),
    ]


@pytest.mark.parametrize(("event", "expected_valid"), structural_parity_events())
def test_json_schema_and_python_validator_agree_on_tool_event_structure(
    event, expected_valid
):
    trace = trace_for_event(event)

    assert (validate_trace(trace) == []) is expected_valid
    assert (schema_errors(trace) == []) is expected_valid


def test_unknown_event_type_keeps_open_data_and_envelope():
    event = {
        "type": "vendor.custom",
        "at": "2026-08-05T00:00:00.000Z",
        "data": {"anything": {"is": "allowed"}},
        "extension": True,
    }
    trace = valid_trace(events=[event])

    assert validate_trace(trace) == []
    assert schema_errors(trace) == []


def test_unknown_tool_call_prefix_event_remains_open():
    event = {
        "type": "tool.call.progress",
        "at": "2026-08-05T00:00:00.000Z",
        "data": {"future": "shape"},
    }
    trace = valid_trace(events=[event])

    assert validate_trace(trace) == []
    assert schema_errors(trace) == []


def test_known_tool_event_keeps_open_outer_envelope():
    event = tool_started()
    event["extension"] = {"future": True}
    trace = valid_trace(events=[event])

    assert validate_trace(trace) == []
    assert schema_errors(trace) == []


def test_existing_command_and_file_diff_events_retain_open_data():
    events = [
        {
            "type": "command.finished",
            "at": "2026-08-05T00:00:00.000Z",
            "data": {"exit_code": 0, "future": True},
        },
        {
            "type": "file.diff",
            "at": "2026-08-05T00:00:00.000Z",
            "data": {"status": "omitted", "reason": "disabled"},
        },
    ]
    trace = valid_trace(events=events)

    assert validate_trace(trace) == []
    assert schema_errors(trace) == []


def test_json_schema_structure_does_not_claim_python_trace_safety_bounds():
    deeply_nested: object = None
    for _ in range(256):
        deeply_nested = {"nested": deeply_nested}

    for value in (MAX_SAFE_JSON_INTEGER + 1, deeply_nested):
        trace = valid_trace(events=[tool_started(), tool_finished(value=value)])

        assert schema_errors(trace) == []
        assert validate_trace(trace)
