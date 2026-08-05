"""Byte-transparent MCP stdio proxy with bounded tool-call observation."""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import select
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .recorder import STDERR_ARTIFACT, STDOUT_ARTIFACT, new_run_id, utc_now
from .redaction import REDACTION, redact_text, sanitize_label
from .result_codes import MAX_SAFE_JSON_INTEGER
from .schema import MAX_TRACE_NESTING_DEPTH, SCHEMA_VERSION
from .storage import (
    StoreSafetyError,
    prepare_run_for_write,
    publish_prepared_json,
    write_prepared_bytes,
)


OBSERVATION_BYTES = 1024 * 1024
MAX_CAPTURE_VALUE_BYTES = 1024 * 1024
MAX_CAPTURE_TOTAL_BYTES = 8 * 1024 * 1024
MAX_STORED_COMMAND_BYTES = 64 * 1024
MAX_CORRELATION_KEY_BYTES = 4 * 1024
CAPTURE_TRACE_DEPTH = 5
MAX_EVENTS = 10_000
MAX_IN_FLIGHT = 4_096
TRACE_TARGET_BYTES = 15 * 1024 * 1024
CLIENT_EOF_GRACE_SECONDS = 2.0
PROCESS_TERM_GRACE_SECONDS = 1.0
POLL_SECONDS = 0.05
IO_CHUNK_BYTES = 64 * 1024

CaptureMode = Literal["omitted", "redacted", "captured"]
Direction = Literal["client", "server"]


class McpProxyError(ValueError):
    """A fixed-code, path-free proxy failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProxyResult:
    trace: dict[str, object]
    exit_code: int


@dataclass(slots=True)
class _InFlight:
    call_id: str
    started: float


class _DuplicateKey(ValueError):
    pass


class _RedactionCollision(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey
        value[key] = item
    return value


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def _control_free(value: str) -> bool:
    return all(not (ord(ch) <= 0x1F or 0x7F <= ord(ch) <= 0x9F) for ch in value)


def _request_key(value: object) -> tuple[str, str] | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_CORRELATION_KEY_BYTES:
            return None
        return ("string", value)
    if isinstance(value, int):
        encoded = str(value)
        if len(encoded) > MAX_CORRELATION_KEY_BYTES:
            return None
        return ("number", encoded)
    if isinstance(value, float) and math.isfinite(value):
        if value.is_integer():
            return ("number", str(int(value)))
        return ("number", repr(value))
    return None


def _json_safe(value: object) -> bool:
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, bool) or current is None or isinstance(current, str):
            continue
        if isinstance(current, int):
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                return False
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                return False
            continue
        if isinstance(current, dict):
            if depth + CAPTURE_TRACE_DEPTH >= MAX_TRACE_NESTING_DEPTH:
                return False
            if not all(isinstance(key, str) for key in current):
                return False
            pending.extend((item, depth + 1) for item in current.values())
            continue
        if isinstance(current, list):
            if depth + CAPTURE_TRACE_DEPTH >= MAX_TRACE_NESTING_DEPTH:
                return False
            pending.extend((item, depth + 1) for item in current)
            continue
        return False
    return True


def _redact_json(value: object, *, depth: int = 0) -> tuple[object, bool]:
    if depth > MAX_TRACE_NESTING_DEPTH:
        raise ValueError("capture depth")
    if isinstance(value, str):
        changed = redact_text(value)
        return changed, changed != value
    if isinstance(value, list):
        output: list[object] = []
        mutated = False
        for item in value:
            result, changed = _redact_json(item, depth=depth + 1)
            output.append(result)
            mutated = mutated or changed
        return output, mutated
    if isinstance(value, dict):
        output_dict: dict[str, object] = {}
        mutated = False
        for key, item in value.items():
            redacted_key = redact_text(key)
            if redacted_key in output_dict:
                raise _RedactionCollision
            result, changed = _redact_json(item, depth=depth + 1)
            output_dict[redacted_key] = result
            mutated = mutated or changed or redacted_key != key
        return output_dict, mutated
    return value, False


class _Observer:
    def __init__(self, mode: CaptureMode, started_at: str, command: list[str], cwd: str):
        self.mode = mode
        self.lock = threading.Lock()
        self.events: list[dict[str, object]] = [
            {
                "type": "command.started",
                "at": started_at,
                "data": {"command": command, "cwd": cwd, "adapter": "mcp-stdio"},
            }
        ]
        self.in_flight: dict[tuple[str, str, str], _InFlight] = {}
        self.ignored_response_keys: set[tuple[str, str, str]] = set()
        self.next_call = 1
        self.reserved_finishes = 0
        self.capture_bytes = 0
        self.sealed = False
        self.unobserved_messages = 0
        self.unmatched_results = 0
        self.events_dropped = 0
        self.capture_omitted = {
            "policy": 0,
            "budget": 0,
            "unsafe": 0,
            "final_size": 0,
        }
        self.reasons: set[str] = set()

    def mark_unobserved(self, reason: str) -> None:
        with self.lock:
            self._mark_unobserved_locked(reason)

    def _mark_unobserved_locked(self, reason: str) -> None:
        self.unobserved_messages += 1
        self.reasons.add(reason)

    def observe(self, direction: Direction, raw: bytes) -> None:
        try:
            parse_raw = raw[:-1] if raw.endswith(b"\n") else raw
            if parse_raw.endswith(b"\r"):
                parse_raw = parse_raw[:-1]
            if parse_raw.startswith(b"\xef\xbb\xbf"):
                parse_raw = parse_raw[3:]
            text = parse_raw.decode("utf-8", errors="strict")
            message = json.loads(
                text,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
        except (RecursionError, UnicodeDecodeError, ValueError):
            self.mark_unobserved("unparseable")
            return
        if not isinstance(message, dict):
            self.mark_unobserved("unsupported_shape")
            return
        if not _supported_envelope(message):
            self.mark_unobserved("unsupported_envelope")
            return
        if direction == "client":
            self._observe_client(message)
        else:
            self._observe_server(message)

    def _observe_client(self, message: dict[str, object]) -> None:
        method = message.get("method")
        if method == "tools/call":
            self._start_tool_call(message)
        elif method == "notifications/cancelled":
            params = message.get("params")
            if isinstance(params, dict):
                self._cancel_tool_call(params.get("requestId"))

    def _start_tool_call(self, message: dict[str, object]) -> None:
        key = _request_key(message.get("id"))
        params = message.get("params")
        if key is None or not isinstance(params, dict):
            self.mark_unobserved("invalid_tool_call")
            return
        name = params.get("name")
        arguments = params.get("arguments", {})
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 512
            or not _control_free(name)
            or not isinstance(arguments, dict)
        ):
            self.mark_unobserved("invalid_tool_call")
            return
        inflight_key = ("client", key[0], key[1])
        with self.lock:
            if self.sealed:
                self._mark_unobserved_locked("sealed")
                return
            if inflight_key in self.in_flight or inflight_key in self.ignored_response_keys:
                self._mark_unobserved_locked("duplicate_id")
                return
            if (
                len(self.in_flight) >= MAX_IN_FLIGHT
                or len(self.events) + self.reserved_finishes + 3 > MAX_EVENTS
            ):
                self._mark_unobserved_locked("observer_cap")
                if len(self.ignored_response_keys) < MAX_IN_FLIGHT:
                    self.ignored_response_keys.add(inflight_key)
                return
            call_id = f"call-{self.next_call}"
            self.next_call += 1
            capture = self._capture_locked(arguments, input_value=True)
            self.events.append(
                {
                    "type": "tool.call.started",
                    "at": utc_now(),
                    "data": {"call_id": call_id, "name": name, "input": capture},
                }
            )
            self.reserved_finishes += 1
            self.in_flight[inflight_key] = _InFlight(call_id, time.monotonic())

    def _cancel_tool_call(self, request_id: object) -> None:
        key = _request_key(request_id)
        if key is None:
            return
        with self.lock:
            if self.sealed:
                self._mark_unobserved_locked("sealed")
                return
            entry = self.in_flight.pop(("client", key[0], key[1]), None)
            if entry is None:
                return
            if len(self.ignored_response_keys) < MAX_IN_FLIGHT:
                self.ignored_response_keys.add(("client", key[0], key[1]))
            self._finish_locked(entry, "cancelled", None, None)

    def _observe_server(self, message: dict[str, object]) -> None:
        if "method" in message or ("result" in message) == ("error" in message):
            return
        key = _request_key(message.get("id"))
        if key is None:
            return
        with self.lock:
            if self.sealed:
                self._mark_unobserved_locked("sealed")
                return
            inflight_key = ("client", key[0], key[1])
            entry = self.in_flight.pop(inflight_key, None)
            if entry is None:
                if inflight_key in self.ignored_response_keys:
                    self.ignored_response_keys.remove(inflight_key)
                    self.unmatched_results += 1
                    self.reasons.add("unmatched_result")
                return
            if "error" in message:
                self._finish_locked(
                    entry, "failed", message.get("error"), "Mcp.ProtocolError"
                )
                return
            result = message.get("result")
            if isinstance(result, dict) and result.get("isError") is True:
                self._finish_locked(
                    entry, "failed", result, "Mcp.ToolExecutionError"
                )
            else:
                self._finish_locked(entry, "succeeded", result, None)

    def _finish_locked(
        self,
        entry: _InFlight,
        outcome: str,
        output: object,
        error_type: str | None,
    ) -> None:
        data: dict[str, object] = {
            "call_id": entry.call_id,
            "outcome": outcome,
            "duration_ms": max(0, int((time.monotonic() - entry.started) * 1000)),
            "output": self._capture_locked(output, input_value=False),
        }
        if error_type is not None:
            data["error_type"] = error_type
        self.events.append({"type": "tool.call.finished", "at": utc_now(), "data": data})
        self.reserved_finishes -= 1

    def _capture_locked(self, value: object, *, input_value: bool) -> dict[str, object]:
        if self.mode == "omitted":
            self.capture_omitted["policy"] += 1
            return {"capture": "omitted"}
        if not _json_safe(value):
            self.capture_omitted["unsafe"] += 1
            return {"capture": "omitted"}
        capture_value = value
        capture_state = "captured"
        if self.mode == "redacted":
            try:
                capture_value, changed = _redact_json(value)
            except (RecursionError, ValueError):
                self.capture_omitted["unsafe"] += 1
                return {"capture": "omitted"}
            if changed:
                capture_state = "redacted"
        try:
            encoded = json.dumps(
                capture_value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
        except (TypeError, UnicodeEncodeError, ValueError):
            self.capture_omitted["unsafe"] += 1
            return {"capture": "omitted"}
        if (
            len(encoded) > MAX_CAPTURE_VALUE_BYTES
            or self.capture_bytes + len(encoded) > MAX_CAPTURE_TOTAL_BYTES
        ):
            self.capture_omitted["budget"] += 1
            return {"capture": "omitted"}
        if input_value and not isinstance(capture_value, dict):
            self.capture_omitted["unsafe"] += 1
            return {"capture": "omitted"}
        self.capture_bytes += len(encoded)
        return {"capture": capture_state, "value": capture_value}

    def seal(self, reason: str | None = None) -> tuple[list[dict[str, object]], dict[str, object]]:
        with self.lock:
            self.sealed = True
            if reason:
                self.reasons.add(reason)
            metadata = {
                "complete": not self.reasons,
                "unobserved_messages": self.unobserved_messages,
                "unmatched_results": self.unmatched_results,
                "events_dropped": self.events_dropped,
                "reasons": sorted(self.reasons),
            }
            return list(self.events), metadata


def _sanitize_command(command: list[str]) -> tuple[list[str], bool, int]:
    stored: list[str] = []
    lossy = False
    omitted = 0
    used = 0
    marker = "[OMITTED]"
    marker_bytes = len(marker.encode("utf-8"))
    for index, argument in enumerate(command):
        try:
            normalized = argument.encode("utf-8", "surrogateescape").decode(
                "utf-8", "replace"
            )
        except UnicodeEncodeError:
            normalized = argument.encode("utf-8", "backslashreplace").decode("utf-8")
        lossy = lossy or normalized != argument
        normalized = _mask_url_userinfo(redact_text(normalized))
        encoded = normalized.encode("utf-8")
        if (
            len(encoded) > MAX_STORED_COMMAND_BYTES
            or used + len(encoded) > MAX_STORED_COMMAND_BYTES
        ):
            while stored and used + marker_bytes > MAX_STORED_COMMAND_BYTES:
                removed = stored.pop()
                used -= len(removed.encode("utf-8"))
                omitted += 1
            stored.append(marker)
            omitted += len(command) - index
            break
        stored.append(normalized)
        used += len(encoded)
    return stored or [marker], lossy, omitted


def _mask_url_userinfo(value: str) -> str:
    scheme_end = value.find("://")
    if scheme_end <= 0:
        return value
    authority_start = scheme_end + 3
    authority_end = len(value)
    for separator in "/?#":
        index = value.find(separator, authority_start)
        if index >= 0:
            authority_end = min(authority_end, index)
    authority = value[authority_start:authority_end]
    userinfo_end = authority.rfind("@")
    if userinfo_end <= 0:
        return value
    return (
        value[:authority_start]
        + REDACTION
        + authority[userinfo_end:]
        + value[authority_end:]
    )


def _supported_envelope(message: dict[str, object]) -> bool:
    if message.get("jsonrpc") != "2.0":
        return False
    if "method" in message:
        return (
            isinstance(message.get("method"), str)
            and "result" not in message
            and "error" not in message
        )
    return (
        "id" in message
        and ("result" in message) != ("error" in message)
    )


def _resolve_executable(command: list[str]) -> list[str]:
    target = command[0]
    candidate: Path | None = None
    try:
        if any(separator in target for separator in ("/", "\\")):
            candidate = Path(target).expanduser().resolve()
        else:
            cwd = Path.cwd().resolve()
            suffixes = [""]
            if os.name == "nt":
                suffixes = [
                    suffix
                    for suffix in os.environ.get("PATHEXT", ".EXE;.COM").split(";")
                    if suffix.lower() in {".exe", ".com"}
                ]
                if Path(target).suffix.lower() in {".exe", ".com"}:
                    suffixes = [""]
            for entry in os.environ.get("PATH", "").split(os.pathsep):
                if not entry:
                    continue
                directory = Path(entry).expanduser().resolve()
                if directory == cwd:
                    continue
                for suffix in suffixes:
                    found = directory / f"{target}{suffix}"
                    if found.is_file():
                        candidate = found.resolve()
                        break
                if candidate is not None:
                    break
        if candidate is None or not candidate.is_file():
            raise McpProxyError("mcp_server_not_found")
        if os.name == "nt" and candidate.suffix.lower() not in {".exe", ".com"}:
            raise McpProxyError("mcp_server_unsafe_launcher")
        snapshot = candidate.stat()
    except (OSError, RuntimeError):
        raise McpProxyError("mcp_server_not_found") from None
    if not stat.S_ISREG(snapshot.st_mode):
        raise McpProxyError("mcp_server_not_found")
    return [str(candidate), *command[1:]]


def _prepare_standard_fds() -> tuple[int, int]:
    for fd, flags in ((0, os.O_RDONLY), (1, os.O_WRONLY), (2, os.O_WRONLY)):
        try:
            os.fstat(fd)
        except OSError:
            replacement = os.open(os.devnull, flags)
            if replacement != fd:
                os.dup2(replacement, fd)
                os.close(replacement)
    if os.name == "nt":
        import msvcrt

        for fd in (0, 1, 2):
            msvcrt.setmode(fd, os.O_BINARY)
    stdout_fd: int | None = None
    stderr_fd: int | None = None
    try:
        stdout_fd = os.dup(1)
        stderr_fd = os.dup(2)
        if os.name == "nt":
            import msvcrt

            msvcrt.setmode(stdout_fd, os.O_BINARY)
            msvcrt.setmode(stderr_fd, os.O_BINARY)
        return stdout_fd, stderr_fd
    except OSError:
        if stdout_fd is not None:
            os.close(stdout_fd)
        if stderr_fd is not None:
            os.close(stderr_fd)
        raise


def _raw_write(fd: int, data: bytes, lock: threading.Lock) -> None:
    view = memoryview(data)
    with lock:
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            except BlockingIOError:
                if os.name == "nt":
                    time.sleep(0.01)
                else:
                    select.select([], [fd], [], 0.1)
                continue
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    continue
                raise
            if written <= 0:
                raise OSError(errno.EPIPE, "zero-byte write")
            view = view[written:]


def _raw_read(fd: int) -> bytes:
    while True:
        try:
            return os.read(fd, IO_CHUNK_BYTES)
        except InterruptedError:
            continue
        except BlockingIOError:
            if os.name == "nt":
                time.sleep(0.01)
            else:
                select.select([fd], [], [], 0.1)
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                raise


def _relay_messages(
    source_fd: int,
    target_fd: int,
    target_lock: threading.Lock,
    observer: _Observer,
    direction: Direction,
    done: threading.Event,
    failure: list[str],
    shutdown: threading.Event,
    *,
    on_eof: Callable[[], None] | None = None,
) -> None:
    pending = bytearray()
    streaming = False
    try:
        while not shutdown.is_set():
            chunk = _raw_read(source_fd)
            if not chunk:
                if pending:
                    if streaming:
                        _raw_write(target_fd, pending, target_lock)
                    else:
                        observer.observe(direction, bytes(pending))
                        _raw_write(target_fd, pending, target_lock)
                if on_eof is not None:
                    on_eof()
                return
            offset = 0
            while offset < len(chunk):
                newline = chunk.find(b"\n", offset)
                end = len(chunk) if newline < 0 else newline + 1
                segment = chunk[offset:end]
                offset = end
                if streaming:
                    _raw_write(target_fd, segment, target_lock)
                    if newline >= 0:
                        streaming = False
                    continue
                remaining = OBSERVATION_BYTES + 1 - len(pending)
                pending.extend(segment[:remaining])
                overflow = segment[remaining:]
                if len(pending) > OBSERVATION_BYTES:
                    observer.mark_unobserved("oversized")
                    _raw_write(target_fd, pending, target_lock)
                    if overflow:
                        _raw_write(target_fd, overflow, target_lock)
                    pending.clear()
                    streaming = newline < 0
                elif newline >= 0:
                    payload = bytes(pending)
                    pending.clear()
                    observer.observe(direction, payload)
                    _raw_write(target_fd, payload, target_lock)
    except BaseException:
        if not shutdown.is_set():
            failure.append("mcp_relay_failed")
            shutdown.set()
    finally:
        done.set()


def _relay_stderr(
    source_fd: int,
    target_fd: int,
    lock: threading.Lock,
    done: threading.Event,
    failure: list[str],
    shutdown: threading.Event,
    last_byte: list[int | None],
) -> None:
    try:
        while not shutdown.is_set():
            chunk = _raw_read(source_fd)
            if not chunk:
                return
            _raw_write(target_fd, chunk, lock)
            last_byte[0] = chunk[-1]
    except BaseException:
        if not shutdown.is_set():
            failure.append("mcp_stderr_relay_failed")
            shutdown.set()
    finally:
        done.set()


class _WindowsJob:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.handle: int | None = None
        self._kernel32 = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"
            )]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
        process_handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(handle, process_handle):
            kernel32.TerminateProcess(process_handle, 1)
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        in_job = ctypes.c_int()
        if (
            not kernel32.IsProcessInJob(process_handle, handle, ctypes.byref(in_job))
            or not in_job.value
        ):
            kernel32.TerminateProcess(process_handle, 1)
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "IsProcessInJob")
        self.handle = handle
        self._kernel32 = kernel32

    def terminate(self) -> None:
        if self.handle is not None and self._kernel32 is not None:
            self._kernel32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None
            self._kernel32 = None


def _terminate_tree(process: subprocess.Popen[bytes], job: _WindowsJob | None) -> None:
    if os.name == "nt":
        if job is not None:
            job.terminate()
        elif process.poll() is None:
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    if os.name != "nt":
        deadline = time.monotonic() + PROCESS_TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(POLL_SECONDS)
    try:
        process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        if job is not None:
            job.terminate()
        elif process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _mapped_exit_code(raw: int | None) -> int:
    if raw is None:
        return 1
    if os.name == "nt":
        return raw if 0 <= raw <= 255 else (0 if raw == 0 else 1)
    if raw < 0:
        return 128 + abs(raw)
    mapped = raw & 0xFF
    return 1 if raw != 0 and mapped == 0 else mapped


def _trim_trace(trace: dict[str, object], observer: _Observer) -> None:
    def encoded_size() -> int:
        return len(json.dumps(trace, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))

    events = trace["events"]
    assert isinstance(events, list)
    for event in reversed(events):
        if encoded_size() <= TRACE_TARGET_BYTES:
            return
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        capture = data.get("output") or data.get("input")
        if isinstance(capture, dict) and capture.get("capture") != "omitted":
            capture.clear()
            capture["capture"] = "omitted"
            observer.capture_omitted["final_size"] += 1
    while encoded_size() > TRACE_TARGET_BYTES:
        tool_index = next(
            (
                index
                for index, event in enumerate(events)
                if isinstance(event, dict)
                and str(event.get("type", "")).startswith("tool.call.")
            ),
            None,
        )
        if tool_index is None:
            raise McpProxyError("mcp_trace_too_large")
        event = events[tool_index]
        event_data = event.get("data")
        call_id = event_data.get("call_id") if isinstance(event_data, dict) else None
        kept: list[object] = []
        removed = 0
        for item in events:
            data = item.get("data") if isinstance(item, dict) else None
            if isinstance(data, dict) and data.get("call_id") == call_id:
                removed += 1
            else:
                kept.append(item)
        events[:] = kept
        observer.events_dropped += removed
        observer.reasons.add("events_dropped")


def proxy_mcp_stdio(
    command: list[str],
    *,
    root: Path,
    name: str | None = None,
    tool_data: CaptureMode = "omitted",
) -> ProxyResult:
    if not command:
        raise McpProxyError("mcp_server_required")
    resolved = _resolve_executable(command)
    stored_command, command_lossy, command_omitted = _sanitize_command(command)
    try:
        stdout_fd, stderr_fd = _prepare_standard_fds()
    except OSError:
        raise McpProxyError("mcp_stdio_unavailable") from None
    try:
        prepared = prepare_run_for_write(root, new_run_id())
    except StoreSafetyError:
        os.close(stdout_fd)
        os.close(stderr_fd)
        raise McpProxyError("mcp_store_unwritable") from None

    cwd = str(Path.cwd())
    started_at = utc_now()
    started = time.monotonic()
    observer = _Observer(tool_data, started_at, stored_command, cwd)
    stdout_lock = threading.Lock()
    stderr_lock = threading.Lock()
    shutdown = threading.Event()
    client_eof = threading.Event()
    stdout_done = threading.Event()
    stderr_done = threading.Event()
    client_done = threading.Event()
    failures: list[str] = []
    stderr_last: list[int | None] = [None]
    process: subprocess.Popen[bytes] | None = None
    job: _WindowsJob | None = None
    proxy_cleanup = False
    shutdown_reason = "child_exit"
    caught_signal: int | None = None
    old_handlers: dict[int, object] = {}

    def request_signal(signum: int, _frame: object) -> None:
        nonlocal caught_signal
        caught_signal = signum
        shutdown.set()

    signals = [signal.SIGINT]
    if os.name != "nt":
        signals.extend([signal.SIGTERM, signal.SIGHUP])
    if threading.current_thread() is threading.main_thread():
        for signum in signals:
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_signal)

    try:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                resolved,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
            try:
                job = _WindowsJob(process)
            except OSError:
                try:
                    process.terminate()
                    process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
                raise
        except OSError:
            raise McpProxyError("mcp_spawn_failed") from None
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        def close_child_stdin() -> None:
            client_eof.set()
            try:
                process.stdin.close()
            except OSError:
                pass

        threads = [
            threading.Thread(
                target=_relay_messages,
                args=(
                    0,
                    process.stdin.fileno(),
                    threading.Lock(),
                    observer,
                    "client",
                    client_done,
                    failures,
                    shutdown,
                ),
                kwargs={"on_eof": close_child_stdin},
                daemon=True,
            ),
            threading.Thread(
                target=_relay_messages,
                args=(
                    process.stdout.fileno(),
                    stdout_fd,
                    stdout_lock,
                    observer,
                    "server",
                    stdout_done,
                    failures,
                    shutdown,
                ),
                daemon=True,
            ),
            threading.Thread(
                target=_relay_stderr,
                args=(
                    process.stderr.fileno(),
                    stderr_fd,
                    stderr_lock,
                    stderr_done,
                    failures,
                    shutdown,
                    stderr_last,
                ),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        eof_deadline: float | None = None
        while True:
            if caught_signal is not None:
                shutdown_reason = "signal"
                break
            if failures:
                shutdown_reason = failures[0]
                break
            raw = process.poll()
            if raw is not None:
                shutdown_reason = "child_exit"
                break
            if client_eof.is_set():
                if eof_deadline is None:
                    eof_deadline = time.monotonic() + CLIENT_EOF_GRACE_SECONDS
                if time.monotonic() >= eof_deadline:
                    proxy_cleanup = True
                    shutdown_reason = "client_eof_grace_expired"
                    observer.mark_unobserved("client_disconnected")
                    break
            shutdown.wait(POLL_SECONDS)

        if process.poll() is None and (proxy_cleanup or failures or caught_signal is not None):
            _terminate_tree(process, job)
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            raw_child_code = process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_tree(process, job)
            raw_child_code = process.poll()
        stdout_done.wait(PROCESS_TERM_GRACE_SECONDS)
        stderr_done.wait(PROCESS_TERM_GRACE_SECONDS)
        if not stdout_done.is_set() or not stderr_done.is_set():
            proxy_cleanup = True
            shutdown_reason = "descendant_grace_expired"
            observer.mark_unobserved("descendant_cleanup")
            _terminate_tree(process, job)
            stdout_done.wait(PROCESS_TERM_GRACE_SECONDS)
            stderr_done.wait(PROCESS_TERM_GRACE_SECONDS)
    except McpProxyError as exc:
        failures.append(exc.code)
        shutdown_reason = exc.code
        raw_child_code = process.poll() if process is not None else None
    except BaseException:
        failures.append("mcp_proxy_failed")
        shutdown_reason = "mcp_proxy_failed"
        if process is not None and process.poll() is None:
            _terminate_tree(process, job)
        raw_child_code = process.poll() if process is not None else None
    finally:
        shutdown.set()
        for signum, handler in old_handlers.items():
            signal.signal(signum, signal.SIG_IGN)

    finished_at = utc_now()
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    if not client_done.is_set():
        observer.mark_unobserved("client_relay_unfinished")
    if process is not None and not stdout_done.is_set():
        observer.mark_unobserved("server_relay_unfinished")
    seal_reason = "client_disconnected" if proxy_cleanup else None
    events, observation = observer.seal(seal_reason)
    independent_child_failure = raw_child_code not in {None, 0} and not proxy_cleanup
    incomplete_inflight = bool(observer.in_flight) and bool(observation["complete"])
    failed = bool(failures or caught_signal or independent_child_failure or incomplete_inflight)
    status = "failed" if failed else "passed"
    top_exit = (
        None
        if raw_child_code is None
        else (0 if proxy_cleanup and not failures else raw_child_code)
    )
    events.append(
        {
            "type": "command.finished",
            "at": finished_at,
            "data": {
                "status": status,
                "exit_code": top_exit,
                "timed_out": False,
                "duration_ms": duration_ms,
            },
        }
    )
    metadata: dict[str, object] = {
        "transport": "stdio",
        "tool_data": tool_data,
        "streams": {"stdout": "relayed", "stderr": "relayed"},
        "observation": observation,
        "observation_limit_bytes": OBSERVATION_BYTES,
        "capture_omitted": observer.capture_omitted,
        "raw_child_exit_code": raw_child_code,
        "proxy_initiated_cleanup": proxy_cleanup,
        "shutdown_reason": shutdown_reason,
        "command_args_lossy": command_lossy,
        "command_args_omitted": command_omitted,
    }
    trace: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": prepared.run_id,
        "kind": "command",
        "name": sanitize_label(name) if name is not None else None,
        "status": status,
        "command": stored_command,
        "cwd": cwd,
        "pid": os.getpid(),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "timeout_seconds": None,
        "exit_code": top_exit,
        "timed_out": False,
        "redacted": tool_data == "redacted" or stored_command != command,
        "store_identity_verified": prepared.store.identity_verified,
        "artifacts": {"stdout": STDOUT_ARTIFACT, "stderr": STDERR_ARTIFACT},
        "events": events,
        "mcp_proxy": metadata,
    }
    try:
        _trim_trace(trace, observer)
        observation["events_dropped"] = observer.events_dropped
        observation["reasons"] = sorted(observer.reasons)
        observation["complete"] = not observer.reasons
        write_prepared_bytes(prepared, STDOUT_ARTIFACT, b"")
        write_prepared_bytes(prepared, STDERR_ARTIFACT, b"")
        publish_prepared_json(prepared, "trace.json", trace)
    except (McpProxyError, StoreSafetyError):
        failures.append("mcp_store_unwritable")

    if caught_signal is not None:
        signal_codes = {signal.SIGINT: 130}
        if hasattr(signal, "SIGTERM"):
            signal_codes[signal.SIGTERM] = 143
        if hasattr(signal, "SIGHUP"):
            signal_codes[signal.SIGHUP] = 129
        exit_code = signal_codes.get(caught_signal, 1)
    elif failures:
        exit_code = 1
    elif incomplete_inflight:
        exit_code = 1
    elif proxy_cleanup:
        exit_code = 0
    else:
        exit_code = _mapped_exit_code(raw_child_code)

    if failures:
        _write_diagnostic(stderr_fd, stderr_lock, stderr_last, failures[0], stderr_done.is_set())
    for signum, handler in old_handlers.items():
        signal.signal(signum, handler)
    if stdout_done.is_set():
        os.close(stdout_fd)
    if stderr_done.is_set():
        os.close(stderr_fd)
    if job is not None:
        job.close()
    return ProxyResult(trace=trace, exit_code=exit_code)


def _write_diagnostic(
    fd: int,
    lock: threading.Lock,
    last_byte: list[int | None],
    code: str,
    relay_done: bool,
) -> None:
    payload = f"tracord: mcp-proxy failed: {code}\n".encode("ascii")
    if not relay_done or last_byte[0] not in {None, 10}:
        payload = b"\n" + payload

    def write() -> None:
        try:
            _raw_write(fd, payload, lock)
        except OSError:
            pass

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    thread.join(0.2)
