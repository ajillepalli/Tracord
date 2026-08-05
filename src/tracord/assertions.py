"""Deterministic, bounded trace assertions."""

from __future__ import annotations

import codecs
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .bundle import TRACE_FILE, validate_run_id
from .paths import (
    IdentityComparison,
    SafePathError,
    combine_identity,
    compare_snapshot,
    containment_issue,
    is_link_or_junction,
    open_prepared_file,
    prepare_regular_file,
    verify_opened_file,
)
from .schema import STATUSES, validate_trace
from .storage import RUNS_DIR


MAX_TRACE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
MAX_NEEDLE_BYTES = 65_536
TAIL_BYTES = MAX_NEEDLE_BYTES - 1


class ExpectationValidationError(ValueError):
    """A fixed-code expectation construction error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AssertionRunError(ValueError):
    """A fixed-code run evaluation error."""

    def __init__(self, code: str, location: str | None = None) -> None:
        self.code = code
        self.location = location
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AssertionFailure:
    code: str
    location: str


@dataclass(frozen=True)
class TraceExpectations:
    status: str | None = None
    exit_code: int | None = None
    stdout_contains: str | bytes | None = field(default=None, repr=False)
    stderr_contains: str | bytes | None = field(default=None, repr=False)
    max_duration_ms: int | None = None
    no_timeout: bool = False
    stdout_contains_bytes: bytes | None = field(init=False, repr=False)
    stderr_contains_bytes: bytes | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stdout_contains_bytes",
            _try_encode_needle(self.stdout_contains),
        )
        object.__setattr__(
            self,
            "stderr_contains_bytes",
            _try_encode_needle(self.stderr_contains),
        )


def validate_expectations(expectations: TraceExpectations) -> None:
    """Validate expectation values before any run or assertion-file I/O."""
    if not isinstance(expectations, TraceExpectations):
        raise ExpectationValidationError("invalid_expectations")
    if expectations.status is not None and expectations.status not in STATUSES:
        raise ExpectationValidationError("invalid_status_expectation")
    if expectations.exit_code is not None and (
        not isinstance(expectations.exit_code, int)
        or isinstance(expectations.exit_code, bool)
    ):
        raise ExpectationValidationError("invalid_exit_code_expectation")
    if expectations.max_duration_ms is not None and (
        not isinstance(expectations.max_duration_ms, int)
        or isinstance(expectations.max_duration_ms, bool)
        or expectations.max_duration_ms < 0
    ):
        raise ExpectationValidationError("invalid_duration_expectation")
    if not isinstance(expectations.no_timeout, bool):
        raise ExpectationValidationError("invalid_timeout_expectation")
    if (
        expectations.stdout_contains is not None
        and expectations.stdout_contains_bytes is None
    ):
        raise ExpectationValidationError("invalid_stdout_expectation")
    if (
        expectations.stderr_contains is not None
        and expectations.stderr_contains_bytes is None
    ):
        raise ExpectationValidationError("invalid_stderr_expectation")
    if not any(
        (
            expectations.status is not None,
            expectations.exit_code is not None,
            expectations.stdout_contains is not None,
            expectations.stderr_contains is not None,
            expectations.max_duration_ms is not None,
            expectations.no_timeout,
        )
    ):
        raise ExpectationValidationError("expectation_required")


@dataclass(frozen=True, slots=True)
class _RunDirectory:
    root_path: Path
    root_initial: os.stat_result
    runs_path: Path
    runs_initial: os.stat_result
    path: Path
    initial: os.stat_result


def evaluate_run(
    root: Path,
    run_id: str,
    expectations: TraceExpectations,
) -> tuple[str, list[AssertionFailure]]:
    """Evaluate one run and return only stable, content-free failure codes."""
    validate_expectations(expectations)
    if not isinstance(run_id, str):
        raise AssertionRunError("invalid_run_id")
    try:
        validate_run_id(run_id)
    except ValueError:
        raise AssertionRunError("invalid_run_id") from None

    run, run_failure = _resolve_run_directory(root, run_id)
    if run_failure is not None or run is None:
        raise AssertionRunError(run_failure or "run_not_found")

    trace_bytes, trace_failure = _read_trace(run.path)
    if trace_failure is not None or trace_bytes is None:
        raise AssertionRunError(trace_failure or "trace_unreadable")

    try:
        trace_text = trace_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise AssertionRunError("trace_invalid_utf8") from None
    try:
        trace = json.loads(trace_text)
    except RecursionError:
        raise AssertionRunError("trace_invalid") from None
    except (json.JSONDecodeError, ValueError):
        raise AssertionRunError("trace_invalid_json") from None
    if not isinstance(trace, dict) or validate_trace(trace):
        raise AssertionRunError("trace_invalid")
    if trace.get("run_id") != run_id:
        raise AssertionRunError("trace_run_id_mismatch")

    failures: list[AssertionFailure] = []
    if expectations.status is not None and trace.get("status") != expectations.status:
        failures.append(AssertionFailure("assertion_mismatch", "status"))
    if (
        expectations.exit_code is not None
        and trace.get("exit_code") != expectations.exit_code
    ):
        failures.append(AssertionFailure("assertion_mismatch", "exit_code"))

    artifacts = trace.get("artifacts")
    remaining_bytes = MAX_TOTAL_ARTIFACT_BYTES
    stdout_failure, stdout_read = _contains_failure(
        artifacts.get("stdout") if isinstance(artifacts, Mapping) else None,
        trace_dir=run.path,
        label="stdout",
        expected=expectations.stdout_contains_bytes,
        remaining_bytes=remaining_bytes,
    )
    remaining_bytes -= stdout_read
    if stdout_failure is not None:
        failures.append(stdout_failure)

    stderr_failure, _stderr_read = _contains_failure(
        artifacts.get("stderr") if isinstance(artifacts, Mapping) else None,
        trace_dir=run.path,
        label="stderr",
        expected=expectations.stderr_contains_bytes,
        remaining_bytes=remaining_bytes,
    )
    if stderr_failure is not None:
        failures.append(stderr_failure)

    duration_ms = trace.get("duration_ms")
    if (
        expectations.max_duration_ms is not None
        and isinstance(duration_ms, int)
        and duration_ms > expectations.max_duration_ms
    ):
        failures.append(AssertionFailure("assertion_mismatch", "max_duration_ms"))
    if expectations.no_timeout and trace.get("timed_out") is not False:
        failures.append(AssertionFailure("assertion_mismatch", "no_timeout"))

    run_failure = _verify_run_directory(run)
    if run_failure is not None:
        raise AssertionRunError(run_failure)
    return run.path.name, failures


def _try_encode_needle(value: str | bytes | None) -> bytes | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="strict")
        elif isinstance(value, bytes):
            value.decode("utf-8", errors="strict")
            encoded = value
        else:
            raise TypeError
    except (TypeError, UnicodeError):
        return None
    if not encoded or len(encoded) > MAX_NEEDLE_BYTES:
        return None
    return encoded


def _resolve_run_directory(
    root: Path, run_id: str
) -> tuple[_RunDirectory | None, str | None]:
    runs_path = root / RUNS_DIR
    try:
        root_initial = root.lstat()
        runs_initial = runs_path.lstat()
    except FileNotFoundError:
        return None, "run_not_found"
    except OSError:
        return None, "run_directory_unreadable"
    if (
        is_link_or_junction(root, root_initial)
        or not stat.S_ISDIR(root_initial.st_mode)
        or is_link_or_junction(runs_path, runs_initial)
        or not stat.S_ISDIR(runs_initial.st_mode)
        or containment_issue(root, runs_path) is not None
    ):
        return None, "run_directory_unsafe"

    try:
        with os.scandir(runs_path) as entries:
            exact_name = next((entry.name for entry in entries if entry.name == run_id), None)
    except OSError:
        return None, "run_directory_unreadable"
    if exact_name is None:
        return None, "run_not_found"

    path = runs_path / exact_name
    if containment_issue(runs_path, path) is not None:
        return None, "run_directory_unsafe"
    try:
        initial = path.lstat()
    except FileNotFoundError:
        return None, "run_not_found"
    except OSError:
        return None, "run_directory_unreadable"
    if is_link_or_junction(path, initial) or not stat.S_ISDIR(initial.st_mode):
        return None, "run_directory_unsafe"

    try:
        root_checked = root.lstat()
        runs_checked = runs_path.lstat()
        checked = path.lstat()
    except OSError:
        return None, "run_directory_changed"
    identity = combine_identity(
        compare_snapshot(root_initial, root_checked),
        compare_snapshot(runs_initial, runs_checked),
        compare_snapshot(initial, checked),
    )
    if identity is IdentityComparison.DIFFERENT:
        return None, "run_directory_changed"
    if identity is IdentityComparison.UNAVAILABLE:
        return None, "run_directory_identity_unverified"
    return _RunDirectory(
        root,
        root_initial,
        runs_path,
        runs_initial,
        path,
        initial,
    ), None


def _verify_run_directory(run: _RunDirectory) -> str | None:
    try:
        root_final = run.root_path.lstat()
        runs_final = run.runs_path.lstat()
        final = run.path.lstat()
    except OSError:
        return "run_directory_changed"
    if (
        is_link_or_junction(run.root_path, root_final)
        or not stat.S_ISDIR(root_final.st_mode)
        or is_link_or_junction(run.runs_path, runs_final)
        or not stat.S_ISDIR(runs_final.st_mode)
        or is_link_or_junction(run.path, final)
        or not stat.S_ISDIR(final.st_mode)
        or containment_issue(run.root_path, run.runs_path) is not None
        or containment_issue(run.runs_path, run.path) is not None
    ):
        return "run_directory_changed"
    identity = combine_identity(
        compare_snapshot(run.root_initial, root_final),
        compare_snapshot(run.runs_initial, runs_final),
        compare_snapshot(run.initial, final),
    )
    if identity is IdentityComparison.DIFFERENT:
        return "run_directory_changed"
    if identity is IdentityComparison.UNAVAILABLE:
        return "run_directory_identity_unverified"
    return None


def _read_trace(trace_dir: Path) -> tuple[bytes | None, str | None]:
    try:
        prepared = prepare_regular_file(
            trace_dir, TRACE_FILE, require_single_link=True
        )
    except SafePathError as exc:
        if exc.reason == "missing":
            return None, "trace_missing"
        return None, "trace_unreadable"
    if prepared.initial.st_size > MAX_TRACE_BYTES:
        return None, "trace_too_large"

    try:
        with open_prepared_file(prepared) as opened:
            data, short_read = _read_exact(opened.stream, prepared.initial.st_size)
            identity = verify_opened_file(opened)
    except SafePathError as exc:
        return None, "trace_changed" if exc.reason == "changed" else "trace_unreadable"
    except OSError:
        return None, "trace_unreadable"
    if short_read:
        return None, "trace_unreadable"
    if identity is IdentityComparison.UNAVAILABLE:
        return None, "trace_identity_unverified"
    return data, None


def _contains_failure(
    artifact_name: object,
    *,
    trace_dir: Path,
    label: str,
    expected: bytes | None,
    remaining_bytes: int,
) -> tuple[AssertionFailure | None, int]:
    location = f"{label}_contains"
    if expected is None:
        return None, 0
    if not isinstance(artifact_name, str) or not artifact_name:
        return AssertionFailure("assertion_artifact_missing", location), 0
    if remaining_bytes <= 0:
        return AssertionFailure("assertion_scan_incomplete", location), 0

    try:
        prepared = prepare_regular_file(
            trace_dir, artifact_name, require_single_link=True
        )
    except SafePathError as exc:
        if exc.reason == "missing":
            return AssertionFailure("assertion_artifact_missing", location), 0
        return AssertionFailure("assertion_artifact_unreadable", location), 0

    read_limit = min(
        prepared.initial.st_size,
        MAX_ARTIFACT_BYTES,
        remaining_bytes,
    )
    matched = False
    invalid_utf8 = False
    short_read = False
    bytes_read = 0
    tail = b""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")

    try:
        with open_prepared_file(prepared) as opened:
            while bytes_read < read_limit:
                requested = min(READ_CHUNK_BYTES, read_limit - bytes_read)
                chunk = opened.stream.read(requested)
                if not chunk:
                    short_read = True
                    break
                bytes_read += len(chunk)
                if len(chunk) > requested:
                    short_read = True
                    break
                if not invalid_utf8:
                    try:
                        decoder.decode(chunk, final=False)
                    except UnicodeDecodeError:
                        invalid_utf8 = True
                if expected in tail + chunk:
                    matched = True
                tail = (tail + chunk)[-TAIL_BYTES:]

            scan_complete = prepared.initial.st_size <= read_limit
            if scan_complete and not short_read and not invalid_utf8:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    invalid_utf8 = True
            identity = verify_opened_file(opened)
    except SafePathError as exc:
        code = (
            "assertion_artifact_changed"
            if exc.reason == "changed"
            else "assertion_artifact_unreadable"
        )
        return AssertionFailure(code, location), bytes_read
    except OSError:
        return AssertionFailure("assertion_artifact_unreadable", location), bytes_read

    if short_read:
        return AssertionFailure("assertion_artifact_unreadable", location), bytes_read
    if identity is IdentityComparison.UNAVAILABLE:
        return AssertionFailure("assertion_identity_unverified", location), bytes_read
    if invalid_utf8:
        return AssertionFailure("assertion_artifact_invalid_utf8", location), bytes_read
    if matched:
        return None, bytes_read
    if not scan_complete:
        return AssertionFailure("assertion_scan_incomplete", location), bytes_read
    return AssertionFailure("assertion_mismatch", location), bytes_read


def _read_exact(stream: object, size: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    bytes_read = 0
    while bytes_read < size:
        requested = min(READ_CHUNK_BYTES, size - bytes_read)
        chunk = stream.read(requested)
        if not chunk:
            return b"".join(chunks), True
        if len(chunk) > requested:
            return b"".join((*chunks, chunk[:requested])), True
        chunks.append(chunk)
        bytes_read += len(chunk)
    return b"".join(chunks), False
