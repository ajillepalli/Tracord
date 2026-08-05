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

The initial command recorder emits:

- `command.started`
- `command.finished`
- `file.diff` when Git file-change capture is requested

Future event types should be additive and should not require changing the top-level trace shape.

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
treated as `unknown`; missing store identity metadata is treated as unverified.
Decoder replacement does not prove byte integrity or a universal encoding.
Recorder text normalizes CRLF and CR line endings to LF after decoding.

## Privacy

Tracord defaults to redacting obvious secrets from command output before writing artifacts. Redaction is best effort and not a complete DLP system.

Git patch capture is opt-in. By default, textual patches receive the same best-effort redaction and binary payloads are omitted. `--no-redact` stores raw text and binary patch data. See [file-diff-capture.md](file-diff-capture.md).
