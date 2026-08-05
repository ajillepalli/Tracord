"""Stable, privacy-safe result construction and JSON emission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import BinaryIO


class CIOutputError(ValueError):
    """A path-free result construction error."""


def project_full_run(trace: Mapping[str, object]) -> dict[str, object]:
    """Project a published trace into the frozen full-run result shape."""
    raise NotImplementedError


def project_list_run(trace: Mapping[str, object]) -> dict[str, object]:
    """Project a validated trace into the frozen list-run result shape."""
    raise NotImplementedError


def build_record_result(
    *, exit_code: int, run: Mapping[str, object] | None, error_code: str | None = None
) -> dict[str, object]:
    """Build one record-result envelope."""
    raise NotImplementedError


def build_replay_result(
    *, exit_code: int, run: Mapping[str, object] | None, error_code: str | None = None
) -> dict[str, object]:
    """Build one replay-result envelope."""
    raise NotImplementedError


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
    raise NotImplementedError


def build_list_result(
    *,
    exit_code: int,
    runs: Sequence[Mapping[str, object]],
    skipped: int,
    truncated: bool,
    error_code: str | None = None,
) -> dict[str, object]:
    """Build one list-result envelope."""
    raise NotImplementedError


def serialize_json(payload: Mapping[str, object]) -> bytes:
    """Serialize one deterministic JSON object and trailing LF."""
    raise NotImplementedError


def write_json_stdout(payload: Mapping[str, object]) -> None:
    """Write deterministic JSON through the current stdout object."""
    raise NotImplementedError


class JsonEmitter:
    """Attempt at most one guarded result emission."""

    def __init__(self, *, stream: object | None = None) -> None:
        self.stream = stream
        self.emission_started = False
        self.emitted = False

    def emit(self, payload: Mapping[str, object]) -> bool:
        """Return true only after a complete flush."""
        raise NotImplementedError


def _binary_stream(stream: object) -> BinaryIO | None:
    """Return a stdout binary stream when one is exposed."""
    raise NotImplementedError

