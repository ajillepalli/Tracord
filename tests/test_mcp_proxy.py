from __future__ import annotations

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
from tracord.schema import validate_trace


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


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
    assert trace["mcp_proxy"]["shutdown_reason"] == "mcp_spawn_failed"


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
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","result":{},"params":{"name":"echo","arguments":{}}}\n',
    )
    events, metadata = observer.seal()

    assert [event["type"] for event in events] == ["command.started"]
    assert metadata["unobserved_messages"] == 2
    assert metadata["reasons"] == ["unsupported_envelope"]


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
    omitted_size = len(json.dumps(omitted, separators=(",", ":")).encode())
    monkeypatch.setattr(mcp_proxy, "TRACE_TARGET_BYTES", omitted_size + 10)
    mcp_proxy._trim_trace(trace, observer)
    started = next(event for event in trace["events"] if event["type"] == "tool.call.started")
    assert started["data"]["input"] == {"capture": "omitted"}
    assert observer.capture_omitted["final_size"] >= 1

    command_only = {"events": [events[0]]}
    command_size = len(json.dumps(command_only, separators=(",", ":")).encode())
    trace = {"events": deepcopy(events)}
    monkeypatch.setattr(mcp_proxy, "TRACE_TARGET_BYTES", command_size + 1)
    mcp_proxy._trim_trace(trace, observer)
    assert [event["type"] for event in trace["events"]] == ["command.started"]
    assert observer.events_dropped >= 2


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
