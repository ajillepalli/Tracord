"""Filesystem storage for Tracord run records."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from os import stat_result
from pathlib import Path
from typing import Any

from .paths import IdentityComparison, compare_identity, containment_issue, is_link_or_junction


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

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def ensure_store(root: Path) -> Path:
    return prepare_store_for_write(root).runs


def prepare_store_for_write(root: Path) -> PreparedStore:
    """Create if needed, then validate a store before publication."""
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise StoreSafetyError("create_failed") from None

    root_snapshot = _directory_snapshot(root)
    runs = root / RUNS_DIR
    try:
        runs.mkdir(exist_ok=True)
    except OSError:
        raise StoreSafetyError("create_failed") from None

    runs_snapshot = _directory_snapshot(runs)
    root_final = _directory_snapshot(root)
    root_identity = compare_identity(root_snapshot, root_final)
    if root_identity is IdentityComparison.DIFFERENT:
        raise StoreSafetyError("changed")
    if containment_issue(root, runs) is not None:
        raise StoreSafetyError("redirected")

    return PreparedStore(
        root=root,
        runs=runs,
        root_snapshot=root_final,
        runs_snapshot=runs_snapshot,
        identity_verified=(
            root_identity is IdentityComparison.VERIFIED
            and _identity_available(runs_snapshot)
        ),
    )


def prepare_store_for_read(root: Path) -> PreparedStore | None:
    """Validate an existing store, returning none when it is absent."""
    root = Path(root)
    try:
        root_snapshot = root.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StoreSafetyError("stat_failed") from None
    _validate_directory(root, root_snapshot)

    root_final = _directory_snapshot(root)
    root_identity = compare_identity(root_snapshot, root_final)
    if root_identity is IdentityComparison.DIFFERENT:
        raise StoreSafetyError("changed")
    if root_identity is IdentityComparison.UNAVAILABLE:
        raise StoreSafetyError("identity_unverifiable")

    runs = root / RUNS_DIR
    try:
        runs_snapshot = runs.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StoreSafetyError("stat_failed") from None
    _validate_directory(runs, runs_snapshot)
    if containment_issue(root, runs) is not None:
        raise StoreSafetyError("redirected")

    root_final = _directory_snapshot(root)
    runs_final = _directory_snapshot(runs)
    root_identity = compare_identity(root_snapshot, root_final)
    runs_identity = compare_identity(runs_snapshot, runs_final)
    if IdentityComparison.DIFFERENT in {root_identity, runs_identity}:
        raise StoreSafetyError("changed")
    if IdentityComparison.UNAVAILABLE in {root_identity, runs_identity}:
        raise StoreSafetyError("identity_unverifiable")

    return PreparedStore(
        root=root,
        runs=runs,
        root_snapshot=root_final,
        runs_snapshot=runs_final,
        identity_verified=True,
    )


def verify_prepared_store(store: PreparedStore) -> bool:
    """Return whether the prepared store still matches its snapshots."""
    try:
        root_snapshot = _directory_snapshot(store.root)
        runs_snapshot = _directory_snapshot(store.runs)
    except StoreSafetyError:
        return False
    if containment_issue(store.root, store.runs) is not None:
        return False

    root_identity = compare_identity(store.root_snapshot, root_snapshot)
    runs_identity = compare_identity(store.runs_snapshot, runs_snapshot)
    if IdentityComparison.DIFFERENT in {root_identity, runs_identity}:
        return False
    if store.identity_verified and (
        root_identity is not IdentityComparison.VERIFIED
        or runs_identity is not IdentityComparison.VERIFIED
    ):
        return False
    return True


def _directory_snapshot(path: Path) -> stat_result:
    try:
        snapshot = path.lstat()
    except OSError:
        raise StoreSafetyError("stat_failed") from None
    _validate_directory(path, snapshot)
    return snapshot


def _validate_directory(path: Path, snapshot: stat_result) -> None:
    if is_link_or_junction(path, snapshot):
        raise StoreSafetyError("redirected")
    if not stat.S_ISDIR(snapshot.st_mode):
        raise StoreSafetyError("not_directory")


def _identity_available(snapshot: stat_result) -> bool:
    return snapshot.st_ino != 0


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
