from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest

from tracord import cli
from tracord import mcp_proxy
from tracord.mcp_proxy import ProxyResult
from tracord.run_listing import scan_runs
from tracord.schema import validate_trace


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


class _FakeWinFunction:
    def __init__(self, implementation: object) -> None:
        self.implementation = implementation
        self.restype: object = None
        self.argtypes: object = None

    def __call__(self, *args: object) -> object:
        return self.implementation(*args)  # type: ignore[operator]


def message(request_id: object, method: str, params: dict[str, object]) -> bytes:
    return (
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def run_proxy(
    tmp_path: Path,
    payload: bytes,
    *options: str,
    fixture_args: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = tmp_path / ".tracord"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tracord",
            "mcp-proxy",
            "--store",
            str(store),
            *options,
            "--",
            sys.executable,
            str(FIXTURE),
            *fixture_args,
        ],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        timeout=15,
        check=False,
    )
    run_paths = list((store / "runs").glob("*/trace.json"))
    assert len(run_paths) == 1
    trace_path = run_paths[0]
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    return completed, trace, trace_path.parent


def response_map(stdout: bytes) -> dict[object, dict[str, object]]:
    responses = [json.loads(line) for line in stdout.splitlines()]
    return {response["id"]: response for response in responses}


def tool_events(trace: dict[str, object]) -> list[dict[str, object]]:
    return [
        event
        for event in trace["events"]
        if isinstance(event, dict) and str(event.get("type", "")).startswith("tool.call.")
    ]


def test_cli_preserves_child_arguments_after_first_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[str] = []

    def fake_proxy(command: list[str], **_kwargs: object) -> ProxyResult:
        received.extend(command)
        return ProxyResult(trace={}, exit_code=0)

    monkeypatch.setattr(cli, "proxy_mcp_stdio", fake_proxy)
    assert (
        cli.main(
            [
                "mcp-proxy",
                "--store",
                "outer",
                "--",
                "server",
                "--store",
                "inner",
                "--",
                "--flag",
            ]
        )
        == 0
    )
    assert received == ["server", "--store", "inner", "--", "--flag"]


@pytest.mark.parametrize(
    "argv",
    [
        ["mcp-proxy"],
        ["mcp-proxy", "--"],
    ],
)
def test_cli_requires_separator_and_child_before_creating_store(
    tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([*argv[:1], "--store", str(tmp_path / "store"), *argv[1:]])
    assert exc_info.value.code == 2
    assert not (tmp_path / "store").exists()


def test_cli_help_does_not_require_server_separator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["mcp-proxy", "--help"])

    assert exc_info.value.code == 0
    assert "usage: tracord mcp-proxy" in capsys.readouterr().out


def test_proxy_forwards_lifecycle_and_records_outcomes(tmp_path: Path) -> None:
    payload = b"".join(
        [
            message(
                "init",
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            ),
            message("discover", "server/discover", {}),
            message("list", "tools/list", {}),
            message(1, "tools/call", {"name": "echo", "arguments": {"x": 1}}),
            message(2, "tools/call", {"name": "fail", "arguments": {}}),
            message(3, "tools/call", {"name": "protocol_error", "arguments": {}}),
        ]
    )
    completed, trace, run_dir = run_proxy(tmp_path, payload)

    assert completed.returncode == 0
    responses = response_map(completed.stdout)
    assert set(responses) == {"init", "discover", "list", 1, 2, 3}
    assert responses[1]["result"]["echo"] == {"x": 1}
    events = tool_events(trace)
    assert [event["type"] for event in events].count("tool.call.started") == 3
    finishes = [event["data"] for event in events if event["type"] == "tool.call.finished"]
    outcomes = {finish["call_id"]: finish["outcome"] for finish in finishes}
    error_types = {finish["call_id"]: finish.get("error_type") for finish in finishes}
    assert outcomes == {"call-1": "succeeded", "call-2": "failed", "call-3": "failed"}
    assert error_types == {
        "call-1": None,
        "call-2": "Mcp.ToolExecutionError",
        "call-3": "Mcp.ProtocolError",
    }
    assert trace["status"] == "passed"
    assert trace["mcp_proxy"]["observation"]["complete"] is True
    assert validate_trace(trace) == []
    assert (run_dir / "stdout.log").read_bytes() == b""
    assert (run_dir / "stderr.log").read_bytes() == b""


def test_proxy_correlates_out_of_order_calls(tmp_path: Path) -> None:
    payload = b"".join(
        [
            message("slow", "tools/call", {"name": "echo", "arguments": {"delay": 0.1}}),
            message("fast", "tools/call", {"name": "echo", "arguments": {"delay": 0}}),
        ]
    )
    completed, trace, _run_dir = run_proxy(tmp_path, payload)

    assert [json.loads(line)["id"] for line in completed.stdout.splitlines()] == [
        "fast",
        "slow",
    ]
    finishes = [
        event["data"]["call_id"]
        for event in tool_events(trace)
        if event["type"] == "tool.call.finished"
    ]
    assert finishes == ["call-2", "call-1"]
    assert validate_trace(trace) == []


def test_cancellation_finishes_once_and_late_response_is_unmatched(tmp_path: Path) -> None:
    payload = b"".join(
        [
            message(1, "tools/call", {"name": "echo", "arguments": {"delay": 0.05}}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 1.0, "reason": "private reason"},
                },
                separators=(",", ":"),
            ).encode()
            + b"\n",
        ]
    )
    completed, trace, run_dir = run_proxy(tmp_path, payload)

    assert completed.returncode == 0
    finishes = [event for event in tool_events(trace) if event["type"] == "tool.call.finished"]
    assert len(finishes) == 1
    assert finishes[0]["data"]["outcome"] == "cancelled"
    assert trace["mcp_proxy"]["observation"]["unmatched_results"] == 1
    assert b"private reason" not in (run_dir / "trace.json").read_bytes()
    assert validate_trace(trace) == []


def test_id_reuse_waits_for_late_cancelled_response() -> None:
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    call = message(1, "tools/call", {"name": "echo", "arguments": {}})
    observer.observe("client", call)
    observer.observe(
        "client",
        b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}\n',
    )
    observer.observe("client", call)
    observer.observe("server", b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    observer.observe("client", call)
    observer.observe("server", b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    events, metadata = observer.seal()

    assert [event["type"] for event in events].count("tool.call.started") == 2
    outcomes = [
        event["data"]["outcome"]
        for event in events
        if event["type"] == "tool.call.finished"
    ]
    assert outcomes == ["cancelled", "succeeded"]
    assert metadata["reasons"] == ["duplicate_id", "unmatched_result"]


def test_malformed_and_oversized_messages_relay_without_breaking_later_call(
    tmp_path: Path,
) -> None:
    oversized = message(
        "large",
        "tools/call",
        {"name": "echo", "arguments": {"padding": "x" * (1024 * 1024 + 64)}},
    )
    payload = b"not-json\n" + oversized + message(
        "small", "tools/call", {"name": "echo", "arguments": {}}
    )
    completed, trace, _run_dir = run_proxy(tmp_path, payload)

    responses = response_map(completed.stdout)
    assert set(responses) == {"large", "small"}
    starts = [event for event in tool_events(trace) if event["type"] == "tool.call.started"]
    assert len(starts) == 1
    assert starts[0]["data"]["call_id"] == "call-1"
    observation = trace["mcp_proxy"]["observation"]
    assert observation["complete"] is False
    assert observation["unobserved_messages"] >= 2
    assert "oversized" in observation["reasons"]
    assert "unparseable" in observation["reasons"]
    assert validate_trace(trace) == []


def test_tool_capture_modes_are_private_and_explicit(tmp_path: Path) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    payload = message(
        1,
        "tools/call",
        {"name": "echo", "arguments": {"token": secret, "plain": "visible"}},
    )

    omitted, omitted_trace, omitted_dir = run_proxy(tmp_path / "omitted", payload)
    assert omitted.returncode == 0
    assert secret.encode() not in (omitted_dir / "trace.json").read_bytes()
    omitted_events = tool_events(omitted_trace)
    assert omitted_events[0]["data"]["input"] == {"capture": "omitted"}

    redacted, redacted_trace, redacted_dir = run_proxy(
        tmp_path / "redacted", payload, "--tool-data", "redacted"
    )
    assert redacted.returncode == 0
    redacted_bytes = (redacted_dir / "trace.json").read_bytes()
    assert secret.encode() not in redacted_bytes
    assert b"[REDACTED]" in redacted_bytes
    redacted_events = tool_events(redacted_trace)
    assert redacted_events[0]["data"]["input"]["capture"] == "redacted"

    captured, captured_trace, captured_dir = run_proxy(
        tmp_path / "captured", payload, "--tool-data", "captured"
    )
    assert captured.returncode == 0
    assert secret.encode() in (captured_dir / "trace.json").read_bytes()
    captured_events = tool_events(captured_trace)
    assert captured_events[0]["data"]["input"]["capture"] == "captured"


def test_server_stderr_is_relayed_but_not_recorded(tmp_path: Path) -> None:
    completed, trace, run_dir = run_proxy(
        tmp_path,
        message(1, "tools/call", {"name": "stderr", "arguments": {}}),
    )

    assert completed.stderr == b"fixture stderr without newline"
    assert (run_dir / "stderr.log").read_bytes() == b""
    assert b"fixture stderr" not in (run_dir / "trace.json").read_bytes()
    assert trace["mcp_proxy"]["streams"]["stderr"] == "relayed"


def test_startup_failure_is_fixed_and_does_not_create_a_store(tmp_path: Path) -> None:
    store = tmp_path / ".tracord"
    missing = tmp_path / "private-server-name"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tracord",
            "mcp-proxy",
            "--store",
            str(store),
            "--",
            str(missing),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr.replace(b"\r\n", b"\n") == (
        b"tracord: mcp-proxy failed: mcp_server_not_found\n"
    )
    assert str(missing).encode() not in completed.stderr
    assert not store.exists()


def test_spawn_failure_publishes_a_path_free_failed_trace(tmp_path: Path) -> None:
    store = tmp_path / ".tracord"
    invalid = tmp_path / ("private.exe" if sys.platform == "win32" else "private")
    invalid.write_bytes(b"not an executable")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tracord",
            "mcp-proxy",
            "--store",
            str(store),
            "--",
            str(invalid),
        ],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )

    traces = list((store / "runs").glob("*/trace.json"))
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert b"mcp_spawn_failed" in completed.stderr
    assert str(invalid).encode() not in completed.stderr
    assert len(traces) == 1
    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    assert trace["status"] == "failed"
    assert trace["exit_code"] == 1
    assert trace["mcp_proxy"]["shutdown_reason"] == "mcp_spawn_failed"
    listing = scan_runs(store)
    assert len(listing.runs) == 1
    assert listing.skipped == 0


def test_raw_framing_is_byte_identical(tmp_path: Path) -> None:
    payload = (
        b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"method":"noop"}\r\n'
        b"not-json\r\n"
        b'{"jsonrpc":"2.0","id":2,"method":"noop"}'
    )
    completed, trace, _run_dir = run_proxy(
        tmp_path, payload, fixture_args=("raw-echo",)
    )

    assert completed.returncode == 0
    assert completed.stdout == payload
    assert trace["mcp_proxy"]["observation"]["complete"] is False
    assert "unparseable" in trace["mcp_proxy"]["observation"]["reasons"]


def test_observation_buffer_stops_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_proxy, "OBSERVATION_BYTES", 4)
    reads = iter([b"abcdefghijkl", b""])
    writes: list[bytes] = []
    monkeypatch.setattr(mcp_proxy, "_raw_read", lambda _fd: next(reads))
    monkeypatch.setattr(
        mcp_proxy,
        "_raw_write",
        lambda _fd, data, _lock: writes.append(bytes(data)),
    )
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    done = mcp_proxy.threading.Event()
    failures: list[str] = []
    mcp_proxy._relay_messages(
        0,
        1,
        mcp_proxy.threading.Lock(),
        observer,
        "client",
        done,
        failures,
        mcp_proxy.threading.Event(),
    )

    assert writes == [b"abcde", b"fghijkl"]
    assert failures == []
    assert observer.unobserved_messages == 1


def test_overflow_write_failure_does_not_repeat_forwarded_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_proxy, "OBSERVATION_BYTES", 4)
    reads = iter([b"abcdefghijkl"])
    attempts: list[bytes] = []

    def write(_fd: int, data: bytes, _lock: object) -> None:
        attempts.append(bytes(data))
        if len(attempts) == 2:
            raise OSError("blocked")

    monkeypatch.setattr(mcp_proxy, "_raw_read", lambda _fd: next(reads))
    monkeypatch.setattr(mcp_proxy, "_raw_write", write)
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    failures: list[str] = []
    mcp_proxy._relay_messages(
        0,
        1,
        mcp_proxy.threading.Lock(),
        observer,
        "server",
        mcp_proxy.threading.Event(),
        failures,
        mcp_proxy.threading.Event(),
    )

    assert attempts == [b"abcde", b"fghijkl"]
    assert failures == ["mcp_relay_failed"]


def test_partial_message_is_flushed_when_shutdown_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = mcp_proxy.threading.Event()
    writes: list[bytes] = []

    def read(_fd: int) -> bytes:
        shutdown.set()
        return b"partial"

    monkeypatch.setattr(mcp_proxy, "_raw_read", read)
    monkeypatch.setattr(
        mcp_proxy,
        "_raw_write",
        lambda _fd, data, _lock, **_kwargs: writes.append(bytes(data)) or True,
    )
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    done = mcp_proxy.threading.Event()
    mcp_proxy._relay_messages(
        0,
        1,
        mcp_proxy.threading.Lock(),
        observer,
        "client",
        done,
        [],
        shutdown,
    )
    assert writes == [b"partial"]


def test_closed_write_channel_cannot_write_to_reused_fd(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    stream = target.open("wb")
    channel = mcp_proxy._WriteChannel(
        stream.fileno(), mcp_proxy.threading.Lock(), mcp_proxy.threading.Event()
    )
    channel.close(stream)
    replacement = os.open(target, os.O_WRONLY | os.O_APPEND)
    try:
        assert channel.write(b"private protocol bytes") is False
    finally:
        os.close(replacement)
    assert target.read_bytes() == b""


def test_write_channel_close_is_bounded_while_writer_holds_lock(
    tmp_path: Path,
) -> None:
    stream = (tmp_path / "target").open("wb")
    channel = mcp_proxy._WriteChannel(
        stream.fileno(), mcp_proxy.threading.Lock(), mcp_proxy.threading.Event()
    )
    channel.lock.acquire()
    try:
        assert channel.close(stream, timeout=0.01) is False
        assert channel.closed.is_set()
        assert not channel.stream_closed.is_set()
    finally:
        channel.lock.release()

    assert channel.close(stream, timeout=0.01) is True
    assert channel.stream_closed.is_set()
    assert channel.write(b"late") is False


def test_raw_io_retries_interruptions_and_partial_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[object] = [InterruptedError(), b"data"]

    def read(_fd: int, _size: int) -> bytes:
        result = reads.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    writes: list[bytes] = []

    def write(_fd: int, data: object) -> int:
        payload = bytes(data)
        writes.append(payload)
        return min(2, len(payload))

    monkeypatch.setattr(mcp_proxy.os, "read", read)
    monkeypatch.setattr(mcp_proxy.os, "write", write)
    assert mcp_proxy._raw_read(0) == b"data"
    mcp_proxy._raw_write(1, b"abcde", mcp_proxy.threading.Lock())
    assert writes == [b"abcde", b"cde", b"e"]


def test_zero_byte_raw_write_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_proxy.os, "write", lambda _fd, _data: 0)
    with pytest.raises(OSError, match="zero-byte write"):
        mcp_proxy._raw_write(1, b"data", mcp_proxy.threading.Lock())


def test_unsupported_jsonrpc_envelopes_are_not_observed() -> None:
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    observer.observe(
        "client",
        b'{"id":1,"method":"tools/call","params":{"name":"echo","arguments":{}}}\n',
    )
    observer.observe(
        "client",
        (
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call","result":{},'
            b'"params":{"name":"echo","arguments":{}}}\n'
        ),
    )
    events, metadata = observer.seal()

    assert [event["type"] for event in events] == ["command.started"]
    assert metadata["unobserved_messages"] == 2
    assert metadata["reasons"] == ["unsupported_envelope"]


def test_sealed_observer_metadata_is_immutable() -> None:
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    _events, before = observer.seal()
    observer.mark_unobserved("late")
    observer.observe("client", b"not-json\n")
    after = observer.snapshot_observation()
    assert after == before


def test_incomplete_lifecycle_reason_does_not_count_as_a_message() -> None:
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    observer.mark_incomplete("descendant_cleanup")

    metadata = observer.snapshot_observation()
    assert metadata["complete"] is False
    assert metadata["unobserved_messages"] == 0
    assert metadata["reasons"] == ["descendant_cleanup"]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"jsonrpc":"2.0","id":1,"id":2,"result":{}}\n',
        b'{"jsonrpc":"2.0","id":1,"result":NaN}\n',
        b'\xff\n',
        b'[]\n',
    ],
)
def test_unsupported_json_domains_are_unobserved(payload: bytes) -> None:
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    observer.observe("server", payload)
    _events, metadata = observer.seal()
    assert metadata["complete"] is False
    assert metadata["unobserved_messages"] == 1


def test_server_request_with_same_id_does_not_consume_client_call(tmp_path: Path) -> None:
    completed, trace, _run_dir = run_proxy(
        tmp_path,
        message(1, "tools/call", {"name": "echo", "arguments": {}}),
        fixture_args=("server-request",),
    )

    assert completed.returncode == 0
    messages = [json.loads(line) for line in completed.stdout.splitlines()]
    assert messages[0]["method"] == "sampling/createMessage"
    finishes = [event for event in tool_events(trace) if event["type"] == "tool.call.finished"]
    assert len(finishes) == 1
    assert finishes[0]["data"]["outcome"] == "succeeded"


def test_only_literal_true_is_a_tool_execution_error() -> None:
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    for index in range(1, 4):
        observer.observe("client", message(index, "tools/call", {"name": "echo", "arguments": {}}))
    for index, value in enumerate((False, "true", True), start=1):
        observer.observe(
            "server",
            json.dumps(
                {"jsonrpc": "2.0", "id": index, "result": {"isError": value}}
            ).encode()
            + b"\n",
        )
    events, _metadata = observer.seal()
    outcomes = [
        event["data"]["outcome"]
        for event in events
        if event["type"] == "tool.call.finished"
    ]
    assert outcomes == ["succeeded", "succeeded", "failed"]


def test_client_eof_cleans_up_non_exiting_server(tmp_path: Path) -> None:
    started = time.monotonic()
    completed, trace, _run_dir = run_proxy(
        tmp_path, b"", fixture_args=("hang-after-eof",)
    )

    assert time.monotonic() - started < 8
    assert completed.returncode == 0
    assert trace["mcp_proxy"]["proxy_initiated_cleanup"] is True
    assert trace["mcp_proxy"]["shutdown_reason"] == "client_eof_grace_expired"
    assert trace["mcp_proxy"]["observation"]["complete"] is False


def test_non_exiting_descendant_is_cleaned_up(tmp_path: Path) -> None:
    started = time.monotonic()
    completed, trace, _run_dir = run_proxy(
        tmp_path, b"", fixture_args=("spawn-descendant",)
    )

    assert time.monotonic() - started < 8
    assert completed.returncode == 0
    assert trace["mcp_proxy"]["proxy_initiated_cleanup"] is True
    assert trace["mcp_proxy"]["shutdown_reason"] == "descendant_grace_expired"
    assert "descendant_cleanup" in trace["mcp_proxy"]["observation"]["reasons"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job launch semantics")
def test_immediate_windows_descendant_starts_in_job_and_is_cleaned_up(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "descendant.pid"
    completed, trace, _run_dir = run_proxy(
        tmp_path,
        b"",
        fixture_args=("spawn-descendant-pid", str(pid_path)),
    )

    assert completed.returncode == 0
    assert trace["mcp_proxy"]["shutdown_reason"] == "descendant_grace_expired"
    descendant_pid = int(pid_path.read_text(encoding="ascii"))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    process_handle = kernel32.OpenProcess(0x1000, False, descendant_pid)
    if process_handle:
        try:
            exit_code = ctypes.c_uint32()
            assert kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code))
            assert exit_code.value != 259
        finally:
            kernel32.CloseHandle(process_handle)
    else:
        assert ctypes.get_last_error() == 87


@pytest.mark.skipif(os.name != "nt", reason="Windows Job launch semantics")
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("create", []),
        ("configure", ["close_job"]),
        ("assign", ["terminate_process", "close_job"]),
        ("verify", ["terminate_process", "close_job"]),
        ("membership", ["terminate_process", "close_job"]),
    ],
)
def test_windows_job_setup_failures_terminate_suspended_process_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: list[str],
) -> None:
    calls: list[str] = []

    def create_job(*_args: object) -> int:
        if failure == "create":
            ctypes.set_last_error(5)
            return 0
        return 101

    def configure(*_args: object) -> int:
        if failure == "configure":
            ctypes.set_last_error(5)
            return 0
        return 1

    def assign(*_args: object) -> int:
        if failure == "assign":
            ctypes.set_last_error(5)
            return 0
        return 1

    def verify(_process: object, _job: object, result: object) -> int:
        if failure == "verify":
            ctypes.set_last_error(5)
            return 0
        result._obj.value = 0 if failure == "membership" else 1  # type: ignore[attr-defined]
        return 1

    class Kernel32:
        CreateJobObjectW = _FakeWinFunction(create_job)
        CloseHandle = _FakeWinFunction(lambda _handle: calls.append("close_job"))
        SetInformationJobObject = _FakeWinFunction(configure)
        AssignProcessToJobObject = _FakeWinFunction(assign)
        IsProcessInJob = _FakeWinFunction(verify)
        TerminateProcess = _FakeWinFunction(
            lambda *_args: calls.append("terminate_process")
        )
        TerminateJobObject = _FakeWinFunction(lambda *_args: None)

    class Process:
        _handle = 202

    monkeypatch.setattr(mcp_proxy.ctypes, "WinDLL", lambda *_a, **_k: Kernel32())

    with pytest.raises(OSError):
        mcp_proxy._WindowsJob(Process())  # type: ignore[arg-type]

    assert calls == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows Job launch semantics")
@pytest.mark.parametrize(
    "failure",
    [
        "snapshot",
        "first",
        "next",
        "no_thread",
        "multiple",
        "open",
        "ownership",
        "resume",
        "suspend_count",
    ],
)
def test_windows_resume_api_failures_leave_process_suspended(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    calls: list[str] = []
    next_calls = 0

    def snapshot(*_args: object) -> int:
        if failure == "snapshot":
            ctypes.set_last_error(5)
            return ctypes.c_void_p(-1).value  # type: ignore[return-value]
        return 303

    def first(_snapshot: object, entry: object) -> int:
        if failure == "first":
            ctypes.set_last_error(5)
            return 0
        entry._obj.th32OwnerProcessID = 9999 if failure == "no_thread" else 4321  # type: ignore[attr-defined]
        entry._obj.th32ThreadID = 404  # type: ignore[attr-defined]
        return 1

    def next_thread(_snapshot: object, entry: object) -> int:
        nonlocal next_calls
        if failure == "next":
            ctypes.set_last_error(5)
            return 0
        if failure == "multiple" and next_calls == 0:
            next_calls += 1
            entry._obj.th32OwnerProcessID = 4321  # type: ignore[attr-defined]
            entry._obj.th32ThreadID = 405  # type: ignore[attr-defined]
            return 1
        ctypes.set_last_error(mcp_proxy._ERROR_NO_MORE_FILES)
        return 0

    def open_thread(*_args: object) -> int:
        if failure == "open":
            ctypes.set_last_error(5)
            return 0
        return 505

    def resume(*_args: object) -> int:
        if failure == "resume":
            ctypes.set_last_error(5)
            return 0xFFFFFFFF
        if failure == "suspend_count":
            return 2
        return 1

    class Kernel32:
        CreateToolhelp32Snapshot = _FakeWinFunction(snapshot)
        Thread32First = _FakeWinFunction(first)
        Thread32Next = _FakeWinFunction(next_thread)
        OpenThread = _FakeWinFunction(open_thread)
        GetProcessIdOfThread = _FakeWinFunction(
            lambda _thread: 9999 if failure == "ownership" else 4321
        )
        ResumeThread = _FakeWinFunction(resume)
        CloseHandle = _FakeWinFunction(lambda handle: calls.append(f"close:{handle}"))

    class Process:
        pid = 4321

    monkeypatch.setattr(mcp_proxy.ctypes, "WinDLL", lambda *_a, **_k: Kernel32())

    with pytest.raises(OSError):
        mcp_proxy._resume_windows_process(Process())  # type: ignore[arg-type]

    if failure == "snapshot":
        assert calls == []
    else:
        assert calls[0] == "close:303"
        if failure == "resume":
            assert calls[-1] == "close:505"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job launch semantics")
def test_windows_thread_snapshot_retries_transient_bad_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def snapshot(*_args: object) -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            ctypes.set_last_error(mcp_proxy._ERROR_BAD_LENGTH)
            return ctypes.c_void_p(-1).value  # type: ignore[return-value]
        return 303

    def first(_snapshot: object, entry: object) -> int:
        entry._obj.th32OwnerProcessID = 4321  # type: ignore[attr-defined]
        entry._obj.th32ThreadID = 404  # type: ignore[attr-defined]
        return 1

    def no_more(*_args: object) -> int:
        ctypes.set_last_error(mcp_proxy._ERROR_NO_MORE_FILES)
        return 0

    class Kernel32:
        CreateToolhelp32Snapshot = _FakeWinFunction(snapshot)
        Thread32First = _FakeWinFunction(first)
        Thread32Next = _FakeWinFunction(no_more)
        OpenThread = _FakeWinFunction(lambda *_args: 505)
        GetProcessIdOfThread = _FakeWinFunction(lambda _thread: 4321)
        ResumeThread = _FakeWinFunction(lambda _thread: 1)
        CloseHandle = _FakeWinFunction(lambda _handle: None)

    class Process:
        pid = 4321

    monkeypatch.setattr(mcp_proxy.ctypes, "WinDLL", lambda *_a, **_k: Kernel32())
    monkeypatch.setattr(mcp_proxy.time, "sleep", lambda delay: sleeps.append(delay))

    mcp_proxy._resume_windows_process(Process())  # type: ignore[arg-type]

    assert attempts == 3
    assert sleeps == [mcp_proxy.POLL_SECONDS, mcp_proxy.POLL_SECONDS]


def test_windows_launch_abort_without_job_escalates_to_kill() -> None:
    order: list[str] = []

    class Process:
        waits = 0

        def terminate(self) -> None:
            order.append("terminate")

        def wait(self, *, timeout: float) -> int:
            self.waits += 1
            order.append("wait")
            if self.waits == 1:
                raise subprocess.TimeoutExpired("server", timeout)
            return 1

        def kill(self) -> None:
            order.append("kill")

    mcp_proxy._abort_windows_launch(Process(), None)  # type: ignore[arg-type]

    assert order == ["terminate", "wait", "kill", "wait"]


def test_windows_launch_abort_closes_streams_and_job_when_termination_fails() -> None:
    order: list[str] = []

    class Stream:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            order.append(f"close_{self.name}")

    class Process:
        stdin = Stream("stdin")
        stdout = Stream("stdout")
        stderr = Stream("stderr")

        def wait(self, *, timeout: float) -> int:
            order.append("wait")
            return 1

    class Job:
        def terminate(self) -> None:
            order.append("terminate_job")
            raise OSError("termination failed")

        def close(self) -> None:
            order.append("close_job")

    mcp_proxy._abort_windows_launch(Process(), Job())  # type: ignore[arg-type]

    assert order == [
        "terminate_job",
        "wait",
        "close_stdin",
        "close_stdout",
        "close_stderr",
        "close_job",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job launch semantics")
def test_windows_launch_is_suspended_until_job_membership_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    captured: dict[str, object] = {}

    class Process:
        pid = 4321

    class Job:
        def __init__(self, process: object) -> None:
            assert isinstance(process, Process)
            order.append("job")

    def popen(command: list[str], **kwargs: object) -> Process:
        captured["command"] = command
        captured.update(kwargs)
        order.append("popen")
        return Process()

    monkeypatch.setattr(mcp_proxy.subprocess, "Popen", popen)
    monkeypatch.setattr(mcp_proxy, "_WindowsJob", Job)
    monkeypatch.setattr(
        mcp_proxy,
        "_resume_windows_process",
        lambda process: order.append("resume"),
    )

    process, job, process_group = mcp_proxy._launch_process(["server.exe"])

    assert isinstance(process, Process)
    assert isinstance(job, Job)
    assert process_group is None
    assert order == ["popen", "job", "resume"]
    assert int(captured["creationflags"]) & mcp_proxy._CREATE_SUSPENDED
    assert int(captured["creationflags"]) & subprocess.CREATE_NEW_PROCESS_GROUP
    assert captured["shell"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows Job launch semantics")
def test_windows_launch_failure_terminates_job_before_reporting_spawn_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class Process:
        pid = 4321

        def wait(self, *, timeout: float) -> int:
            order.append("wait")
            return 1

        def kill(self) -> None:
            order.append("kill")

    class Job:
        def __init__(self, _process: object) -> None:
            order.append("job")

        def terminate(self) -> None:
            order.append("terminate_job")

        def close(self) -> None:
            order.append("close_job")

    monkeypatch.setattr(mcp_proxy.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(mcp_proxy, "_WindowsJob", Job)
    monkeypatch.setattr(
        mcp_proxy,
        "_resume_windows_process",
        lambda _process: (_ for _ in ()).throw(OSError("resume failed")),
    )

    with pytest.raises(OSError, match="secure Windows process launch unavailable"):
        mcp_proxy._launch_process(["server.exe"])

    assert order == ["job", "terminate_job", "wait", "close_job"]


def test_descendant_cleanup_does_not_mask_independent_child_failure(
    tmp_path: Path,
) -> None:
    completed, trace, _run_dir = run_proxy(
        tmp_path, b"", fixture_args=("spawn-descendant", "7")
    )

    assert completed.returncode == 7
    assert trace["status"] == "failed"
    assert trace["exit_code"] == 7
    assert trace["mcp_proxy"]["raw_child_exit_code"] == 7
    assert trace["mcp_proxy"]["proxy_initiated_cleanup"] is True


def test_blocked_child_stdin_does_not_block_descendant_cleanup(tmp_path: Path) -> None:
    started = time.monotonic()
    completed, trace, _run_dir = run_proxy(
        tmp_path,
        b"x" * (2 * 1024 * 1024),
        fixture_args=("spawn-descendant",),
    )

    assert time.monotonic() - started < 8
    assert completed.returncode == 0
    assert trace["mcp_proxy"]["proxy_initiated_cleanup"] is True
    assert "client_relay_blocked" in trace["mcp_proxy"]["observation"]["reasons"]


def test_blocked_stdout_consumer_does_not_block_cleanup(tmp_path: Path) -> None:
    store = tmp_path / ".tracord"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tracord",
            "mcp-proxy",
            "--store",
            str(store),
            "--",
            sys.executable,
            str(FIXTURE),
            "flood",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )
    process.wait(timeout=8)
    assert process.stdout is not None
    process.stdout.read()

    traces = list((store / "runs").glob("*/trace.json"))
    assert process.returncode == 0
    assert len(traces) == 1
    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    assert trace["mcp_proxy"]["proxy_initiated_cleanup"] is True
    assert trace["mcp_proxy"]["observation"]["complete"] is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
def test_signal_uses_bounded_cleanup_and_publishes_trace(tmp_path: Path) -> None:
    store = tmp_path / ".tracord"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tracord",
            "mcp-proxy",
            "--store",
            str(store),
            "--",
            sys.executable,
            str(FIXTURE),
            "hang-after-eof",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )
    deadline = time.monotonic() + 5
    while not (store / "runs").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    process.send_signal(signal.SIGTERM)
    _stdout, _stderr = process.communicate(timeout=8)

    traces = list((store / "runs").glob("*/trace.json"))
    assert process.returncode == 143
    assert len(traces) == 1
    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    assert trace["status"] == "failed"
    assert trace["mcp_proxy"]["shutdown_reason"] == "signal"


def test_nonzero_child_exit_is_preserved(tmp_path: Path) -> None:
    completed, trace, _run_dir = run_proxy(
        tmp_path, b"", fixture_args=("exit", "7")
    )

    assert completed.returncode == 7
    assert trace["status"] == "failed"
    assert trace["exit_code"] == 7
    assert trace["mcp_proxy"]["raw_child_exit_code"] == 7


def test_complete_observation_with_unfinished_call_fails(tmp_path: Path) -> None:
    completed, trace, _run_dir = run_proxy(
        tmp_path,
        message(1, "tools/call", {"name": "echo", "arguments": {}}),
        fixture_args=("no-response",),
    )

    assert completed.returncode == 1
    assert trace["status"] == "failed"
    assert trace["exit_code"] == 1
    assert trace["mcp_proxy"]["observation"]["complete"] is True
    assert [event["type"] for event in tool_events(trace)] == ["tool.call.started"]


def test_capture_omits_unsafe_and_over_budget_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_proxy, "MAX_CAPTURE_VALUE_BYTES", 16)
    observer = mcp_proxy._Observer("captured", "2026-08-05T00:00:00Z", ["server"], ".")
    observer.observe(
        "client",
        message(1, "tools/call", {"name": "echo", "arguments": {"padding": "x" * 32}}),
    )
    observer.observe(
        "server",
        b'{"jsonrpc":"2.0","id":1,"result":1e999}\n',
    )
    events, metadata = observer.seal()

    captures = [
        event["data"].get("input") or event["data"].get("output")
        for event in events
        if event["type"].startswith("tool.call.")
    ]
    assert captures == [{"capture": "omitted"}, {"capture": "omitted"}]
    assert observer.capture_omitted["budget"] == 1
    assert observer.capture_omitted["unsafe"] == 1
    assert metadata["complete"] is True


def test_capture_reserves_trace_depth_and_aggregate_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested: object = {}
    for _index in range(mcp_proxy.MAX_TRACE_NESTING_DEPTH - mcp_proxy.CAPTURE_TRACE_DEPTH):
        nested = [nested]
    depth_observer = mcp_proxy._Observer(
        "captured", "2026-08-05T00:00:00Z", ["server"], "."
    )
    with depth_observer.lock:
        capture = depth_observer._capture_locked(nested, input_value=False)
    assert capture == {"capture": "omitted"}
    assert depth_observer.capture_omitted["unsafe"] == 1

    allowed: object = {}
    for _index in range(
        mcp_proxy.MAX_TRACE_NESTING_DEPTH - mcp_proxy.CAPTURE_TRACE_DEPTH - 1
    ):
        allowed = [allowed]
    with depth_observer.lock:
        allowed_capture = depth_observer._capture_locked(allowed, input_value=False)
    assert allowed_capture["capture"] == "captured"

    monkeypatch.setattr(mcp_proxy, "MAX_CAPTURE_TOTAL_BYTES", 12)
    budget_observer = mcp_proxy._Observer(
        "captured", "2026-08-05T00:00:00Z", ["server"], "."
    )
    with budget_observer.lock:
        first = budget_observer._capture_locked({"a": 1}, input_value=True)
        second = budget_observer._capture_locked({"b": 2}, input_value=True)
    assert first["capture"] == "captured"
    assert second == {"capture": "omitted"}
    assert budget_observer.capture_omitted["budget"] == 1


def test_observer_caps_in_flight_and_event_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_proxy, "MAX_IN_FLIGHT", 1)
    monkeypatch.setattr(mcp_proxy, "MAX_EVENTS", 4)
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    observer.observe("client", message(1, "tools/call", {"name": "one", "arguments": {}}))
    observer.observe("client", message(2, "tools/call", {"name": "two", "arguments": {}}))
    observer.observe("server", b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    events, metadata = observer.seal()

    assert len(events) == 3
    assert metadata["complete"] is False
    assert metadata["unobserved_messages"] == 1
    assert "observer_cap" in metadata["reasons"]


def test_oversized_correlation_id_and_stored_argv_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_id = "x" * (mcp_proxy.MAX_CORRELATION_KEY_BYTES + 1)
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    observer.observe(
        "client",
        message(oversized_id, "tools/call", {"name": "echo", "arguments": {}}),
    )
    events, metadata = observer.seal()
    assert [event["type"] for event in events] == ["command.started"]
    assert metadata["reasons"] == ["invalid_tool_call"]

    monkeypatch.setattr(mcp_proxy, "redact_text", lambda value: value)
    stored, _lossy, omitted = mcp_proxy._sanitize_command(
        ["server", "x" * mcp_proxy.MAX_STORED_COMMAND_BYTES, *(["tail"] * 100)]
    )
    assert stored == ["server", "[OMITTED]"]
    assert omitted == 101
    assert len(json.dumps(stored).encode()) < mcp_proxy.MAX_STORED_COMMAND_BYTES


def test_redaction_handles_unicode_newlines_and_key_collisions() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    value, changed = mcp_proxy._redact_json(
        {"message": f'\"snowman ☃\"\n{secret}'},
    )
    assert changed is True
    assert secret not in json.dumps(value)
    assert "snowman ☃" in value["message"]

    collision_source = {secret: 1, "[REDACTED]": 2}
    with pytest.raises(mcp_proxy._RedactionCollision):
        mcp_proxy._redact_json(collision_source)


def test_command_sanitization_masks_even_malformed_url_userinfo() -> None:
    secret = "supersecret"
    malformed = f"https://alice:{secret}@example.com:notaport/path"
    stored, _lossy, omitted = mcp_proxy._sanitize_command(["server", malformed])

    assert omitted == 0
    assert secret not in stored[1]
    assert stored[1] == "https://[REDACTED]@example.com:notaport/path"


def test_trace_trimming_omits_captures_then_complete_call_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = mcp_proxy._Observer("captured", "2026-08-05T00:00:00Z", ["server"], ".")
    observer.observe(
        "client",
        message(1, "tools/call", {"name": "echo", "arguments": {"x": "y" * 1000}}),
    )
    observer.observe(
        "server",
        b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n',
    )
    events, _metadata = observer.seal()
    trace: dict[str, object] = {"events": deepcopy(events)}
    omitted = deepcopy(trace)
    for event in omitted["events"]:
        if event["type"] == "tool.call.started":
            event["data"]["input"] = {"capture": "omitted"}
        elif event["type"] == "tool.call.finished":
            event["data"]["output"] = {"capture": "omitted"}
    omitted_size = len(mcp_proxy.encode_prepared_json(omitted))
    monkeypatch.setattr(mcp_proxy, "TRACE_TARGET_BYTES", omitted_size + 10)
    mcp_proxy._trim_trace(trace, observer)
    started = next(event for event in trace["events"] if event["type"] == "tool.call.started")
    assert started["data"]["input"] == {"capture": "omitted"}
    assert observer.capture_omitted["final_size"] >= 1

    command_only = {"events": [events[0]]}
    command_size = len(mcp_proxy.encode_prepared_json(command_only))
    trace = {"events": deepcopy(events)}
    monkeypatch.setattr(mcp_proxy, "TRACE_TARGET_BYTES", command_size + 1)
    mcp_proxy._trim_trace(trace, observer)
    assert [event["type"] for event in trace["events"]] == ["command.started"]
    assert observer.events_dropped >= 2


def test_trace_trimming_uses_bounded_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = [{"type": "command.started", "data": {}}]
    for index in range(256):
        call_id = f"call-{index}"
        events.extend(
            [
                {
                    "type": "tool.call.started",
                    "data": {"call_id": call_id, "payload": "x" * 100},
                },
                {"type": "tool.call.finished", "data": {"call_id": call_id}},
            ]
        )
    trace: dict[str, object] = {"events": events}
    target_trace = {"events": [events[0], *events[257:]]}
    target_size = len(mcp_proxy.encode_prepared_json(target_trace))
    monkeypatch.setattr(mcp_proxy, "TRACE_TARGET_BYTES", target_size)

    encode_calls = 0
    original_encode = mcp_proxy.encode_prepared_json

    def counted_encode(value: object) -> bytes:
        nonlocal encode_calls
        encode_calls += 1
        return original_encode(value)

    monkeypatch.setattr(mcp_proxy, "encode_prepared_json", counted_encode)
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    mcp_proxy._trim_trace(trace, observer)

    assert encode_calls <= 12
    assert len(original_encode(trace)) <= target_size
    assert observer.events_dropped == 256


def test_oversized_trace_fallback_remains_listable() -> None:
    observer = mcp_proxy._Observer("omitted", "2026-08-05T00:00:00Z", ["server"], ".")
    events: list[dict[str, object]] = [
        {"type": "command.started", "data": {}},
        {"type": "tool.call.started", "data": {"call_id": "call-1"}},
        {"type": "tool.call.finished", "data": {"call_id": "call-1"}},
        {"type": "command.finished", "data": {"status": "passed", "exit_code": 0}},
    ]
    metadata: dict[str, object] = {"shutdown_reason": "child_exit"}
    trace: dict[str, object] = {
        "status": "passed",
        "exit_code": 0,
        "events": events,
        "mcp_proxy": metadata,
    }

    mcp_proxy._collapse_oversized_trace(
        trace,
        observer,
        code="mcp_trace_too_large",
        exit_code=1,
        duration_ms=12,
    )

    assert trace["status"] == "failed"
    assert trace["exit_code"] == 1
    assert metadata["shutdown_reason"] == "mcp_trace_too_large"
    assert [event["type"] for event in events] == [
        "command.started",
        "command.finished",
    ]
    assert events[-1]["data"]["exit_code"] == 1
    assert observer.events_dropped == 2


def test_standard_fd_failure_happens_before_store_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> tuple[int, int]:
        raise OSError("private")

    monkeypatch.setattr(mcp_proxy, "_prepare_standard_fds", fail)
    with pytest.raises(mcp_proxy.McpProxyError, match="mcp_stdio_unavailable"):
        mcp_proxy.proxy_mcp_stdio([sys.executable, "-c", "pass"], root=tmp_path / "store")
    assert not (tmp_path / "store").exists()


def test_standard_fd_duplication_cleans_up_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicates: list[object] = [10, OSError("private")]
    closed: list[int] = []

    def duplicate(_fd: int) -> int:
        result = duplicates.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(mcp_proxy.os, "fstat", lambda _fd: object())
    monkeypatch.setattr(mcp_proxy.os, "dup", duplicate)
    monkeypatch.setattr(mcp_proxy.os, "close", closed.append)
    with pytest.raises(OSError, match="private"):
        mcp_proxy._prepare_standard_fds()
    assert closed == [10]


def test_store_preparation_failure_is_fixed_and_does_not_launch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    root.write_text("not a directory", encoding="ascii")
    with pytest.raises(mcp_proxy.McpProxyError, match="mcp_store_unwritable"):
        mcp_proxy.proxy_mcp_stdio([sys.executable, "-c", "pass"], root=root)


def test_diagnostic_newline_policy_and_exit_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[bytes] = []
    monkeypatch.setattr(
        mcp_proxy,
        "_raw_write",
        lambda _fd, data, _lock: writes.append(bytes(data)),
    )
    mcp_proxy._write_diagnostic(
        2,
        mcp_proxy.threading.Lock(),
        [ord("x")],
        "mcp_relay_failed",
        True,
    )
    assert writes == [b"\ntracord: mcp-proxy failed: mcp_relay_failed\n"]
    assert mcp_proxy._mapped_exit_code(0) == 0
    assert mcp_proxy._mapped_exit_code(7) == 7
    assert mcp_proxy._mapped_exit_code(None) == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(ProcessLookupError(), None), (PermissionError(), 4321)],
)
def test_process_group_capture_has_safe_error_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected: int | None,
) -> None:
    class Process:
        pid = 4321

    def fail(_pid: int) -> int:
        raise failure

    monkeypatch.setattr(mcp_proxy.os, "getpgid", fail, raising=False)
    assert mcp_proxy._capture_process_group(Process()) == expected  # type: ignore[arg-type]


def test_leader_fallback_escalates_without_waiting_or_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 4321

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(mcp_proxy.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(mcp_proxy.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(mcp_proxy, "PROCESS_TERM_GRACE_SECONDS", 0)

    mcp_proxy._terminate_leader_without_reaping(Process())  # type: ignore[arg-type]

    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher policy")
def test_windows_rejects_cwd_hijacking_and_batch_launchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_exe = tmp_path / "private.exe"
    fake_exe.write_bytes(b"not an executable")
    batch = tmp_path / "private.cmd"
    batch.write_text("@echo private", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(mcp_proxy.McpProxyError, match="mcp_server_not_found"):
        mcp_proxy._resolve_executable(["private"])
    with pytest.raises(mcp_proxy.McpProxyError, match="mcp_server_unsafe_launcher"):
        mcp_proxy._resolve_executable([str(batch)])


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute-bit policy")
def test_posix_path_resolution_skips_non_executable_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    allowed = tmp_path / "allowed"
    blocked.mkdir()
    allowed.mkdir()
    (blocked / "server").write_text("not executable", encoding="ascii")
    executable = allowed / "server"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(blocked), str(allowed))))
    assert mcp_proxy._resolve_executable(["server"])[0] == str(executable.resolve())
