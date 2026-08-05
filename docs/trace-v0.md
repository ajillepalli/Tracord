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

Future event types should be additive and should not require changing the top-level trace shape.

## Artifacts

Artifact paths are relative to the directory containing `trace.json`. v0 command traces write:

- `stdout.log`
- `stderr.log`

## Privacy

Tracord defaults to redacting obvious secrets from command output before writing artifacts. Redaction is best effort and not a complete DLP system.
