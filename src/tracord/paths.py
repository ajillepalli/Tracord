"""Path safety helpers for trace artifacts and bundles."""

from __future__ import annotations

import stat
from os import stat_result
from pathlib import Path, PurePosixPath, PureWindowsPath


_NAME_SURROGATE_BIT = 0x20000000
_LINK_REPARSE_TAGS = {
    0xA0000003,  # IO_REPARSE_TAG_MOUNT_POINT
    0xA000000C,  # IO_REPARSE_TAG_SYMLINK
    0x8000001B,  # IO_REPARSE_TAG_APPEXECLINK
}


def validate_relative_path(value: str) -> list[str]:
    errors: list[str] = []
    if not value:
        return ["path must not be empty"]

    raw_parts = value.split("/")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute():
        errors.append("path must be relative")
    if windows.drive:
        errors.append("path must not include a drive")
    if any(part in ("", ".", "..") for part in raw_parts):
        errors.append("path must not contain empty, current, or parent segments")
    if "\\" in value:
        errors.append("path must use forward slashes")
    if "\x00" in value:
        errors.append("path must not contain NUL")
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
        raise ValueError("path escapes root") from exc
    return target_path


def is_link_or_junction(path: Path, info: stat_result) -> bool:
    """Reject symlinks and Windows reparse points, including 3.11 junctions."""
    reparse_tag = getattr(info, "st_reparse_tag", 0) or 0
    is_junction = getattr(path, "is_junction", None)
    try:
        junction = callable(is_junction) and is_junction()
    except OSError:
        junction = True
    name_surrogate = bool(reparse_tag & _NAME_SURROGATE_BIT)
    return (
        stat.S_ISLNK(info.st_mode)
        or name_surrogate
        or reparse_tag in _LINK_REPARSE_TAGS
        or junction
    )
