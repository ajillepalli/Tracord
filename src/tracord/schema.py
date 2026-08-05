"""Trace schema checks used by the CLI and tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "tracord.trace.v0"
STATUSES = {"passed", "failed", "timeout"}
FILE_CHANGE_STATUSES = {"captured", "unchanged", "skipped", "omitted", "error"}
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
    if not isinstance(duration_ms, int) or duration_ms < 0:
        errors.append("duration_ms must be a non-negative integer")

    exit_code = trace.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        errors.append("exit_code must be an integer or null")

    if not isinstance(trace.get("timed_out"), bool):
        errors.append("timed_out must be a boolean")

    if not isinstance(trace.get("redacted"), bool):
        errors.append("redacted must be a boolean")

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
        for index, event in enumerate(events):
            if not isinstance(event, Mapping):
                errors.append(f"events[{index}] must be an object")
                continue
            if not isinstance(event.get("type"), str) or not event.get("type"):
                errors.append(f"events[{index}].type must be a non-empty string")
            if not isinstance(event.get("at"), str) or not event.get("at"):
                errors.append(f"events[{index}].at must be a non-empty string")
            if not isinstance(event.get("data"), Mapping):
                errors.append(f"events[{index}].data must be an object")

    if "file_changes" in trace:
        _validate_file_changes(trace.get("file_changes"), artifacts, errors)

    return errors


def trace_nesting_exceeded(value: object) -> bool:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if not isinstance(current, (Mapping, list)):
            continue
        if depth > MAX_TRACE_NESTING_DEPTH:
            return True
        children = current.values() if isinstance(current, Mapping) else current
        pending.extend((child, depth + 1) for child in children)
    return False


def _is_non_empty_string_sequence(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    return bool(value) and all(isinstance(item, str) for item in value)


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
        not isinstance(changed_files, int) or isinstance(changed_files, bool) or changed_files < 0
    ):
        errors.append("file_changes.changed_files must be a non-negative integer")

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
