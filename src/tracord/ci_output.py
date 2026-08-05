"""Stable, privacy-safe result construction and JSON emission."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping, Sequence
from typing import BinaryIO

from .result_codes import (
    ASSERTION_ERROR_CODES,
    ASSERTION_ERROR_LOCATION,
    ASSERTION_EXPECTATION_LOCATIONS,
    ASSERTION_FAILURE_CODES,
    ASSERTION_FAILURE_KINDS,
    ASSERTION_INDETERMINATE_CODES,
    ASSERTION_MISMATCH_CODES,
    ASSERTION_OUTCOMES,
    ASSERTION_RESULT_VERSION,
    CI_RUN_ID,
    COMMAND_ASSERT,
    COMMAND_LIST,
    COMMAND_RECORD,
    COMMAND_REPLAY,
    DECODE_REPLACEMENT_STATES,
    LIST_ERROR_CODES,
    LIST_RESULT_VERSION,
    MAX_LIST_RUNS,
    MAX_PROCESS_EXIT_CODE,
    MAX_SAFE_JSON_INTEGER,
    MIN_PROCESS_EXIT_CODE,
    RECORD_ERROR_CODES,
    RECORD_RESULT_VERSION,
    REPLAY_ERROR_CODES,
    REPLAY_RESULT_VERSION,
    TRACE_STATUSES,
)


class CIOutputError(ValueError):
    """A path-free result construction error."""


def project_full_run(trace: Mapping[str, object]) -> dict[str, object]:
    """Project a published trace into the frozen full-run result shape."""
    projected = project_list_run(trace)
    replacement = trace.get("decode_replacement")
    if replacement is None:
        legacy_state = "unknown" if trace.get("timed_out") is True else "none"
        decoded = {"stdout": legacy_state, "stderr": legacy_state}
    elif isinstance(replacement, Mapping):
        stdout = replacement.get("stdout")
        stderr = replacement.get("stderr")
        if stdout not in DECODE_REPLACEMENT_STATES or stderr not in DECODE_REPLACEMENT_STATES:
            raise CIOutputError("invalid run projection")
        decoded = {"stdout": stdout, "stderr": stderr}
    else:
        raise CIOutputError("invalid run projection")
    identity = trace.get("store_identity_verified", False)
    if not isinstance(identity, bool):
        raise CIOutputError("invalid run projection")
    projected["decode_replacement"] = decoded
    projected["store_identity_verified"] = identity
    return projected


def project_list_run(trace: Mapping[str, object]) -> dict[str, object]:
    """Project a validated trace into the frozen list-run result shape."""
    if not isinstance(trace, Mapping):
        raise CIOutputError("invalid run projection")
    run_id = trace.get("run_id")
    status = trace.get("status")
    process_exit_code = trace.get("exit_code")
    timed_out = trace.get("timed_out")
    duration_ms = trace.get("duration_ms")
    redacted = trace.get("redacted")
    if not isinstance(run_id, str) or CI_RUN_ID.fullmatch(run_id) is None:
        raise CIOutputError("invalid run projection")
    if status not in TRACE_STATUSES:
        raise CIOutputError("invalid run projection")
    if process_exit_code is not None and not _bounded_integer(
        process_exit_code,
        minimum=MIN_PROCESS_EXIT_CODE,
        maximum=MAX_PROCESS_EXIT_CODE,
    ):
        raise CIOutputError("invalid run projection")
    if not isinstance(timed_out, bool):
        raise CIOutputError("invalid run projection")
    if not _bounded_integer(duration_ms, minimum=0, maximum=MAX_SAFE_JSON_INTEGER):
        raise CIOutputError("invalid run projection")
    if not isinstance(redacted, bool):
        raise CIOutputError("invalid run projection")
    if status == "passed" and (process_exit_code != 0 or timed_out):
        raise CIOutputError("invalid run projection")
    if status == "failed" and (
        process_exit_code is None or process_exit_code == 0 or timed_out
    ):
        raise CIOutputError("invalid run projection")
    if status == "timeout" and (process_exit_code is not None or not timed_out):
        raise CIOutputError("invalid run projection")
    return {
        "run_id": run_id,
        "status": status,
        "process_exit_code": process_exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "redacted": redacted,
    }


def build_record_result(
    *, exit_code: int, run: Mapping[str, object] | None, error_code: str | None = None
) -> dict[str, object]:
    """Build one record-result envelope."""
    return _build_run_result(
        result_version=RECORD_RESULT_VERSION,
        command=COMMAND_RECORD,
        allowed_errors=RECORD_ERROR_CODES,
        exit_code=exit_code,
        run=run,
        error_code=error_code,
    )


def build_replay_result(
    *, exit_code: int, run: Mapping[str, object] | None, error_code: str | None = None
) -> dict[str, object]:
    """Build one replay-result envelope."""
    return _build_run_result(
        result_version=REPLAY_RESULT_VERSION,
        command=COMMAND_REPLAY,
        allowed_errors=REPLAY_ERROR_CODES,
        exit_code=exit_code,
        run=run,
        error_code=error_code,
    )


def build_assertion_result(
    *,
    exit_code: int,
    outcome: str,
    run_id: str | None,
    source: str,
    case: str | None,
    failures: Sequence[Mapping[str, object]],
    error_code: str | None = None,
    error_location: str | None = None,
) -> dict[str, object]:
    """Build one assertion-result envelope."""
    _validate_command_exit(exit_code)
    if outcome not in ASSERTION_OUTCOMES:
        raise CIOutputError("invalid assertion outcome")
    if source not in {"inline", "file"}:
        raise CIOutputError("invalid assertion source")
    if run_id is not None and (
        not isinstance(run_id, str) or CI_RUN_ID.fullmatch(run_id) is None
    ):
        raise CIOutputError("invalid assertion run id")
    if case is not None and (
        source != "file"
        or not isinstance(case, str)
        or CI_RUN_ID.fullmatch(case) is None
    ):
        raise CIOutputError("invalid assertion case")
    if error_code is not None and error_code not in ASSERTION_ERROR_CODES:
        raise CIOutputError("invalid assertion error")
    if error_location is not None and (
        not isinstance(error_location, str)
        or ASSERTION_ERROR_LOCATION.fullmatch(error_location) is None
    ):
        raise CIOutputError("invalid assertion error location")
    if error_code is None and error_location is not None:
        raise CIOutputError("assertion error location without error")

    projected_failures: list[dict[str, str]] = []
    for failure in failures:
        if not isinstance(failure, Mapping):
            raise CIOutputError("invalid assertion failure")
        code = failure.get("code")
        location = failure.get("location")
        if code not in ASSERTION_FAILURE_CODES or location not in ASSERTION_EXPECTATION_LOCATIONS:
            raise CIOutputError("invalid assertion failure")
        kind = "mismatch" if code in ASSERTION_MISMATCH_CODES else "indeterminate"
        supplied_kind = failure.get("kind", kind)
        if supplied_kind != kind or supplied_kind not in ASSERTION_FAILURE_KINDS:
            raise CIOutputError("invalid assertion failure kind")
        projected_failures.append({"code": code, "kind": kind, "location": location})
    if len(projected_failures) > len(ASSERTION_EXPECTATION_LOCATIONS):
        raise CIOutputError("too many assertion failures")

    expected_outcome = _assertion_outcome(projected_failures, error_code)
    if outcome != expected_outcome:
        raise CIOutputError("inconsistent assertion outcome")
    if (exit_code == 0) != (outcome == "pass"):
        raise CIOutputError("inconsistent assertion exit code")
    if outcome == "error" and exit_code not in {1, 2}:
        raise CIOutputError("inconsistent assertion exit code")
    if outcome in {"mismatch", "indeterminate"} and exit_code != 1:
        raise CIOutputError("inconsistent assertion exit code")
    return {
        "result_version": ASSERTION_RESULT_VERSION,
        "command": COMMAND_ASSERT,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "error": error_code,
        "outcome": outcome,
        "run_id": run_id,
        "source": source,
        "case": case,
        "failures": projected_failures,
        "error_location": error_location,
    }


def build_list_result(
    *,
    exit_code: int,
    runs: Sequence[Mapping[str, object]],
    skipped: int,
    truncated: bool,
    error_code: str | None = None,
) -> dict[str, object]:
    """Build one list-result envelope."""
    _validate_command_exit(exit_code)
    if error_code is not None and error_code not in LIST_ERROR_CODES:
        raise CIOutputError("invalid list error")
    if not _bounded_integer(skipped, minimum=0, maximum=MAX_SAFE_JSON_INTEGER):
        raise CIOutputError("invalid skipped count")
    if not isinstance(truncated, bool):
        raise CIOutputError("invalid truncated flag")
    projected_runs = [project_list_run(run) for run in runs]
    if len(projected_runs) > MAX_LIST_RUNS:
        raise CIOutputError("too many list runs")
    if error_code is None:
        if exit_code != 0:
            raise CIOutputError("list failure without error")
    elif exit_code == 0 or projected_runs or skipped or truncated:
        raise CIOutputError("inconsistent list error")
    return {
        "result_version": LIST_RESULT_VERSION,
        "command": COMMAND_LIST,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "error": error_code,
        "runs": projected_runs,
        "skipped": skipped,
        "truncated": truncated,
    }


def serialize_json(payload: Mapping[str, object]) -> bytes:
    """Serialize one deterministic JSON object and trailing LF."""
    if not isinstance(payload, Mapping):
        raise CIOutputError("result must be an object")
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise CIOutputError("result is not serializable") from None
    return encoded + b"\n"


class JsonEmitter:
    """Attempt at most one guarded result emission."""

    def __init__(self, *, stream: object | None = None) -> None:
        self.stream = stream
        self.emission_started = False
        self.emitted = False

    def emit(self, payload: Mapping[str, object]) -> bool:
        """Return true only after a complete flush."""
        if self.emission_started:
            return False
        self.emission_started = True
        try:
            data = serialize_json(payload)
            stream = sys.stdout if self.stream is None else self.stream
            binary = _binary_stream(stream)
            if binary is not None:
                written = binary.write(data)
                if written != len(data):
                    return False
                binary.flush()
            else:
                text = data.decode("utf-8")
                write = getattr(stream, "write", None)
                flush = getattr(stream, "flush", None)
                if not callable(write) or not callable(flush):
                    return False
                written = write(text)
                if written != len(text):
                    return False
                flush()
        except (BrokenPipeError, OSError, ValueError, TypeError, AttributeError, CIOutputError):
            return False
        self.emitted = True
        return True


def _binary_stream(stream: object) -> BinaryIO | None:
    """Return a stdout binary stream when one is exposed."""
    if isinstance(stream, (io.BufferedIOBase, io.RawIOBase, io.BytesIO)):
        return stream
    candidate = getattr(stream, "buffer", None)
    if candidate is not None and callable(getattr(candidate, "write", None)) and callable(
        getattr(candidate, "flush", None)
    ):
        return candidate
    return None


def _build_run_result(
    *,
    result_version: str,
    command: str,
    allowed_errors: frozenset[str],
    exit_code: int,
    run: Mapping[str, object] | None,
    error_code: str | None,
) -> dict[str, object]:
    _validate_command_exit(exit_code)
    if error_code is not None and error_code not in allowed_errors:
        raise CIOutputError("invalid run-result error")
    projected_run = project_full_run(run) if run is not None else None
    if error_code is None:
        if projected_run is None or exit_code not in {0, 1}:
            raise CIOutputError("inconsistent run result")
        expected_exit = 0 if projected_run["status"] == "passed" else 1
        if exit_code != expected_exit:
            raise CIOutputError("inconsistent run result")
    elif projected_run is not None or exit_code == 0:
        raise CIOutputError("inconsistent run-result error")
    return {
        "result_version": result_version,
        "command": command,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "error": error_code,
        "run": projected_run,
    }


def _assertion_outcome(
    failures: Sequence[Mapping[str, str]], error_code: str | None
) -> str:
    if error_code is not None:
        if failures:
            raise CIOutputError("assertion error with failures")
        return "error"
    if not failures:
        return "pass"
    if any(failure["code"] in ASSERTION_INDETERMINATE_CODES for failure in failures):
        return "indeterminate"
    return "mismatch"


def _validate_command_exit(value: object) -> None:
    if not _bounded_integer(value, minimum=0, maximum=MAX_SAFE_JSON_INTEGER):
        raise CIOutputError("invalid command exit code")


def _bounded_integer(value: object, *, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )
