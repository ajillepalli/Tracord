"""Portable trace bundle import and export."""

from __future__ import annotations

import json
import os
import shutil
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import is_link_or_junction, safe_join, validate_relative_path
from .schema import validate_trace
from .storage import RUNS_DIR, read_json, run_dir


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
    with _open_export_file(source_dir, TRACE_FILE) as trace_stream:
        trace_bytes = trace_stream.read()
    trace = json.loads(trace_bytes.decode("utf-8"))
    errors = validate_trace(trace)
    if errors:
        raise ValueError("trace is invalid: " + "; ".join(errors))
    if trace.get("run_id") != run_id:
        raise ValueError("trace run id does not match requested run id")

    artifacts = _artifact_names(trace)
    files = [TRACE_FILE, *artifacts]
    if output_path is None:
        output_path = Path(f"{run_id}.tracord.zip")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"bundle already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(
        run_id=run_id,
        trace=trace,
        files=files,
        created_at=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )

    with zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_FILE, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        archive.writestr(TRACE_FILE, trace_bytes)
        for file_name in artifacts:
            with _open_export_file(source_dir, file_name) as source:
                with archive.open(file_name, "w") as target:
                    shutil.copyfileobj(source, target)

    return output_path


def import_bundle(
    *,
    root: Path,
    bundle_path: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    if not bundle_path.exists():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")

    _ensure_import_runs(root)
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

        run_id = trace["run_id"]
        _validate_run_id(run_id)
        target_dir = run_dir(root, run_id)
        target_exists = target_dir.exists() or target_dir.is_symlink()
        if target_exists and not overwrite:
            raise FileExistsError(f"run already exists: {run_id}")
        if target_exists:
            _validate_run_directory(target_dir)
            _remove_stale_artifacts(target_dir, expected_files, member_names)
        else:
            target_dir.mkdir()

        for file_name in sorted(expected_files):
            _write_import_file(target_dir, file_name, archive.read(file_name))

        if MANIFEST_FILE in member_names:
            manifest_bytes = (
                json.dumps(json.loads(archive.read(MANIFEST_FILE)), indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            _write_import_file(target_dir, "bundle-manifest.json", manifest_bytes)

    return trace


def build_manifest(
    *,
    run_id: str,
    trace: dict[str, Any],
    files: list[str],
    created_at: str | None = None,
) -> dict[str, Any]:
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "run_id": run_id,
        "schema_version": trace.get("schema_version"),
        "files": files,
    }
    if created_at is not None:
        manifest["created_at"] = created_at
    return manifest


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
    except OSError:
        raise ValueError("run directory is unreadable") from None
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


def _open_export_file(source_dir: Path, relative_path: str):
    _validate_export_file(source_dir, relative_path)
    candidate = safe_join(source_dir, relative_path)
    initial = candidate.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_export_snapshot(initial, opened):
            raise ValueError("run artifact changed during export")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        return stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _same_export_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    if first.st_ino and second.st_ino:
        identity_matches = first.st_ino == second.st_ino and first.st_dev == second.st_dev
    else:
        identity_matches = True
    return (
        identity_matches
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _ensure_import_runs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    runs_directory = root / RUNS_DIR
    if not runs_directory.exists() and not runs_directory.is_symlink():
        runs_directory.mkdir()
        return
    info = runs_directory.lstat()
    if is_link_or_junction(runs_directory, info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("runs directory must be a real directory")


def _write_import_file(target_dir: Path, relative_path: str, data: bytes) -> None:
    target = safe_join(target_dir, relative_path)
    current = target_dir
    for part in PurePosixPath(relative_path).parts[:-1]:
        current /= part
        if not current.exists() and not current.is_symlink():
            current.mkdir()
        info = current.lstat()
        if is_link_or_junction(current, info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("import target parent must be a real directory")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError:
        raise ValueError("import target must be a regular file") from None
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(data)


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
