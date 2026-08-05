"""Exact, bounded, identity-safe access to recorded command traces."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .bundle import TRACE_FILE, validate_run_id
from .paths import (
    IdentityComparison,
    SafePathError,
    combine_identity,
    compare_snapshot,
    containment_issue,
    is_link_or_junction,
    open_prepared_file,
    prepare_regular_file,
    verify_opened_file,
)
from .schema import validate_trace
from .storage import PreparedStore, StoreSafetyError, prepare_store_for_read


MAX_TRACE_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 128


class TraceAccessError(ValueError):
    """A fixed, path-free trace access classification."""

    def __init__(self, code: str, *, attempted_bytes: int = 0) -> None:
        self.code = code
        self.attempted_bytes = attempted_bytes
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SafeRunDirectory:
    """An exact run directory plus the identity evidence used to select it."""

    store: PreparedStore
    path: Path
    initial: os.stat_result


@dataclass(frozen=True, slots=True)
class LoadedTrace:
    """A validated trace and its still-verifiable run directory."""

    run: SafeRunDirectory
    trace: dict[str, object]
    attempted_bytes: int


def resolve_run_directory(root: Path, run_id: str) -> SafeRunDirectory:
    """Resolve one portable run ID using exact spelling and stable identities."""
    if not isinstance(run_id, str):
        raise TraceAccessError("invalid_run_id")
    try:
        validate_run_id(run_id)
    except ValueError:
        raise TraceAccessError("invalid_run_id") from None

    try:
        store = prepare_store_for_read(root)
    except StoreSafetyError as exc:
        raise TraceAccessError(_store_error_code(exc.reason)) from None
    if store is None:
        raise TraceAccessError("run_not_found")

    try:
        with os.scandir(store.runs) as entries:
            names = [entry.name for entry in entries]
    except OSError:
        raise TraceAccessError("run_not_found") from None

    exact = next((name for name in names if name == run_id), None)
    aliases = [name for name in names if name.casefold() == run_id.casefold()]
    if exact is None:
        if aliases:
            raise TraceAccessError("run_identity_mismatch")
        raise TraceAccessError("run_not_found")
    if aliases != [exact]:
        raise TraceAccessError("run_identity_mismatch")
    return resolve_store_candidate(store, exact)


def resolve_store_candidate(store: PreparedStore, run_id: str) -> SafeRunDirectory:
    """Resolve an already-enumerated exact candidate beneath a prepared store."""
    path = store.runs / run_id
    if containment_issue(store.runs, path) is not None:
        raise TraceAccessError("run_identity_mismatch")
    try:
        initial = path.lstat()
    except FileNotFoundError:
        raise TraceAccessError("run_not_found") from None
    except OSError:
        raise TraceAccessError("run_identity_mismatch") from None
    if is_link_or_junction(path, initial) or not stat.S_ISDIR(initial.st_mode):
        raise TraceAccessError("run_identity_mismatch")

    try:
        checked = path.lstat()
    except OSError:
        raise TraceAccessError("run_identity_mismatch") from None
    identity = compare_snapshot(initial, checked)
    if identity is IdentityComparison.DIFFERENT:
        raise TraceAccessError("run_identity_mismatch")
    if identity is IdentityComparison.UNAVAILABLE:
        raise TraceAccessError("run_identity_unverifiable")
    run = SafeRunDirectory(store=store, path=path, initial=initial)
    verify_run_directory(run)
    return run


def verify_run_directory(run: SafeRunDirectory) -> None:
    """Fail closed when the store or selected run changed after resolution."""
    try:
        root_final = run.store.root.lstat()
        runs_final = run.store.runs.lstat()
        run_final = run.path.lstat()
    except OSError:
        raise TraceAccessError("run_identity_mismatch") from None
    if (
        is_link_or_junction(run.store.root, root_final)
        or not stat.S_ISDIR(root_final.st_mode)
        or is_link_or_junction(run.store.runs, runs_final)
        or not stat.S_ISDIR(runs_final.st_mode)
        or is_link_or_junction(run.path, run_final)
        or not stat.S_ISDIR(run_final.st_mode)
        or containment_issue(run.store.root, run.store.runs) is not None
        or containment_issue(run.store.runs, run.path) is not None
    ):
        raise TraceAccessError("run_identity_mismatch")
    identity = combine_identity(
        compare_snapshot(run.store.root_snapshot, root_final),
        compare_snapshot(run.store.runs_snapshot, runs_final),
        compare_snapshot(run.initial, run_final),
    )
    if identity is IdentityComparison.DIFFERENT:
        raise TraceAccessError("run_identity_mismatch")
    if identity is IdentityComparison.UNAVAILABLE:
        raise TraceAccessError("run_identity_unverifiable")


def read_trace(
    run: SafeRunDirectory,
    *,
    max_bytes: int = MAX_TRACE_BYTES,
    byte_budget: int | None = None,
) -> LoadedTrace:
    """Read and validate one trace without exceeding either supplied bound."""
    try:
        prepared = prepare_regular_file(run.path, TRACE_FILE, require_single_link=True)
    except SafePathError as exc:
        if exc.reason == "missing":
            raise TraceAccessError("trace_missing") from None
        raise TraceAccessError("trace_unreadable") from None

    declared_size = prepared.initial.st_size
    if declared_size > max_bytes:
        raise TraceAccessError("trace_too_large")
    if byte_budget is not None and declared_size > byte_budget:
        raise TraceAccessError("trace_budget_exceeded")

    attempted = 0
    probe_allowed = byte_budget is None or declared_size < byte_budget
    try:
        opened_file = open_prepared_file(prepared)
    except SafePathError as exc:
        if exc.reason == "changed":
            raise TraceAccessError("run_identity_mismatch") from None
        raise TraceAccessError("trace_unreadable") from None
    try:
        with opened_file as opened:
            attempted = declared_size
            data, short_read = _read_exact(opened.stream, declared_size)
            growth = b""
            if not short_read and probe_allowed:
                growth = opened.stream.read(1)
                attempted += 1
            identity = verify_opened_file(opened)
    except SafePathError as exc:
        if exc.reason == "changed":
            raise TraceAccessError(
                "run_identity_mismatch", attempted_bytes=attempted
            ) from None
        raise TraceAccessError("trace_unreadable", attempted_bytes=attempted) from None
    except OSError:
        raise TraceAccessError("trace_unreadable", attempted_bytes=attempted) from None
    if short_read or growth:
        raise TraceAccessError("trace_unreadable", attempted_bytes=attempted)
    if identity is IdentityComparison.UNAVAILABLE:
        raise TraceAccessError(
            "run_identity_unverifiable", attempted_bytes=attempted
        )

    try:
        trace = parse_trace(data)
    except TraceAccessError as exc:
        raise TraceAccessError(exc.code, attempted_bytes=attempted) from None
    if trace.get("run_id") != run.path.name:
        raise TraceAccessError("run_identity_mismatch", attempted_bytes=attempted)
    try:
        verify_run_directory(run)
    except TraceAccessError as exc:
        raise TraceAccessError(exc.code, attempted_bytes=attempted) from None
    return LoadedTrace(run=run, trace=trace, attempted_bytes=attempted)


def load_trace(
    root: Path,
    run_id: str,
    *,
    max_bytes: int = MAX_TRACE_BYTES,
) -> LoadedTrace:
    """Resolve, read, validate, and re-verify one exact trace."""
    return read_trace(resolve_run_directory(root, run_id), max_bytes=max_bytes)


def parse_trace(data: bytes) -> dict[str, object]:
    """Parse strict UTF-8 JSON and enforce the canonical trace schema."""
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
            parse_int=_bounded_json_integer,
        )
    except (RecursionError, UnicodeDecodeError, ValueError):
        raise TraceAccessError("trace_invalid") from None
    if not isinstance(value, dict) or validate_trace(value):
        raise TraceAccessError("trace_invalid")
    return value


def _store_error_code(reason: str) -> str:
    if reason == "identity_unverifiable":
        return "run_identity_unverifiable"
    return "run_identity_mismatch"


def _reject_json_constant(_: str) -> float:
    raise ValueError("nonfinite JSON number")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def _bounded_json_integer(value: str) -> int:
    if len(value.removeprefix("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("oversized JSON integer")
    return int(value)


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_exact(stream: object, size: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    bytes_read = 0
    while bytes_read < size:
        requested = min(READ_CHUNK_BYTES, size - bytes_read)
        chunk = stream.read(requested)
        if not chunk:
            return b"".join(chunks), True
        if len(chunk) > requested:
            return b"".join((*chunks, chunk[:requested])), True
        chunks.append(chunk)
        bytes_read += len(chunk)
    return b"".join(chunks), False
