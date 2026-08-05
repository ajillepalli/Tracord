"""Filesystem storage for Tracord run records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_HOME = ".tracord"
RUNS_DIR = "runs"


def ensure_store(root: Path) -> Path:
    runs_dir = root / RUNS_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir


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
