"""Deterministic trace assertions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import validate_trace


@dataclass(frozen=True)
class TraceExpectations:
    status: str | None = None
    exit_code: int | None = None
    stdout_contains: str | None = None
    stderr_contains: str | None = None
    max_duration_ms: int | None = None
    no_timeout: bool = False


def evaluate_trace(
    trace: Mapping[str, Any],
    *,
    trace_dir: Path,
    expectations: TraceExpectations,
) -> list[str]:
    failures = validate_trace(trace)

    if expectations.status is not None and trace.get("status") != expectations.status:
        failures.append(f"expected status {expectations.status}, got {trace.get('status')}")

    if expectations.exit_code is not None and trace.get("exit_code") != expectations.exit_code:
        failures.append(f"expected exit_code {expectations.exit_code}, got {trace.get('exit_code')}")

    if expectations.no_timeout and trace.get("timed_out") is not False:
        failures.append("expected timed_out to be false")

    duration_ms = trace.get("duration_ms")
    if (
        expectations.max_duration_ms is not None
        and isinstance(duration_ms, int)
        and duration_ms > expectations.max_duration_ms
    ):
        failures.append(
            f"expected duration_ms <= {expectations.max_duration_ms}, got {duration_ms}"
        )

    artifacts = trace.get("artifacts")
    if isinstance(artifacts, Mapping):
        failures.extend(
            _contains_failure(
                artifacts.get("stdout"),
                trace_dir=trace_dir,
                label="stdout",
                expected=expectations.stdout_contains,
            )
        )
        failures.extend(
            _contains_failure(
                artifacts.get("stderr"),
                trace_dir=trace_dir,
                label="stderr",
                expected=expectations.stderr_contains,
            )
        )

    return failures


def _contains_failure(
    artifact_name: object,
    *,
    trace_dir: Path,
    label: str,
    expected: str | None,
) -> list[str]:
    if expected is None:
        return []
    if not isinstance(artifact_name, str) or not artifact_name:
        return [f"cannot check {label}: missing artifact path"]

    artifact_path = trace_dir / artifact_name
    try:
        content = artifact_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read {label} artifact: {exc}"]

    if expected not in content:
        return [f"expected {label} to contain {expected!r}"]
    return []
