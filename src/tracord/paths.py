"""Path safety helpers for trace artifacts and bundles."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


def validate_relative_path(value: str) -> list[str]:
    errors: list[str] = []
    if not value:
        return ["path must not be empty"]

    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute():
        errors.append(f"path must be relative: {value}")
    if windows.drive:
        errors.append(f"path must not include a drive: {value}")
    if any(part in ("", ".", "..") for part in posix.parts):
        errors.append(f"path must not contain empty, current, or parent segments: {value}")
    if "\\" in value:
        errors.append(f"path must use forward slashes: {value}")
    return errors


def safe_join(root: Path, relative: str) -> Path:
    errors = validate_relative_path(relative)
    if errors:
        raise ValueError("; ".join(errors))

    root_resolved = root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes root: {relative}") from exc
    return target
