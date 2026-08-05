"""Trace schema checks used by the CLI and tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any

from .result_codes import (
    DECODE_REPLACEMENT_STATES,
    MAX_PROCESS_EXIT_CODE,
    MAX_SAFE_JSON_INTEGER,
    MIN_PROCESS_EXIT_CODE,
)


SCHEMA_VERSION = "tracord.trace.v0"
STATUSES = {"passed", "failed", "timeout"}
FILE_CHANGE_STATUSES = {"captured", "unchanged", "skipped", "omitted", "error"}
TOOL_EVENT_TYPES = {"tool.call.started", "tool.call.finished"}
TOOL_CAPTURE_STATES = {"captured", "redacted", "omitted"}
TOOL_OUTCOMES = {"succeeded", "failed", "cancelled", "timeout"}
TOOL_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
MAX_TRACE_NESTING_DEPTH = 256
REQUIRED_FIELDS = (
    "schema_version",
    "run_id",
    "kind",
    "status",
    "command",
    "cwd",
    "started_at",
    "finished_at",
    "duration_ms",
    "exit_code",
    "timed_out",
    "redacted",
    "artifacts",
    "events",
)


def validate_trace(trace: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []

    if trace_nesting_exceeded(trace):
        errors.append(f"trace nesting must not exceed {MAX_TRACE_NESTING_DEPTH}")
    if not _json_numbers_safe(trace):
        errors.append("trace numbers must be finite JSON-safe values")

    for field in REQUIRED_FIELDS:
        if field not in trace:
            errors.append(f"missing required field: {field}")

    if trace.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    if trace.get("kind") != "command":
        errors.append("kind must be command")

    if not isinstance(trace.get("run_id"), str) or not trace.get("run_id"):
        errors.append("run_id must be a non-empty string")

    if trace.get("status") not in STATUSES:
        errors.append("status must be one of: failed, passed, timeout")

    command = trace.get("command")
    if not _is_non_empty_string_sequence(command):
        errors.append("command must be a non-empty list of strings")

    if not isinstance(trace.get("cwd"), str) or not trace.get("cwd"):
        errors.append("cwd must be a non-empty string")

    duration_ms = trace.get("duration_ms")
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or not 0 <= duration_ms <= MAX_SAFE_JSON_INTEGER
    ):
        errors.append("duration_ms must be a non-negative JSON-safe integer")

    exit_code = trace.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not MIN_PROCESS_EXIT_CODE <= exit_code <= MAX_PROCESS_EXIT_CODE
    ):
        errors.append("exit_code must be a supported process integer or null")

    if not isinstance(trace.get("timed_out"), bool):
        errors.append("timed_out must be a boolean")

    if not isinstance(trace.get("redacted"), bool):
        errors.append("redacted must be a boolean")

    replacement = trace.get("decode_replacement")
    if replacement is not None and (
        not isinstance(replacement, Mapping)
        or set(replacement) != {"stdout", "stderr"}
        or replacement.get("stdout") not in DECODE_REPLACEMENT_STATES
        or replacement.get("stderr") not in DECODE_REPLACEMENT_STATES
    ):
        errors.append("decode_replacement must contain approved stdout and stderr states")

    if "store_identity_verified" in trace and not isinstance(
        trace.get("store_identity_verified"), bool
    ):
        errors.append("store_identity_verified must be a boolean")

    artifacts = trace.get("artifacts")
    if not isinstance(artifacts, Mapping):
        errors.append("artifacts must be an object")
    else:
        for name in ("stdout", "stderr"):
            if not isinstance(artifacts.get(name), str) or not artifacts.get(name):
                errors.append(f"artifacts.{name} must be a non-empty string")

    events = trace.get("events")
    if not isinstance(events, list):
        errors.append("events must be a list")
    else:
        event_structure_valid = True
        for index, event in enumerate(events):
            if not isinstance(event, Mapping):
                errors.append(f"events[{index}] must be an object")
                event_structure_valid = False
                continue
            error_count = len(errors)
            if not isinstance(event.get("type"), str) or not event.get("type"):
                errors.append(f"events[{index}].type must be a non-empty string")
            if not isinstance(event.get("at"), str) or not event.get("at"):
                errors.append(f"events[{index}].at must be a non-empty string")
            data = event.get("data")
            if not isinstance(data, Mapping):
                errors.append(f"events[{index}].data must be an object")
            elif event.get("type") == "tool.call.started":
                _validate_tool_call_started(index, data, errors)
            elif event.get("type") == "tool.call.finished":
                _validate_tool_call_finished(index, data, errors)
            if len(errors) != error_count:
                event_structure_valid = False

        if event_structure_valid:
            _validate_tool_call_lifecycle(events, errors)

    if "file_changes" in trace:
        _validate_file_changes(trace.get("file_changes"), artifacts, errors)

    return errors


def trace_nesting_exceeded(value: object) -> bool:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if not isinstance(current, (Mapping, list)):
            continue
        if depth >= MAX_TRACE_NESTING_DEPTH:
            return True
        children = current.values() if isinstance(current, Mapping) else current
        pending.extend((child, depth + 1) for child in children)
    return False


def _json_numbers_safe(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, bool) or current is None:
            continue
        if isinstance(current, int):
            if not -MAX_SAFE_JSON_INTEGER <= current <= MAX_SAFE_JSON_INTEGER:
                return False
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                return False
            continue
        if isinstance(current, Mapping):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return True


def _is_non_empty_string_sequence(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    return bool(value) and all(isinstance(item, str) for item in value)


def _validate_tool_call_started(
    index: int,
    data: Mapping[str, Any],
    errors: list[str],
) -> None:
    if not _is_non_empty_string(data.get("call_id")):
        errors.append(f"events[{index}].data.call_id must be a non-empty string")
        return
    if not _is_non_empty_string(data.get("name")):
        errors.append(f"events[{index}].data.name must be a non-empty string")
        return
    input_data = data.get("input")
    if not isinstance(input_data, Mapping):
        errors.append(f"events[{index}].data.input must be an object")
        return
    if not _validate_tool_capture(
        index, "input", input_data, object_value=True, errors=errors
    ):
        return
    if set(data) != {"call_id", "name", "input"}:
        errors.append(
            f"events[{index}].data must contain only approved tool.call.started fields"
        )


def _validate_tool_call_finished(
    index: int,
    data: Mapping[str, Any],
    errors: list[str],
) -> None:
    if not _is_non_empty_string(data.get("call_id")):
        errors.append(f"events[{index}].data.call_id must be a non-empty string")
        return

    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in TOOL_OUTCOMES:
        errors.append(
            f"events[{index}].data.outcome must be one of: cancelled, failed, succeeded, timeout"
        )
        return

    duration_ms = data.get("duration_ms")
    if (
        not _is_json_schema_integer(duration_ms)
        or not 0 <= duration_ms <= MAX_SAFE_JSON_INTEGER
    ):
        errors.append(
            f"events[{index}].data.duration_ms must be a non-negative JSON-safe integer"
        )
        return

    output = data.get("output")
    if not isinstance(output, Mapping):
        errors.append(f"events[{index}].data.output must be an object")
        return
    if not _validate_tool_capture(
        index, "output", output, object_value=False, errors=errors
    ):
        return

    error_type = data.get("error_type")
    if outcome == "failed":
        if (
            not isinstance(error_type, str)
            or TOOL_ERROR_TYPE_PATTERN.fullmatch(error_type) is None
        ):
            errors.append(
                f"events[{index}].data.error_type must be an approved failure classification"
            )
            return
        expected_fields = {"call_id", "outcome", "duration_ms", "output", "error_type"}
    else:
        if "error_type" in data:
            errors.append(
                f"events[{index}].data.error_type is allowed only for failed tool calls"
            )
            return
        expected_fields = {"call_id", "outcome", "duration_ms", "output"}

    if set(data) != expected_fields:
        errors.append(
            f"events[{index}].data must contain only approved tool.call.finished fields"
        )


def _validate_tool_capture(
    index: int,
    field: str,
    value: Mapping[str, Any],
    *,
    object_value: bool,
    errors: list[str],
) -> bool:
    capture = value.get("capture")
    if not isinstance(capture, str) or capture not in TOOL_CAPTURE_STATES:
        errors.append(
            f"events[{index}].data.{field}.capture must be one of: captured, omitted, redacted"
        )
        return False

    has_value = "value" in value
    if capture == "omitted":
        if has_value:
            errors.append(
                f"events[{index}].data.{field}.value is forbidden when capture is omitted"
            )
            return False
        expected_fields = {"capture"}
    else:
        if not has_value:
            errors.append(
                f"events[{index}].data.{field}.value is required when capture is {capture}"
            )
            return False
        if object_value and not isinstance(value["value"], Mapping):
            errors.append(f"events[{index}].data.{field}.value must be an object")
            return False
        expected_fields = {"capture", "value"}

    if set(value) != expected_fields:
        errors.append(
            f"events[{index}].data.{field} must contain only approved capture fields"
        )
        return False
    return True


def _validate_tool_call_lifecycle(
    events: list[object],
    errors: list[str],
) -> None:
    started: set[str] = set()
    finished: set[str] = set()

    for index, event in enumerate(events):
        if not isinstance(event, Mapping) or event.get("type") not in TOOL_EVENT_TYPES:
            continue
        data = event["data"]
        call_id = data["call_id"]
        if event["type"] == "tool.call.started":
            if call_id in started:
                errors.append(
                    f"events[{index}].data.call_id duplicates an earlier tool-call start"
                )
            else:
                started.add(call_id)
            continue

        if call_id not in started:
            errors.append(
                f"events[{index}].data.call_id must reference an earlier tool-call start"
            )
        elif call_id in finished:
            errors.append(
                f"events[{index}].data.call_id duplicates an earlier tool-call finish"
            )
        else:
            finished.add(call_id)


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_json_schema_integer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _validate_file_changes(
    value: object,
    artifacts: object,
    errors: list[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append("file_changes must be an object")
        return

    status = value.get("status")
    if status not in FILE_CHANGE_STATUSES:
        errors.append(
            "file_changes.status must be one of: captured, error, omitted, skipped, unchanged"
        )

    changed_files = value.get("changed_files")
    if changed_files is not None and (
        not isinstance(changed_files, int)
        or isinstance(changed_files, bool)
        or not 0 <= changed_files <= MAX_SAFE_JSON_INTEGER
    ):
        errors.append("file_changes.changed_files must be a non-negative JSON-safe integer")

    files = value.get("files")
    if files is not None:
        if not isinstance(files, list):
            errors.append("file_changes.files must be a list")
        else:
            for index, change in enumerate(files):
                if not isinstance(change, Mapping):
                    errors.append(f"file_changes.files[{index}] must be an object")
                    continue
                if not isinstance(change.get("status"), str) or not change.get("status"):
                    errors.append(f"file_changes.files[{index}].status must be a non-empty string")
                if not isinstance(change.get("path"), str) or not change.get("path"):
                    errors.append(f"file_changes.files[{index}].path must be a non-empty string")

    artifact = value.get("artifact")
    if status == "captured":
        if not isinstance(artifact, str) or not artifact:
            errors.append("captured file_changes must name an artifact")
        elif not isinstance(artifacts, Mapping) or artifacts.get("file_diff") != artifact:
            errors.append("artifacts.file_diff must match file_changes.artifact")
