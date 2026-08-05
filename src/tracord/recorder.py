"""Command recording primitives."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .git_capture import DEFAULT_GIT_TIMEOUT_SECONDS, DEFAULT_MAX_DIFF_BYTES, GitDiffCapture
from .redaction import redact_text
from .schema import SCHEMA_VERSION
from .storage import ensure_store, run_dir, write_json


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
        raise ValueError("command must not be empty")

    ensure_store(root)
    run_id = new_run_id()
    output_dir = run_dir(root, run_id)
    output_dir.mkdir(parents=True, exist_ok=False)

    working_directory = Path.cwd()
    cwd = str(working_directory)
    diff_capture: GitDiffCapture | None = None
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
            cwd=Path.cwd(),
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = _decode_timeout_output(exc.stdout)
        stderr = _decode_timeout_output(exc.stderr)
    except OSError:
        if diff_capture is not None:
            diff_capture.close()
        raise

    finished_at = utc_now()
    duration_ms = int((time.monotonic() - started) * 1000)

    stored_stdout = redact_text(stdout) if redact else stdout
    stored_stderr = redact_text(stderr) if redact else stderr
    (output_dir / STDOUT_ARTIFACT).write_bytes(stored_stdout.encode("utf-8"))
    (output_dir / STDERR_ARTIFACT).write_bytes(stored_stderr.encode("utf-8"))

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
        "artifacts": artifacts,
        "events": events,
    }
    if file_changes is not None:
        trace["file_changes"] = file_changes
    write_json(output_dir / "trace.json", trace)
    return trace


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
