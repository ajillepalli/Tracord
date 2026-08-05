"""Replay previously recorded command traces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .recorder import record_command
from .schema import validate_trace
from .storage import read_json, run_dir


def replay_run(
    *,
    root: Path,
    run_id: str,
    name: str | None = None,
    timeout_seconds: float | None = None,
    redact: bool = True,
) -> dict[str, object]:
    trace_path = run_dir(root, run_id) / "trace.json"
    if not trace_path.exists():
        raise FileNotFoundError(f"run not found: {run_id}")

    trace: dict[str, Any] = read_json(trace_path)
    errors = validate_trace(trace)
    if errors:
        raise ValueError("trace is invalid: " + "; ".join(errors))
    if trace.get("kind") != "command":
        raise ValueError(f"cannot replay trace kind: {trace.get('kind')}")

    command = trace.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("trace command is invalid")

    replay_name = name if name is not None else f"replay of {run_id}"
    timeout = timeout_seconds if timeout_seconds is not None else _timeout(trace.get("timeout_seconds"))
    return record_command(command, root=root, name=replay_name, timeout_seconds=timeout, redact=redact)


def _timeout(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
