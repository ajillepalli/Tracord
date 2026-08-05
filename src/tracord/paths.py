"""Path safety helpers for trace artifacts and bundles."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum
from os import stat_result
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO


_NAME_SURROGATE_BIT = 0x20000000
_LINK_REPARSE_TAGS = {
    0xA0000003,  # IO_REPARSE_TAG_MOUNT_POINT
    0xA000000C,  # IO_REPARSE_TAG_SYMLINK
    0x8000001B,  # IO_REPARSE_TAG_APPEXECLINK
}


class IdentityComparison(Enum):
    """Result of comparing filesystem identities without guessing."""

    VERIFIED = "verified"
    DIFFERENT = "different"
    UNAVAILABLE = "unavailable"


class SafePathError(ValueError):
    """Neutral filesystem failure for callers to map to public diagnostics."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class PreparedFile:
    """A regular file inspected before opening."""

    root: Path
    relative_path: str
    path: Path
    initial: stat_result
    require_single_link: bool


@dataclass(slots=True)
class OpenedFile:
    """An opened descriptor and the evidence collected at open time."""

    prepared: PreparedFile
    stream: BinaryIO
    opened: stat_result
    identity: IdentityComparison

    def __enter__(self) -> OpenedFile:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stream.close()


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


def containment_issue(root: Path, candidate: Path) -> str | None:
    """Return a neutral reason when candidate does not resolve beneath root."""
    try:
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return "path_escape"
    return None


def parent_component_issue(root: Path, relative_path: str) -> str | None:
    """Check that every existing parent is a real directory."""
    current = root
    for part in PurePosixPath(relative_path).parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return "missing_parent"
        except OSError:
            return "parent_stat_failed"
        if is_link_or_junction(current, info):
            return "symlink_parent"
        if not stat.S_ISDIR(info.st_mode):
            return "parent_not_directory"
    return None


def compare_identity(first: stat_result, second: stat_result) -> IdentityComparison:
    """Compare device/inode identity, preserving unavailable identity evidence."""
    if first.st_ino == 0 or second.st_ino == 0:
        return IdentityComparison.UNAVAILABLE
    if (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino):
        return IdentityComparison.VERIFIED
    return IdentityComparison.DIFFERENT


def compare_snapshot(
    first: stat_result,
    second: stat_result,
    *,
    compare_ctime: bool = True,
) -> IdentityComparison:
    """Compare mutable metadata before identity so known changes always win."""
    if (
        first.st_mode != second.st_mode
        or first.st_size != second.st_size
        or _mtime_ns(first) != _mtime_ns(second)
        or (compare_ctime and _ctime_ns(first) != _ctime_ns(second))
        or first.st_nlink != second.st_nlink
    ):
        return IdentityComparison.DIFFERENT
    return compare_identity(first, second)


def combine_identity(*comparisons: IdentityComparison) -> IdentityComparison:
    """Combine identity evidence with differences taking precedence."""
    if IdentityComparison.DIFFERENT in comparisons:
        return IdentityComparison.DIFFERENT
    if IdentityComparison.UNAVAILABLE in comparisons:
        return IdentityComparison.UNAVAILABLE
    return IdentityComparison.VERIFIED


def prepare_regular_file(
    root: Path,
    relative_path: str,
    *,
    require_single_link: bool = False,
) -> PreparedFile:
    """Perform path and metadata checks that must precede descriptor opening."""
    if validate_relative_path(relative_path):
        raise SafePathError("invalid_relative_path")

    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    containment = containment_issue(root, candidate)
    if containment is not None:
        raise SafePathError(containment)
    parent_issue = parent_component_issue(root, relative_path)
    if parent_issue is not None:
        raise SafePathError(parent_issue)

    try:
        initial = candidate.lstat()
    except FileNotFoundError:
        raise SafePathError("missing") from None
    except OSError:
        raise SafePathError("stat_failed") from None

    if is_link_or_junction(candidate, initial):
        raise SafePathError("symlink")
    if not stat.S_ISREG(initial.st_mode):
        raise SafePathError("not_regular_file")
    if require_single_link and initial.st_nlink != 1:
        raise SafePathError("multiple_links")
    return PreparedFile(root, relative_path, candidate, initial, require_single_link)


def open_prepared_file(prepared: PreparedFile) -> OpenedFile:
    """Open an inspected file and compare the descriptor with pre-open evidence."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(prepared.path, flags)
    except OSError:
        raise SafePathError("open_failed") from None

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SafePathError("changed")
        if prepared.require_single_link and opened.st_nlink != 1:
            raise SafePathError("changed")
        identity = _compare_path_descriptor_snapshot(prepared.initial, opened)
        if identity is IdentityComparison.DIFFERENT:
            raise SafePathError("changed")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        return OpenedFile(prepared, stream, opened, identity)
    except OSError:
        raise SafePathError("open_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def verify_opened_file(opened: OpenedFile) -> IdentityComparison:
    """Re-check an opened file and its path after the caller finishes reading."""
    prepared = opened.prepared
    try:
        final_descriptor = os.fstat(opened.stream.fileno())
    except OSError:
        raise SafePathError("verify_failed") from None

    if not stat.S_ISREG(final_descriptor.st_mode):
        raise SafePathError("changed")
    if prepared.require_single_link and final_descriptor.st_nlink != 1:
        raise SafePathError("changed")
    if containment_issue(prepared.root, prepared.path) is not None:
        raise SafePathError("changed")
    if parent_component_issue(prepared.root, prepared.relative_path) is not None:
        raise SafePathError("changed")

    try:
        final_path = prepared.path.lstat()
    except OSError:
        raise SafePathError("changed") from None
    if is_link_or_junction(prepared.path, final_path):
        raise SafePathError("changed")
    if not stat.S_ISREG(final_path.st_mode):
        raise SafePathError("changed")
    if prepared.require_single_link and final_path.st_nlink != 1:
        raise SafePathError("changed")

    comparison = combine_identity(
        opened.identity,
        compare_snapshot(opened.opened, final_descriptor),
        compare_snapshot(prepared.initial, final_path),
        _compare_path_descriptor_snapshot(final_path, final_descriptor),
    )
    if comparison is IdentityComparison.DIFFERENT:
        raise SafePathError("changed")
    return comparison


def _mtime_ns(info: stat_result) -> int:
    return getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))


def _ctime_ns(info: stat_result) -> int:
    return getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))


def _compare_path_descriptor_snapshot(
    path_info: stat_result, descriptor_info: stat_result
) -> IdentityComparison:
    # Windows path stat exposes creation time while fstat can mirror mtime as ctime.
    return compare_snapshot(
        path_info,
        descriptor_info,
        compare_ctime=os.name != "nt",
    )
