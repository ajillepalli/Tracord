"""Path safety helpers for trace artifacts and bundles."""

from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath, PureWindowsPath


def validate_relative_path(value: str) -> list[str]:
    errors: list[str] = []
    if not value:
        return ["path must not be empty"]

    raw_parts = value.split("/")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute():
        errors.append(f"path must be relative: {value}")
    if windows.drive:
        errors.append(f"path must not include a drive: {value}")
    if any(part in ("", ".", "..") for part in raw_parts):
        errors.append(f"path must not contain empty, current, or parent segments: {value}")
    if "\\" in value:
        errors.append(f"path must use forward slashes: {value}")
    return errors


def safe_join(root: Path, relative: str) -> Path:
    errors = validate_relative_path(relative)
    if errors:
        raise ValueError("; ".join(errors))

    root_resolved = root.resolve()
    target_path = root.joinpath(*PurePosixPath(relative).parts)
    target = target_path.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes root: {relative}") from exc
    return target_path


def is_link_or_junction(path: Path, info: object) -> bool:
    """Reject symlinks and Windows reparse points, including 3.11 junctions."""
    mode = getattr(info, "st_mode")
    reparse_tag = getattr(info, "st_reparse_tag", 0)
    is_junction = getattr(path, "is_junction", None)
    try:
        junction = callable(is_junction) and is_junction()
    except OSError:
        junction = True
    return stat.S_ISLNK(mode) or bool(reparse_tag) or junction
