"""Bounded, identity-safe run listing shared by text and JSON output."""

from __future__ import annotations

import heapq
import os
from dataclasses import dataclass
from pathlib import Path

from .bundle import validate_run_id
from .ci_output import CIOutputError, project_list_run
from .result_codes import CI_RUN_ID
from .storage import (
    StoreSafetyError,
    prepare_store_for_read,
    verify_prepared_store,
)
from .trace_access import TraceAccessError, read_trace, resolve_store_candidate


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
    try:
        store = prepare_store_for_read(root)
    except StoreSafetyError:
        raise RunListingError("list_store_unreadable") from None
    if store is None:
        return RunListing(runs=(), skipped=0, truncated=False)

    candidates: list[str] = []
    skipped = 0
    overflow = False
    try:
        with os.scandir(store.runs) as entries:
            for entry in entries:
                name = entry.name
                if not _candidate_id(name):
                    skipped += 1
                    continue
                if len(candidates) < MAX_LIST_CANDIDATES + 1:
                    heapq.heappush(candidates, name)
                    continue
                overflow = True
                if name > candidates[0]:
                    heapq.heapreplace(candidates, name)
    except OSError:
        raise RunListingError("list_store_unreadable") from None

    ordered = sorted(candidates, reverse=True)
    retained = ordered[:MAX_LIST_CANDIDATES]
    truncated = overflow or len(ordered) > MAX_LIST_CANDIDATES
    runs: list[dict[str, object]] = []
    aggregate_attempted = 0

    for index, run_id in enumerate(retained):
        if len(runs) == MAX_LIST_RUNS:
            truncated = True
            break
        try:
            run = resolve_store_candidate(store, run_id)
            loaded = read_trace(
                run,
                max_bytes=MAX_LIST_TRACE_BYTES,
                byte_budget=MAX_LIST_AGGREGATE_BYTES - aggregate_attempted,
            )
        except TraceAccessError as exc:
            aggregate_attempted += exc.attempted_bytes
            if exc.code == "trace_budget_exceeded":
                truncated = True
                break
            skipped += 1
            continue
        aggregate_attempted += loaded.attempted_bytes
        try:
            project_list_run(loaded.trace)
        except CIOutputError:
            skipped += 1
            continue
        name = loaded.trace.get("name")
        if name is not None and not isinstance(name, str):
            skipped += 1
            continue
        runs.append(loaded.trace)
        if len(runs) == MAX_LIST_RUNS and index + 1 < len(retained):
            truncated = True
            break

    if not verify_prepared_store(store):
        raise RunListingError("list_store_unreadable")
    return RunListing(runs=tuple(runs), skipped=skipped, truncated=truncated)


def _candidate_id(value: str) -> bool:
    if CI_RUN_ID.fullmatch(value) is None:
        return False
    try:
        validate_run_id(value)
    except ValueError:
        return False
    return True
