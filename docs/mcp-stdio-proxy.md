# MCP stdio proxy

Tracord can sit between an MCP client and one local stdio server, relaying the
protocol while recording bounded `tool.call.started` and `tool.call.finished`
events in a normal command trace.

## Configure a client

Replace the client's server command with Tracord and place the original command
after the first bare `--`:

```text
tracord mcp-proxy [--store PATH] [--name NAME]
                  [--tool-data omitted|redacted|captured]
                  -- SERVER [ARGS...]
```

For example:

```bash
tracord mcp-proxy --name filesystem -- python servers/filesystem.py --read-only
```

The first separator belongs to Tracord. Any later `--` and all remaining
arguments are passed to the server verbatim. On Windows, invoke an `.exe` or
`.com` executable directly; batch-file launchers are rejected. Tracord does not
use a shell or search the current directory for a bare executable name.

The proxy writes no success summary because stdout belongs exclusively to MCP.
Use `tracord list` to find the resulting run and `tracord inspect <run-id>` to
read its trace. Proxy traces are not replayable because replaying a server
without its MCP client would not reproduce the recorded exchange.

## Choose a capture policy

`--tool-data omitted` is the default and stores neither tool arguments nor
results. `redacted` walks parsed JSON keys and values with Tracord's
best-effort secret rules. If redaction would produce duplicate keys, the value
is omitted. `captured` stores raw parsed arguments and results and can therefore
store credentials, source code, personal data, or other secrets.

All modes leave MCP stdout and server stderr out of trace artifacts. Child argv
receives best-effort redaction for trace storage, but the original argv is
passed to the child. The inherited environment is never recorded.

## Relay and observation limits

Relay has no message-size limit. Tracord buffers at most 1 MiB plus one byte per
direction for observation; larger messages, malformed JSON, duplicate-key
objects, invalid UTF-8, and unsupported envelopes are still forwarded. Their
payloads are not stored, and `mcp_proxy.observation.complete` becomes false with
count-only reasons.

JSON parsing, redaction, and capture run on one background observer so they do
not hold up protocol forwarding. The observer queue is FIFO and bounded to 64
messages and 2,097,154 bytes (two 1 MiB-plus-one observation buffers), including
the message currently being processed. This preserves causal
request-before-response ordering without making an unbounded copy of protocol
traffic. If the queue is saturated, exact wire bytes still forward, but the
affected messages are not captured and observation reports the count-only
`observer_queue_overflow` reason. A recoverable item failure reports
`observer_error` and the worker continues; a worker that cannot continue reports
`observer_worker_failed` for all affected messages. Wire forwarding continues in
both cases. Queue availability depends on observer scheduling; the memory bound
and incomplete-count reporting are deterministic, while capture completeness
under sustained saturation is best effort. OS reads and writes remain subject
to normal transport latency.

Observation retains at most 4,096 in-flight calls and 10,000 event slots.
Captured values are limited to 1 MiB each and 8 MiB in aggregate. Tracord may
omit captures or complete call pairs to keep `trace.json` below its 16 MiB read
limit. These losses are reported in observation and capture counters.

The prototype observes newline-delimited stdio traffic. It is handshake-neutral
and recognizes `tools/call`, JSON-RPC results/errors, and
`notifications/cancelled`; initialize, discovery, notifications, and
server-originated requests pass through without lifecycle events. HTTP
transports, MCP tasks, schema validation, approvals, and unsafe-tool policy are
outside this adapter's scope.

## Shutdown behavior

After client EOF, the server gets two seconds to exit before Tracord cleans up
its process tree. SIGINT, and SIGTERM/SIGHUP on POSIX, use the same bounded
finalization path. An independently observed child failure is returned to the
client. Cleanup initiated after client disconnect does not invent a child
failure, but observation is marked incomplete.

On Windows, Tracord creates the server with its primary thread suspended. It
assigns the process to a kill-on-close Job Object, verifies membership, and
resumes the sole initial thread only after both checks succeed. If Job setup,
thread discovery, or resume support is unavailable or returns an unexpected
state, launch fails closed with `mcp_spawn_failed`; server code is not allowed
to run outside the Job. POSIX servers continue to start in a new session and
verified process group.

The trace's top-level `exit_code` is the Tracord proxy process result so failed
runs remain valid list entries. The server's unmodified result is retained as
`mcp_proxy.raw_child_exit_code`.
