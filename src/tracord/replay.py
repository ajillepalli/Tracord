"""Replay previously recorded command traces."""

from __future__ import annotations

from pathlib import Path

from .git_capture import DEFAULT_GIT_TIMEOUT_SECONDS, DEFAULT_MAX_DIFF_BYTES
from .recorder import RecordError, record_command
from .trace_access import TraceAccessError, load_trace


class ReplayError(ValueError):
    """A fixed-code, path-free replay failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def replay_run(
    *,
    root: Path,
    run_id: str,
    name: str | None = None,
    timeout_seconds: float | None = None,
    redact: bool = True,
    capture_diff: bool = False,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    try:
        trace = load_trace(root, run_id).trace
    except TraceAccessError as exc:
        raise ReplayError(_replay_access_code(exc.code)) from None

    command = trace.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ReplayError("replay_trace_invalid")

    replay_name = name if name is not None else f"replay of {run_id}"
    timeout = timeout_seconds if timeout_seconds is not None else _timeout(trace.get("timeout_seconds"))
    try:
        return record_command(
            command,
            root=root,
            name=replay_name,
            timeout_seconds=timeout,
            redact=redact,
            capture_diff=capture_diff,
            max_diff_bytes=max_diff_bytes,
            git_timeout_seconds=git_timeout_seconds,
        )
    except RecordError as exc:
        mapped = {
            "record_store_unwritable": "replay_store_unwritable",
            "record_spawn_failed": "replay_spawn_failed",
        }.get(exc.code, "replay_failed")
        raise ReplayError(mapped) from None


def _timeout(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _replay_access_code(code: str) -> str:
    return {
        "invalid_run_id": "invalid_run_id",
        "run_not_found": "replay_run_not_found",
        "trace_missing": "replay_trace_missing",
        "trace_unreadable": "replay_trace_unreadable",
        "trace_too_large": "replay_trace_unreadable",
        "trace_invalid": "replay_trace_invalid",
        "run_identity_mismatch": "replay_run_identity_mismatch",
        "run_identity_unverifiable": "replay_run_identity_unverifiable",
    }.get(code, "replay_failed")
