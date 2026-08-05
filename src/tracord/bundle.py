"""Portable trace bundle import and export."""

from __future__ import annotations

import json
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import is_link_or_junction, safe_join, validate_relative_path
from .schema import validate_trace
from .storage import ensure_store, read_json, run_dir, write_json


BUNDLE_VERSION = "tracord.bundle.v0"
TRACE_FILE = "trace.json"
MANIFEST_FILE = "manifest.json"


def export_run(
    *,
    root: Path,
    run_id: str,
    output_path: Path | None = None,
    overwrite: bool = False,
) -> Path:
    _validate_run_id(run_id)
    source_dir = run_dir(root, run_id)
    _validate_run_directory(source_dir)
    trace_path = source_dir / TRACE_FILE
    _validate_export_file(source_dir, TRACE_FILE)

    trace = read_json(trace_path)
    errors = validate_trace(trace)
    if errors:
        raise ValueError("trace is invalid: " + "; ".join(errors))
    if trace.get("run_id") != run_id:
        raise ValueError("trace run id does not match requested run id")

    artifacts = _artifact_names(trace)
    files = [TRACE_FILE, *artifacts]
    for file_name in files:
        _validate_export_file(source_dir, file_name)

    if output_path is None:
        output_path = Path(f"{run_id}.tracord.zip")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"bundle already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "run_id": run_id,
        "schema_version": trace.get("schema_version"),
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "files": files,
    }

    with zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_FILE, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        for file_name in files:
            archive.write(safe_join(source_dir, file_name), file_name)

    return output_path


def import_bundle(
    *,
    root: Path,
    bundle_path: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    if not bundle_path.exists():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")

    ensure_store(root)
    with zipfile.ZipFile(bundle_path) as archive:
        member_names = archive.namelist()
        _validate_members(member_names)
        if TRACE_FILE not in member_names:
            raise ValueError("bundle is missing trace.json")

        trace = json.loads(archive.read(TRACE_FILE).decode("utf-8"))
        errors = validate_trace(trace)
        if errors:
            raise ValueError("trace is invalid: " + "; ".join(errors))

        expected_files = {TRACE_FILE, *_artifact_names(trace)}
        if not expected_files.issubset(set(member_names)):
            missing = sorted(expected_files.difference(member_names))
            raise ValueError("bundle is missing expected files: " + ", ".join(missing))

        run_id = str(trace["run_id"])
        _validate_run_id(run_id)
        target_dir = run_dir(root, run_id)
        if target_dir.exists() and not overwrite:
            raise FileExistsError(f"run already exists: {run_id}")
        if target_dir.exists():
            _remove_stale_artifacts(target_dir, expected_files, member_names)
        target_dir.mkdir(parents=True, exist_ok=True)

        for file_name in sorted(expected_files):
            target = safe_join(target_dir, file_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(file_name))

        if MANIFEST_FILE in member_names:
            write_json(target_dir / "bundle-manifest.json", json.loads(archive.read(MANIFEST_FILE)))

    return trace


def _validate_run_id(run_id: str) -> None:
    errors = validate_relative_path(run_id)
    if errors or "/" in run_id:
        raise ValueError("invalid run id: " + "; ".join(errors or ["must be one path segment"]))


def _validate_run_directory(source_dir: Path) -> None:
    try:
        runs_info = source_dir.parent.lstat()
        info = source_dir.lstat()
    except FileNotFoundError:
        raise FileNotFoundError("run not found") from None
    if (
        is_link_or_junction(source_dir.parent, runs_info)
        or not stat.S_ISDIR(runs_info.st_mode)
        or is_link_or_junction(source_dir, info)
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise ValueError("run directory must be a real directory")


def _validate_export_file(source_dir: Path, relative_path: str) -> None:
    candidate = safe_join(source_dir, relative_path)
    current = source_dir
    for part in PurePosixPath(relative_path).parts[:-1]:
        current /= part
        try:
            parent_info = current.lstat()
        except FileNotFoundError:
            raise FileNotFoundError("run artifact not found") from None
        if is_link_or_junction(current, parent_info) or not stat.S_ISDIR(
            parent_info.st_mode
        ):
            raise ValueError("run artifact parent must be a real directory")
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        raise FileNotFoundError("run artifact not found") from None
    if is_link_or_junction(candidate, info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("run artifact must be a regular file")
    if info.st_ino == 0:
        raise ValueError("run artifact identity is unavailable")


def _remove_stale_artifacts(
    target_dir: Path,
    expected_files: set[str],
    member_names: list[str],
) -> None:
    existing_trace_path = target_dir / TRACE_FILE
    if existing_trace_path.exists():
        try:
            existing_trace = read_json(existing_trace_path)
            stale_artifacts = set(_artifact_names(existing_trace)).difference(expected_files)
        except (OSError, ValueError):
            stale_artifacts = set()
        for file_name in stale_artifacts:
            safe_join(target_dir, file_name).unlink(missing_ok=True)
    if MANIFEST_FILE not in member_names:
        (target_dir / "bundle-manifest.json").unlink(missing_ok=True)

def _artifact_names(trace: dict[str, Any]) -> list[str]:
    artifacts = trace.get("artifacts")
    if not isinstance(artifacts, dict):
        return []

    names: list[str] = []
    for value in artifacts.values():
        if not isinstance(value, str):
            continue
        errors = validate_relative_path(value)
        if errors:
            raise ValueError("invalid artifact path: " + "; ".join(errors))
        if value != TRACE_FILE and value not in names:
            names.append(value)
    return names


def _validate_members(names: list[str]) -> None:
    for name in names:
        errors = validate_relative_path(name)
        if errors:
            raise ValueError("unsafe bundle member: " + "; ".join(errors))
