"""Bounded, identity-safe run listing shared by text and JSON output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_LIST_RUNS = 1_000
MAX_LIST_CANDIDATES = 10_000
MAX_LIST_TRACE_BYTES = 16 * 1024 * 1024
MAX_LIST_AGGREGATE_BYTES = 64 * 1024 * 1024


class RunListingError(ValueError):
    """A fixed-code, path-free store listing error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RunListing:
    runs: tuple[dict[str, object], ...]
    skipped: int
    truncated: bool


def scan_runs(root: Path) -> RunListing:
    """Return one bounded safe snapshot-like listing observation."""
    raise NotImplementedError

