# Tracord Trace v0

`tracord.trace.v0` is the first local trace contract. It is intentionally small: command runs are the first supported trace kind, while the `events` array leaves room for future model calls, tool calls, approvals, file diffs, and replay steps.

## Required Fields

- `schema_version`: must be `tracord.trace.v0`.
- `run_id`: stable identifier for this run.
- `kind`: currently `command`.
- `status`: one of `passed`, `failed`, or `timeout`.
- `command`: argument vector that was executed.
- `cwd`: working directory used for the command.
- `started_at`: UTC ISO-8601 timestamp.
- `finished_at`: UTC ISO-8601 timestamp.
- `duration_ms`: non-negative integer duration.
- `exit_code`: integer process exit code, or `null` when unavailable.
- `timed_out`: whether the command exceeded its timeout.
- `redacted`: whether stdout and stderr artifacts were redacted.
- `artifacts`: relative filenames for trace artifacts.
- `events`: ordered event objects.

## Events

Every event has:

- `type`: machine-readable event type.
- `at`: UTC ISO-8601 timestamp.
- `data`: event-specific object.

The command recorder emits:

- `command.started`
- `command.finished`
- `file.diff` when Git file-change capture is requested

Unknown event types remain additive and keep the open event envelope. Tracord
reserves only the exact names `tool.call.started` and `tool.call.finished` for
the tool-call contract below; names such as `tool.call.progress` remain unknown
and open.

### Tool calls

`tool.call.started` records a protocol-neutral call identifier, tool name, and
input capture declaration:

```json
{
  "type": "tool.call.started",
  "at": "2026-08-05T12:00:00Z",
  "data": {
    "call_id": "call-1",
    "name": "get_weather",
    "input": {
      "capture": "captured",
      "value": {"location": "New York"}
    }
  }
}
```

`data` has exactly `call_id`, `name`, and `input`. Both strings must be
non-empty and are not trimmed or normalized. `input` has one of these exact
shapes:

- `{"capture": "captured", "value": {...}}` stores the input object as supplied.
- `{"capture": "redacted", "value": {...}}` declares that the producer changed at least one sensitive value.
- `{"capture": "omitted"}` stores no input value.

Captured and redacted input values must be JSON objects. An argument-free call
uses an empty captured object. `captured` does not mean secret-free, and the
schema cannot prove that a redacted value is safe.

`tool.call.finished` records the result independently from the input capture
choice:

```json
{
  "type": "tool.call.finished",
  "at": "2026-08-05T12:00:01Z",
  "data": {
    "call_id": "call-1",
    "outcome": "succeeded",
    "duration_ms": 1000,
    "output": {
      "capture": "redacted",
      "value": {"content": "[REDACTED]"}
    }
  }
}
```

`data` has exactly `call_id`, `outcome`, `duration_ms`, `output`, and the
conditional `error_type` field. Outcomes are `succeeded`, `failed`,
`cancelled`, and `timeout`. Duration is a non-negative JSON integer no greater
than `2^53 - 1`; mathematically integral forms such as `1.0` are valid, while
booleans and fractions are not.

Output capture uses the same three declarations as input. Captured and redacted
outputs require the `value` key, but its value may be any JSON value, including
`null`, `false`, `0`, an empty string, array, or object. Omitted output forbids
the `value` key.

A failed call requires `error_type`; every other outcome forbids it:

```json
{
  "type": "tool.call.finished",
  "at": "2026-08-05T12:00:01Z",
  "data": {
    "call_id": "call-1",
    "outcome": "failed",
    "duration_ms": 1000,
    "output": {"capture": "omitted"},
    "error_type": "Tool.ExecutionFailed"
  }
}
```

`error_type` is 1-64 ASCII characters. Its first character is a letter; every
remaining character is a letter, digit, underscore, period, or hyphen.
Producers should keep it low-cardinality and must not place raw exception text,
paths, arguments, or results in it.

Tool-call lifecycle is evaluated in event-array order, not timestamp order:

- A start identifier is unique within its trace.
- A finish references an earlier start and may occur once.
- Concurrent calls may interleave and finish in any order.
- A start with no finish is valid and represents an interrupted trace.
- An orphan or early finish, duplicate start, or duplicate finish is invalid.
- Event timestamps need not be monotonic.
- Tool outcome does not constrain the command trace's top-level status; an agent may recover from a failed call.

Known tool-call `data`, `input`, and `output` objects reject unknown fields. The
outer event envelope stays open. The Python validator checks all event structure
before lifecycle and emits index-based errors that do not echo tool names,
identifiers, arguments, results, or error classifications. Consumers of raw
third-party JSON Schema diagnostics must sanitize them separately.

Captured values retain the trace-wide numeric and nesting limits described
below.

Safe trace readers reject JSON with more than 256 simultaneously open
containers, including the root. This canonical bound applies to list, inspect,
replay, assertions, and captured tool values. All numbers must be finite;
integers must remain within `-(2^53 - 1)` through `2^53 - 1`.

## Artifacts

Artifact paths are relative to the directory containing `trace.json`. v0 command traces write:

- `stdout.log`
- `stderr.log`

A trace with a captured Git patch also writes `changes.patch` and references it as `artifacts.file_diff`.

## Optional File Changes

When capture is requested, `file_changes` records a status, structured changed-file list, configured size limit, and optional patch metadata. Status is one of `captured`, `unchanged`, `skipped`, `omitted`, or `error`. The same object is emitted as the `file.diff` event data.

This field is additive: traces recorded without `--capture-diff` do not contain it and remain valid `tracord.trace.v0` traces.

## Optional Recording Metadata

Current recorders also write:

- `output_encoding`: the platform-preferred encoding used to decode child bytes.
- `decode_replacement`: `stdout` and `stderr` values of `none`, `present`, or
  `unknown`, describing whether decoder replacement was observed.
- `store_identity_verified`: whether the recording filesystem exposed stable
  identity evidence for the store during publication.

These fields are additive for trace-v0 compatibility. Missing decode metadata is
treated as `none` for completed legacy traces and `unknown` for legacy timeout
traces; missing store identity metadata is treated as unverified.
Decoder replacement does not prove byte integrity or a universal encoding.
Recorder text normalizes CRLF and CR line endings to LF after decoding.

## Privacy

Tracord defaults to redacting obvious secrets from command output before writing artifacts. Redaction is best effort and not a complete DLP system.

Git patch capture is opt-in. By default, textual patches receive the same best-effort redaction and binary payloads are omitted. `--no-redact` stores raw text and binary patch data. See [file-diff-capture.md](file-diff-capture.md).
