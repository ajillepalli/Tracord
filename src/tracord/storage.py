"""Filesystem storage for Tracord run records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from os import stat_result
from pathlib import Path
from typing import Any


DEFAULT_HOME = ".tracord"
RUNS_DIR = "runs"


@dataclass(frozen=True, slots=True)
class PreparedStore:
    """Validated store directories and their initial snapshots."""

    root: Path
    runs: Path
    root_snapshot: stat_result
    runs_snapshot: stat_result
    identity_verified: bool


class StoreSafetyError(ValueError):
    """A path-free safe-store preparation or verification failure."""


def ensure_store(root: Path) -> Path:
    runs_dir = root / RUNS_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir


def prepare_store_for_write(root: Path) -> PreparedStore:
    """Create if needed, then validate a store before publication."""
    raise NotImplementedError


def prepare_store_for_read(root: Path) -> PreparedStore | None:
    """Validate an existing store, returning none when it is absent."""
    raise NotImplementedError


def verify_prepared_store(store: PreparedStore) -> bool:
    """Return whether the prepared store still matches its snapshots."""
    raise NotImplementedError


def run_dir(root: Path, run_id: str) -> Path:
    return root / RUNS_DIR / run_id


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs(root: Path) -> list[dict[str, Any]]:
    runs_dir = ensure_store(root)
    traces: list[dict[str, Any]] = []
    for trace_path in sorted(runs_dir.glob("*/trace.json"), reverse=True):
        try:
            traces.append(read_json(trace_path))
        except (OSError, json.JSONDecodeError):
            continue
    return traces
