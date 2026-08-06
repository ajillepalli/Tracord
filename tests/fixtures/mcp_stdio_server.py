"""Deterministic newline-delimited MCP fixture used by proxy integration tests."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time


MODE = sys.argv[1] if len(sys.argv) > 1 else "messages"

if MODE == "raw-echo":
    while chunk := sys.stdin.buffer.read1(64 * 1024):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    raise SystemExit(0)

if MODE == "exit":
    raise SystemExit(int(sys.argv[2]))

if MODE == "server-request":
    sys.stdout.buffer.write(
        b'{"jsonrpc":"2.0","id":1,"method":"sampling/createMessage","params":{}}\n'
    )
    sys.stdout.buffer.flush()

if MODE == "spawn-descendant":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    raise SystemExit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)

if MODE == "flood":
    for _index in range(256):
        sys.stdout.buffer.write(b"x" * (64 * 1024))
        sys.stdout.buffer.flush()
    raise SystemExit(0)


write_lock = threading.Lock()
workers: list[threading.Thread] = []


def send(message: object) -> None:
    payload = json.dumps(message, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    with write_lock:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def answer(request: dict[str, object]) -> None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                },
            }
        )
    elif method == "server/discover":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                },
            }
        )
    elif method == "tools/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]},
            }
        )
    elif method == "tools/call" and isinstance(params, dict):
        name = params.get("name")
        arguments = params.get("arguments", {})
        if isinstance(arguments, dict):
            time.sleep(float(arguments.get("delay", 0)))
        if name == "protocol_error":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "fixture protocol secret"},
                }
            )
        elif name == "fail":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "fixture failure"}],
                        "isError": True,
                    },
                }
            )
        elif name == "stderr":
            sys.stderr.buffer.write(b"fixture stderr without newline")
            sys.stderr.buffer.flush()
            send({"jsonrpc": "2.0", "id": request_id, "result": {"content": []}})
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": "ok"}], "echo": arguments},
                }
            )


for line in sys.stdin.buffer:
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        continue
    if not isinstance(message, dict):
        continue
    if MODE == "no-response":
        continue
    if message.get("method") == "notifications/cancelled":
        continue
    if "id" not in message or "method" not in message:
        continue
    worker = threading.Thread(target=answer, args=(message,))
    worker.start()
    workers.append(worker)

for worker in workers:
    worker.join()

if MODE == "hang-after-eof":
    time.sleep(60)
