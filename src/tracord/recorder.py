"""Command recording primitives."""

from __future__ import annotations

import locale
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .git_capture import DEFAULT_GIT_TIMEOUT_SECONDS, DEFAULT_MAX_DIFF_BYTES, GitDiffCapture
from .redaction import redact_text
from .schema import SCHEMA_VERSION
from .storage import (
    PreparedRun,
    StoreSafetyError,
    prepare_run_for_write,
    publish_prepared_json,
    verify_prepared_run,
    write_prepared_bytes,
)


STDOUT_ARTIFACT = "stdout.log"
STDERR_ARTIFACT = "stderr.log"


class RecordError(ValueError):
    """A fixed-code, path-free recording failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return f"{stamp}-{suffix}"


def record_command(
    command: list[str],
    *,
    root: Path,
    name: str | None = None,
    timeout_seconds: float | None = None,
    redact: bool = True,
    capture_diff: bool = False,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    if not command:
        raise RecordError("record_command_required")

    run_id = new_run_id()
    try:
        prepared_run = prepare_run_for_write(root, run_id)
    except (OSError, StoreSafetyError):
        raise RecordError("record_store_unwritable") from None
    output_dir = prepared_run.path

    working_directory = Path.cwd()
    cwd = str(working_directory)
    output_encoding = _preferred_output_encoding()
    diff_capture: GitDiffCapture | None = None

    try:
        if capture_diff:
            diff_capture = GitDiffCapture(
                cwd=working_directory,
                store=root,
                max_diff_bytes=max_diff_bytes,
                redact=redact,
                git_timeout_seconds=git_timeout_seconds,
            )
            diff_capture.start()

        started_at = utc_now()
        started = time.monotonic()
        timed_out = False
        events: list[dict[str, object]] = [
            {
                "type": "command.started",
                "at": started_at,
                "data": {
                    "command": command,
                    "cwd": cwd,
                    "timeout_seconds": timeout_seconds,
                },
            }
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                cwd=working_directory,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            stdout, stdout_replacement = _decode_output(
                completed.stdout, output_encoding
            )
            stderr, stderr_replacement = _decode_output(
                completed.stderr, output_encoding
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout, stdout_replacement = _decode_output(exc.stdout, output_encoding)
            stderr, stderr_replacement = _decode_output(exc.stderr, output_encoding)
        except OSError:
            raise RecordError("record_spawn_failed") from None

        finished_at = utc_now()
        duration_ms = int((time.monotonic() - started) * 1000)
        stored_stdout = redact_text(stdout) if redact else stdout
        stored_stderr = redact_text(stderr) if redact else stderr

        if timed_out:
            status = "timeout"
        elif exit_code == 0:
            status = "passed"
        else:
            status = "failed"

        events.append(
            {
                "type": "command.finished",
                "at": finished_at,
                "data": {
                    "status": status,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "duration_ms": duration_ms,
                },
            }
        )

        try:
            _require_store_identity(prepared_run)
            write_prepared_bytes(prepared_run, STDOUT_ARTIFACT, stored_stdout.encode("utf-8"))
            _require_store_identity(prepared_run)
            write_prepared_bytes(prepared_run, STDERR_ARTIFACT, stored_stderr.encode("utf-8"))
        except (OSError, StoreSafetyError):
            raise RecordError("record_store_unwritable") from None

        file_changes: dict[str, object] | None = None
        if diff_capture is not None:
            file_changes = diff_capture.finish(output_dir)
            events.append(
                {
                    "type": "file.diff",
                    "at": utc_now(),
                    "data": file_changes,
                }
            )

        artifacts = {
            "stdout": STDOUT_ARTIFACT,
            "stderr": STDERR_ARTIFACT,
        }
        if file_changes is not None and isinstance(file_changes.get("artifact"), str):
            artifacts["file_diff"] = str(file_changes["artifact"])

        trace: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "kind": "command",
            "name": name,
            "status": status,
            "command": command,
            "cwd": cwd,
            "pid": os.getpid(),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "timeout_seconds": timeout_seconds,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "redacted": redact,
            "output_encoding": output_encoding,
            "decode_replacement": {
                "stdout": stdout_replacement,
                "stderr": stderr_replacement,
            },
            "store_identity_verified": prepared_run.store.identity_verified,
            "artifacts": artifacts,
            "events": events,
        }
        if file_changes is not None:
            trace["file_changes"] = file_changes
        try:
            _require_store_identity(prepared_run)
            publish_prepared_json(prepared_run, "trace.json", trace)
            _require_store_identity(prepared_run)
        except (OSError, StoreSafetyError):
            raise RecordError("record_store_unwritable") from None
        return trace
    finally:
        if diff_capture is not None:
            diff_capture.close()


def _preferred_output_encoding() -> str:
    encoding = locale.getpreferredencoding(False)
    return encoding or "utf-8"


def _decode_output(value: str | bytes | None, encoding: str) -> tuple[str, str]:
    if value is None:
        return "", "none"
    if isinstance(value, str):
        return _normalize_newlines(value), "unknown"
    try:
        decoded = value.decode(encoding, errors="strict")
        replacement = "none"
    except UnicodeDecodeError:
        decoded = value.decode(encoding, errors="replace")
        replacement = "present"
    return _normalize_newlines(decoded), replacement


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _require_store_identity(run: PreparedRun) -> None:
    if not verify_prepared_run(run):
        raise StoreSafetyError("changed")
