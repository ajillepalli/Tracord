"""Portable trace bundle import and export."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import lzma
import os
import shutil
import stat
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .paths import is_link_or_junction, safe_join, validate_relative_path
from .schema import validate_trace
from .storage import RUNS_DIR, run_dir


BUNDLE_VERSION = "tracord.bundle.v0"
TRACE_FILE = "trace.json"
MANIFEST_FILE = "manifest.json"
BUNDLE_MANIFEST_FILE = "bundle-manifest.json"
MAX_BUNDLE_MEMBERS = 4096
MAX_BUNDLE_METADATA_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_PORTABLE_COMPONENT_LENGTH = 128
_COPY_CHUNK_BYTES = 1024 * 1024
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 4
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"|?*')


def export_run(
    *,
    root: Path,
    run_id: str,
    output_path: Path | None = None,
    overwrite: bool = False,
) -> Path:
    validate_run_id(run_id)
    source_dir = run_dir(root, run_id)
    _validate_run_directory(source_dir)
    with _open_export_file(source_dir, TRACE_FILE) as trace_stream:
        trace_bytes = _read_bounded(trace_stream, MAX_BUNDLE_METADATA_BYTES)
    try:
        trace = json.loads(trace_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("trace is not valid JSON") from None
    if not isinstance(trace, dict):
        raise ValueError("trace must be an object")
    errors = validate_trace(trace)
    if errors:
        raise ValueError("trace is invalid: " + "; ".join(errors))
    if trace.get("run_id") != run_id:
        raise ValueError("trace run id does not match requested run id")

    artifacts = artifact_names(trace)
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
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(files) + 1 > MAX_BUNDLE_MEMBERS:
        raise ValueError("bundle has too many members")
    if len(manifest_bytes) > MAX_BUNDLE_METADATA_BYTES:
        raise ValueError("bundle manifest exceeds the metadata limit")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".partial", dir=output_path.parent
    )
    temporary_path = Path(temporary_name)
    temporary_snapshot = None
    needs_no_replace_fallback = False
    try:
        bytes_written = len(manifest_bytes) + len(trace_bytes)
        if bytes_written > MAX_BUNDLE_UNCOMPRESSED_BYTES:
            raise ValueError("bundle exceeds the uncompressed size limit")
        with os.fdopen(descriptor, "w+b", closefd=True) as temporary_stream:
            descriptor = -1
            with zipfile.ZipFile(
                temporary_stream, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr(MANIFEST_FILE, manifest_bytes)
                archive.writestr(TRACE_FILE, trace_bytes)
                for file_name in artifacts:
                    with _open_export_file(source_dir, file_name) as source:
                        with archive.open(file_name, "w") as target:
                            bytes_written += _copy_bounded(
                                source,
                                target,
                                MAX_BUNDLE_UNCOMPRESSED_BYTES - bytes_written,
                            )
            temporary_stream.flush()
            os.fsync(temporary_stream.fileno())
            temporary_snapshot = os.fstat(temporary_stream.fileno())
            _verify_temporary_identity(temporary_path, temporary_snapshot)
            if not overwrite:
                try:
                    os.link(temporary_path, output_path, follow_symlinks=False)
                except FileExistsError:
                    raise FileExistsError(f"bundle already exists: {output_path}") from None
                except OSError:
                    needs_no_replace_fallback = True
        if needs_no_replace_fallback:
            if temporary_snapshot is None:
                raise ValueError("temporary bundle was not finalized")
            _verify_temporary_identity(temporary_path, temporary_snapshot)
            _rename_no_replace(temporary_path, output_path, kind="bundle")
        if overwrite:
            if temporary_snapshot is None:
                raise ValueError("temporary bundle was not finalized")
            _verify_temporary_identity(temporary_path, temporary_snapshot)
            os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)

    return output_path


def import_bundle(
    *,
    root: Path,
    bundle_path: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    if not bundle_path.exists():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")

    try:
        with zipfile.ZipFile(bundle_path) as archive:
            members = _validate_members(archive.infolist())
            if TRACE_FILE not in members:
                raise ValueError("bundle is missing trace.json")

            trace_bytes = _read_zip_member(
                archive, members[TRACE_FILE], MAX_BUNDLE_METADATA_BYTES
            )
            trace = json.loads(trace_bytes.decode("utf-8"))
            if not isinstance(trace, dict):
                raise ValueError("trace must be an object")
            errors = validate_trace(trace)
            if errors:
                raise ValueError("trace is invalid: " + "; ".join(errors))

            artifacts = artifact_names(trace)
            expected_files = {TRACE_FILE, *artifacts}
            if not expected_files.issubset(members):
                missing = sorted(expected_files.difference(members))
                raise ValueError("bundle is missing expected files: " + ", ".join(missing))

            manifest_bytes = None
            if MANIFEST_FILE in members:
                if members[MANIFEST_FILE].file_size > MAX_BUNDLE_METADATA_BYTES:
                    raise ValueError("bundle metadata exceeds the size limit")
                manifest_bytes = (
                    json.dumps(
                        build_manifest(
                            run_id=trace["run_id"],
                            trace=trace,
                            files=[TRACE_FILE, *artifacts],
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
                if len(manifest_bytes) > MAX_BUNDLE_METADATA_BYTES:
                    raise ValueError("bundle manifest exceeds the metadata limit")

            run_id = trace["run_id"]
            validate_run_id(run_id)
            _ensure_import_runs(root)
            target_dir = run_dir(root, run_id)
            with _import_run_lock(target_dir):
                _install_import(
                    archive=archive,
                    members=members,
                    trace_bytes=trace_bytes,
                    artifacts=artifacts,
                    manifest_bytes=manifest_bytes,
                    target_dir=target_dir,
                    overwrite=overwrite,
                )
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        lzma.LZMAError,
        EOFError,
        RuntimeError,
        RecursionError,
    ):
        raise ValueError("bundle is not a readable zip archive") from None

    return trace


def _install_import(
    *,
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    trace_bytes: bytes,
    artifacts: list[str],
    manifest_bytes: bytes | None,
    target_dir: Path,
    overwrite: bool,
) -> None:
    _validate_run_alias(target_dir)
    _recover_import_transaction(target_dir)
    target_exists = target_dir.exists() or target_dir.is_symlink()
    if target_exists and not overwrite:
        raise FileExistsError(f"run already exists: {target_dir.name}")
    if target_exists:
        _validate_run_directory(target_dir)
        _validate_existing_import_parents(
            target_dir,
            [TRACE_FILE, *artifacts, BUNDLE_MANIFEST_FILE],
        )

    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=_transaction_prefix(target_dir, "import"),
            dir=target_dir.parent,
        )
    )
    try:
        _write_import_file(staging_dir, TRACE_FILE, trace_bytes)
        for file_name in sorted(artifacts):
            info = members[file_name]
            with archive.open(info) as source:
                _write_import_stream(
                    staging_dir,
                    file_name,
                    source,
                    expected_size=info.file_size,
                )

        if manifest_bytes is not None:
            _write_import_file(staging_dir, BUNDLE_MANIFEST_FILE, manifest_bytes)
        _publish_import(staging_dir, target_dir, overwrite=overwrite)
        staging_dir = None
    finally:
        if staging_dir is not None:
            _remove_tree(staging_dir)


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


def validate_run_id(run_id: str) -> None:
    errors = validate_relative_path(run_id)
    if errors or "/" in run_id:
        raise ValueError("invalid run id: " + "; ".join(errors or ["must be one path segment"]))
    if run_id.casefold().startswith(".tracord-"):
        raise ValueError("invalid run id: reserved for Tracord transaction files")
    try:
        _portable_path_key(run_id)
    except ValueError:
        raise ValueError("invalid run id: must be a portable directory name") from None


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


def _verify_temporary_identity(path: Path, descriptor_info: os.stat_result) -> None:
    try:
        path_info = path.lstat()
    except OSError:
        raise ValueError("temporary bundle changed during export") from None
    if is_link_or_junction(path, path_info) or not _same_export_snapshot(
        path_info, descriptor_info
    ):
        raise ValueError("temporary bundle changed during export")


def _rename_no_replace(source: Path, target: Path, *, kind: str) -> None:
    if os.name == "nt":
        try:
            os.rename(source, target)
        except FileExistsError:
            raise FileExistsError(f"{kind} already exists: {target.name}") from None
        return

    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is not None:
        result = renamex_np(os.fsencode(source), os.fsencode(target), _RENAME_EXCL)
        if result == 0:
            return
        _raise_rename_error(kind)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is not None:
        result = renameatx_np(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(target),
            _RENAME_EXCL,
        )
        if result == 0:
            return
        _raise_rename_error(kind)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ValueError(
            f"filesystem does not support atomic no-overwrite {kind} publication"
        )
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    _raise_rename_error(kind)


def _raise_rename_error(kind: str) -> None:
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(f"{kind} already exists") from None
    if error in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
        raise ValueError(
            f"filesystem does not support atomic no-overwrite {kind} publication"
        ) from None
    raise OSError(error, os.strerror(error))


def _read_bounded(source: BinaryIO, limit: int) -> bytes:
    chunks: list[bytes] = []
    bytes_read = 0
    while True:
        chunk = source.read(min(_COPY_CHUNK_BYTES, limit - bytes_read + 1))
        if not chunk:
            return b"".join(chunks)
        bytes_read += len(chunk)
        if bytes_read > limit:
            raise ValueError("bundle member exceeds its size limit")
        chunks.append(chunk)


def _copy_bounded(source: BinaryIO, target: BinaryIO, limit: int) -> int:
    bytes_written = 0
    while True:
        chunk = source.read(min(_COPY_CHUNK_BYTES, limit - bytes_written + 1))
        if not chunk:
            return bytes_written
        bytes_written += len(chunk)
        if bytes_written > limit:
            raise ValueError("bundle exceeds the uncompressed size limit")
        target.write(chunk)


def _read_zip_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int
) -> bytes:
    if info.file_size > limit:
        raise ValueError("bundle metadata exceeds the size limit")
    with archive.open(info) as source:
        data = _read_bounded(source, limit)
    if len(data) != info.file_size:
        raise ValueError("bundle member size does not match its declaration")
    return data


def _ensure_import_runs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    runs_directory = root / RUNS_DIR
    if not runs_directory.exists() and not runs_directory.is_symlink():
        runs_directory.mkdir()
        return
    info = runs_directory.lstat()
    if is_link_or_junction(runs_directory, info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("runs directory must be a real directory")


def _validate_run_alias(target_dir: Path) -> None:
    target_key = target_dir.name.casefold()
    try:
        aliases = [
            entry.name
            for entry in target_dir.parent.iterdir()
            if entry.name.casefold() == target_key and entry.name != target_dir.name
        ]
    except OSError:
        raise ValueError("runs directory is unreadable") from None
    if aliases:
        raise ValueError("run id collides on case-insensitive filesystems")


@contextmanager
def _import_run_lock(target_dir: Path):
    lock_path = _import_lock_path(target_dir)
    descriptor = _open_import_lock(lock_path)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError:
        if not locked:
            raise ValueError("import already in progress for this run") from None
        raise
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _import_lock_path(target_dir: Path) -> Path:
    return target_dir.parent / f".tracord-{_transaction_key(target_dir)}.lock"


def _open_import_lock(lock_path: Path) -> int:
    binary = getattr(os, "O_BINARY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | binary | no_follow,
            0o600,
        )
        created = True
        initial = os.fstat(descriptor)
    except OSError:
        try:
            initial = lock_path.lstat()
        except OSError:
            raise ValueError("import lock file is unsafe") from None
        if is_link_or_junction(lock_path, initial):
            raise ValueError("import lock file is unsafe")
        try:
            descriptor = os.open(lock_path, os.O_RDWR | binary | no_follow)
        except OSError:
            raise ValueError("import lock file is unsafe") from None

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (not created and opened.st_size not in {0, 1})
            or not _same_export_snapshot(initial, opened)
        ):
            raise ValueError("import lock file is unsafe")
        if not created and opened.st_size == 0:
            raise ValueError("import already in progress for this run")
        if created:
            if os.write(descriptor, b"\0") != 1:
                raise OSError("short import lock write")
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
        final_path = lock_path.lstat()
        if (
            is_link_or_junction(lock_path, final_path)
            or final_path.st_nlink != 1
            or not _same_export_snapshot(opened, final_path)
        ):
            raise ValueError("import lock file is unsafe")
        return descriptor
    except BaseException:
        if created:
            try:
                descriptor_info = os.fstat(descriptor)
                path_info = lock_path.lstat()
                if (
                    not is_link_or_junction(lock_path, path_info)
                    and path_info.st_nlink == 1
                    and _same_export_snapshot(descriptor_info, path_info)
                ):
                    lock_path.unlink()
            except OSError:
                pass
        os.close(descriptor)
        raise


def _import_target(target_dir: Path, relative_path: str) -> Path:
    target = safe_join(target_dir, relative_path)
    current = target_dir
    for part in PurePosixPath(relative_path).parts[:-1]:
        current /= part
        if not current.exists() and not current.is_symlink():
            current.mkdir()
        info = current.lstat()
        if is_link_or_junction(current, info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("import target parent must be a real directory")
    return target


def _validate_existing_import_parents(
    target_dir: Path, relative_paths: list[str]
) -> None:
    for relative_path in relative_paths:
        current = target_dir
        for part in PurePosixPath(relative_path).parts[:-1]:
            current /= part
            if not current.exists() and not current.is_symlink():
                break
            info = current.lstat()
            if is_link_or_junction(current, info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("import target parent must be a real directory")


def _write_import_stream(
    target_dir: Path,
    relative_path: str,
    source: BinaryIO,
    *,
    expected_size: int,
) -> None:
    target = _import_target(target_dir, relative_path)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            copied = _copy_bounded(source, stream, expected_size)
        if copied != expected_size:
            raise ValueError("bundle member size does not match its declaration")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_import_file(target_dir: Path, relative_path: str, data: bytes) -> None:
    _write_import_stream(
        target_dir,
        relative_path,
        BytesIO(data),
        expected_size=len(data),
    )


def _publish_import(staging_dir: Path, target_dir: Path, *, overwrite: bool) -> None:
    if not overwrite:
        _rename_no_replace(staging_dir, target_dir, kind="run")
        return

    target_exists = target_dir.exists() or target_dir.is_symlink()
    if not target_exists:
        _rename_no_replace(staging_dir, target_dir, kind="run")
        return
    _validate_run_directory(target_dir)

    backup_dir = Path(
        tempfile.mkdtemp(
            prefix=_transaction_prefix(target_dir, "backup"), dir=target_dir.parent
        )
    )
    backup_dir.rmdir()
    journal_path = _transaction_journal_path(target_dir)
    _write_import_transaction(
        journal_path, staging_dir, backup_dir, target_dir, phase="prepared"
    )
    try:
        os.rename(target_dir, backup_dir)
        _sync_directory(target_dir.parent)
        _validate_run_directory(backup_dir)
        _write_import_transaction(
            journal_path,
            staging_dir,
            backup_dir,
            target_dir,
            phase="backed_up",
            replace=True,
        )
        os.rename(staging_dir, target_dir)
        _sync_directory(target_dir.parent)
        _write_import_transaction(
            journal_path,
            staging_dir,
            backup_dir,
            target_dir,
            phase="committed",
            replace=True,
        )
    except BaseException:
        _rollback_import_transaction(staging_dir, backup_dir, target_dir)
        journal_path.unlink(missing_ok=True)
        _sync_directory(target_dir.parent)
        raise
    try:
        _remove_tree(backup_dir)
        journal_path.unlink()
        _sync_directory(target_dir.parent)
    except (OSError, ValueError):
        # The committed journal makes cleanup recoverable on the next import.
        pass


def _transaction_journal_path(target_dir: Path) -> Path:
    return target_dir.parent / f".tracord-{_transaction_key(target_dir)}.transaction.json"


def _transaction_key(target_dir: Path) -> str:
    return hashlib.sha256(target_dir.name.casefold().encode("utf-8")).hexdigest()[:32]


def _transaction_prefix(target_dir: Path, kind: str) -> str:
    return f".tracord-{_transaction_key(target_dir)}.{kind}-"


def _write_import_transaction(
    journal_path: Path,
    staging_dir: Path,
    backup_dir: Path,
    target_dir: Path,
    *,
    phase: str = "prepared",
    replace: bool = False,
) -> None:
    payload = json.dumps(
        {
            "backup": backup_dir.name,
            "phase": phase,
            "staging": staging_dir.name,
            "target": target_dir.name,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{journal_path.name}.", suffix=".partial", dir=journal_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            if stream.write(payload) != len(payload):
                raise OSError("short transaction journal write")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary_path, journal_path)
        else:
            _rename_no_replace(
                temporary_path, journal_path, kind="transaction journal"
            )
        _sync_directory(journal_path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_import_transaction(
    staging_dir: Path, backup_dir: Path, target_dir: Path
) -> None:
    backup_exists = backup_dir.exists() or backup_dir.is_symlink()
    target_exists = target_dir.exists() or target_dir.is_symlink()
    if backup_exists and target_exists:
        if staging_dir.exists() or staging_dir.is_symlink():
            _remove_tree(staging_dir)
        os.rename(target_dir, staging_dir)
        os.rename(backup_dir, target_dir)
        _remove_tree(staging_dir)
    elif backup_exists:
        os.rename(backup_dir, target_dir)
    _sync_directory(target_dir.parent)


def _recover_import_transaction(target_dir: Path) -> None:
    journal_path = _transaction_journal_path(target_dir)
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    try:
        journal_info = journal_path.lstat()
        if is_link_or_junction(journal_path, journal_info):
            raise ValueError
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(journal_path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError
            payload = json.loads(_read_bounded(stream, 4096).decode("utf-8"))
        staging_name = payload["staging"]
        backup_name = payload["backup"]
        target_name = payload["target"]
        phase = payload["phase"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("import transaction journal is invalid") from None
    expected_staging_prefix = _transaction_prefix(target_dir, "import")
    expected_backup_prefix = _transaction_prefix(target_dir, "backup")
    if (
        target_name != target_dir.name
        or phase not in {"prepared", "backed_up", "committed"}
        or not isinstance(staging_name, str)
        or not staging_name.startswith(expected_staging_prefix)
        or not isinstance(backup_name, str)
        or not backup_name.startswith(expected_backup_prefix)
        or validate_relative_path(staging_name)
        or validate_relative_path(backup_name)
        or "/" in staging_name
        or "/" in backup_name
    ):
        raise ValueError("import transaction journal is invalid")
    staging_dir = target_dir.parent / staging_name
    backup_dir = target_dir.parent / backup_name
    if phase == "committed" and (target_dir.exists() or target_dir.is_symlink()):
        _validate_run_directory(target_dir)
        if backup_dir.exists() or backup_dir.is_symlink():
            _remove_tree(backup_dir)
        if staging_dir.exists() or staging_dir.is_symlink():
            _remove_tree(staging_dir)
    else:
        _rollback_import_transaction(staging_dir, backup_dir, target_dir)
        if staging_dir.exists() or staging_dir.is_symlink():
            _remove_tree(staging_dir)
    journal_path.unlink()
    _sync_directory(target_dir.parent)


def _remove_tree(path: Path) -> None:
    for attempt in range(2):
        try:
            info = path.lstat()
            if is_link_or_junction(path, info) or not stat.S_ISDIR(info.st_mode):
                path.unlink()
            else:
                shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 1:
                raise ValueError("import transaction cleanup failed") from None

def artifact_names(trace: dict[str, Any]) -> list[str]:
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
        if value not in names:
            names.append(value)
    _validate_artifact_namespace(names)
    return names


def _portable_path_key(value: str) -> str:
    key_parts: list[str] = []
    for part in PurePosixPath(value).parts:
        if len(part.encode("utf-8")) > MAX_PORTABLE_COMPONENT_LENGTH:
            raise ValueError("path segments exceed the portable length limit")
        if part.endswith((".", " ")):
            raise ValueError("path segments must not end with a dot or space")
        if any(character in _WINDOWS_FORBIDDEN_CHARACTERS for character in part):
            raise ValueError("path contains characters unsupported on Windows")
        if any(ord(character) < 32 for character in part):
            raise ValueError("path contains control characters")
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
            raise ValueError("path uses a reserved Windows name")
        key_parts.append(part.casefold())
    return "/".join(key_parts)


def _validate_portable_namespace(names: list[str]) -> None:
    keys: set[str] = set()
    for name in names:
        key = _portable_path_key(name)
        if key in keys:
            raise ValueError("bundle paths collide on case-insensitive filesystems")
        keys.add(key)
    for key in keys:
        parts = key.split("/")
        if any("/".join(parts[:index]) in keys for index in range(1, len(parts))):
            raise ValueError("bundle paths contain a file and parent collision")


def _validate_artifact_namespace(names: list[str]) -> None:
    reserved = [TRACE_FILE, MANIFEST_FILE, BUNDLE_MANIFEST_FILE]
    reserved_keys = {_portable_path_key(name) for name in reserved}
    for name in names:
        if _portable_path_key(name) in reserved_keys:
            raise ValueError("artifact path uses a reserved bundle name")
    _validate_portable_namespace([*reserved, *names])


def _validate_members(infos: list[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
    if len(infos) > MAX_BUNDLE_MEMBERS:
        raise ValueError("bundle has too many members")
    members: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in infos:
        name = info.filename
        errors = validate_relative_path(name)
        if errors:
            raise ValueError("unsafe bundle member: " + "; ".join(errors))
        if name in members:
            raise ValueError("bundle contains duplicate members")
        if info.flag_bits & 0x1:
            raise ValueError("encrypted bundle members are unsupported")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError("bundle member compression is unsupported")
        total_size += info.file_size
        if total_size > MAX_BUNDLE_UNCOMPRESSED_BYTES:
            raise ValueError("bundle exceeds the uncompressed size limit")
        members[name] = info
    if BUNDLE_MANIFEST_FILE in {
        _portable_path_key(name) for name in members
    }:
        raise ValueError("bundle member uses a reserved import name")
    _validate_portable_namespace(list(members))
    return members
