# Architecture

Tracord is a local-first CLI for recording, inspecting, exporting, importing, replaying, and asserting command traces.

## Current Shape

```
src/tracord/
  cli.py          # argparse command surface
  recorder.py     # command execution and trace writing
  mcp_proxy.py    # byte-transparent stdio relay and bounded MCP observation
  storage.py      # local run directory helpers
  schema.py       # lightweight trace validation
  assertion_files.py # strict repository assertion-file loading
  assertions.py   # deterministic assertion evaluation
  bundle.py       # portable .tracord.zip import/export
  export_preview.py # bounded, read-only export safety inspection
  replay.py       # command replay
  trace_access.py # exact bounded trace resolution and parsing
  run_listing.py  # shared bounded text/JSON listing
  result_codes.py # leaf-owned CI result constants and error vocabularies
  ci_output.py    # CI projections, result builders, schemas, and one-shot emitter
  redaction.py    # named redaction rules and count-only summaries
  paths.py        # safe relative path handling
  git_capture.py  # isolated Git before/after snapshots
```

Recorded runs live under `.tracord/runs/<run-id>/` by default. Versioned,
repository-owned assertion cases live at `.tracord/assertions.json`.

## Trace Model

The current trace contract is `tracord.trace.v0`. A command trace has:

- top-level run metadata
- the executed command vector
- relative artifact paths
- ordered events
- stdout and stderr artifacts

See [trace-v0.md](trace-v0.md) and [../schemas/trace-v0.schema.json](../schemas/trace-v0.schema.json).

Trace-v0 reserves protocol-neutral `tool.call.started` and
`tool.call.finished` events. JSON Schema owns each event's closed `data` and
capture-object structure. The Python validator first validates every event's
structure, then performs a linear second pass over call identifiers to enforce
start/finish ordering and uniqueness. Unknown event names retain the open v0
envelope, so adapters can add events without a top-level schema change.

Capture declarations make stored input and output handling explicit but do not
perform capture or redaction. Runner and MCP adapters remain responsible for
policy, redaction, and translating protocol-specific failures into stable,
low-cardinality classifications. Tool outcomes do not determine the enclosing
command status.

## MCP Stdio Proxy

`mcp-proxy` launches one server without a shell and places three independent
binary relays between the MCP client and child process. Client stdin and server
stdout are also observed as newline-delimited messages, but observation cannot
change accepted protocol bytes. Server stderr is relayed and never recorded.
The generated `stdout.log` and `stderr.log` are intentionally empty, with this
policy declared in additive `mcp_proxy` metadata.

The observer is handshake-neutral and recognizes only `tools/call`, matching
responses, and cancellation notifications. JSON-RPC identifiers are typed and
direction-scoped; trace-local call identifiers avoid storing protocol IDs.
Malformed, unsupported, or messages larger than 1 MiB continue over the wire
and make observation explicitly incomplete. In-flight calls, events, captures,
stored argv, and the final trace all have independent bounds.

The proxy and command recorder share identity-checked run creation, exclusive
artifact writes, and atomic `trace.json` publication. POSIX children run in a
new process group. Windows children are created suspended, assigned to a
verified kill-on-close Job Object, and resumed only after membership succeeds;
unexpected initial-thread state fails launch closed. Client EOF and intercepted
signals use bounded graceful shutdown and process-tree cleanup. See
[mcp-stdio-proxy.md](mcp-stdio-proxy.md).

## Bundle Model

Portable bundles are zip archives with a `.tracord.zip` suffix. Import rejects unsafe paths and writes only under the target run directory.

See [bundle-v0.md](bundle-v0.md).

Export preview is separate from archive writing. It opens referenced files with
bounded read-only descriptors, rejects non-regular and unsafe paths, checks file
identity around reads, and emits count-only results. This separation guarantees
that preview has no explicit filesystem writes and does not create a store,
output directory, or bundle. Read-only opens can still update access times, and
cross-platform filesystem races remain a documented best-effort boundary.
Normal export shares the same run-ID, real-directory, regular-file, and stable
identity preflight so preview does not certify inputs that the writer rejects.
Export streams from verified descriptors, while import creates files through
no-follow descriptors beneath validated real directories.

Each opened real-file preview payload reports `identity_verified` independently
from export readiness. A source can still be scanned when inode identity is
unavailable so that findings are not lost, but strict gating fails closed on
that uncertainty. The
`--allow-incomplete-scan` option cannot suppress an identity-verification
failure. `export_would_succeed` continues to describe the current bundle
writer's preflight rather than claiming that preview proved identity.

## Assertion Model

`tracord.assertions.v0` is a frozen repository format with exact top-level keys
`schema_version` and `cases`. Each named case uses an explicit allowlist of
status, exit-code, output-containment, duration, and timeout expectations. The
loader rejects duplicate keys, non-finite numbers, oversized integers,
case-folding name collisions, invalid UTF-8, byte-order marks, unexpected
fields, and empty cases. It validates every case in deterministic order before
selecting one.

Assertion files are descriptor-bounded to 1 MiB and 256 cases. Containment
values are capped at 65,536 UTF-8 bytes. Evaluation resolves an exact portable
run ID without creating storage, reads a regular single-link `trace.json` with
a 16 MiB limit, and requires the requested ID, directory name, and trace ID to
agree. Referenced artifacts must remain contained regular single-link files.
Stdout and stderr receive strict UTF-8 coverage with 10 MiB per-file and 16 MiB
aggregate limits. A match in verified covered bytes satisfies its field, while
an uncovered negative result is `scan_incomplete`; a match never bypasses
identity or snapshot verification.

The CLI keeps result classes stable: `0` passes; `1` represents trace,
expectation, evaluation, or internal failure; and `2` represents recognized
invocation, assertion-file, value, or case-selection errors. Public diagnostics are closed fixed codes and never
include untrusted IDs, paths, values, content, or raw exceptions.

Assertions now share exact directory resolution, descriptor-bounded trace reads,
strict JSON parsing, and final identity verification with replay and inspect.
Artifact evaluation retains its assertion-specific byte limits and failure
locations after the common trace has been established.

The v0 assertion code vocabulary is closed. Invocation and file errors are
`invalid_run_id`, `assertion_mode_conflict`, `assertion_no_expectations`,
`assertion_value_invalid`, `assertion_file_missing`,
`assertion_file_unreadable`, `assertion_file_not_regular`,
`assertion_file_changed`, `assertion_file_too_large`, `assertion_file_bom`,
`assertion_file_invalid_utf8`, `assertion_file_duplicate_key`,
`assertion_file_invalid_json`, `assertion_file_schema_invalid`, and
`case_not_found`. Evaluation errors are `run_not_found`, `trace_missing`,
`trace_unreadable`, `trace_invalid`, `run_identity_mismatch`,
`run_identity_unverifiable`, `artifact_unreadable`, `artifact_invalid_utf8`,
`artifact_decode_replaced`, `artifact_decode_unknown`, `artifact_changed`,
`assertion_mismatch`, and `scan_incomplete`. Adding a public code or expectation
field requires a new assertion format version.

## CI Result Model

`record`, `replay`, `assert`, and `list` each have a separate frozen v0 result
schema. `result_codes.py` owns shared constants without importing runtime code;
`ci_output.py` owns privacy projections, cross-field validation, compact sorted
serialization, and the one-shot stdout emitter. Runtime modules raise typed,
path-free errors and the CLI maps them into the command-specific vocabulary.

Text and JSON list output share `run_listing.py`. It retains only bounded valid
ASCII/portable candidates, reads single-link traces through verified descriptors,
applies the canonical trace schema, and records skipped versus truncated work.
The scanner is read-only when the store is missing. See
[ci-json-output.md](ci-json-output.md) for the wire contract and exact limits.

`trace_access.py` validates run IDs before path construction, resolves exact
on-disk spelling, distinguishes detected replacement from unavailable identity,
and re-verifies the store, run directory, descriptor, and trace ID after a
bounded read. Replay fails closed before executing trace-controlled commands.
Inspect uses the same access path but remains a deliberately sensitive raw view.

## File Change Model

Git diff capture uses temporary indexes and a temporary object directory to compare the working tree immediately before and after a command. It does not mutate the repository index, refs, working tree, or persistent object database.

For an active store inside the repository, synthetic snapshots remove only that
store's `runs` subtree after preserving the store-equals-repository guard.
Stores outside or above the repository need no repository-relative exclusion.
The repository ignore rules apply at any depth: `.tracord/runs` remains runtime
owned, while `.tracord/assertions.json` is re-included for review and capture.
Tracked changes elsewhere, including a foreign nested store, remain observable.

See [file-diff-capture.md](file-diff-capture.md).

## Redaction Model

Redaction rules have stable names, explicit replacement strategies, and gating
metadata. `redact_text` applies them in a fixed order to preserve recorded output.
The count-only summary API inspects each rule independently, so overlapping rules
may count the same substring more than once. It never retains matched values,
locations, lengths, or excerpts.

Named assignments whose value is exactly `[REDACTED]` are tracked as already
redacted and do not count as findings. The broad encoded-secret candidate is
advisory because common Git object IDs and other repository content can match it.

## Design Constraints

- Local-first by default.
- No hosted service dependency.
- Deterministic checks before LLM-as-judge checks.
- Relative artifact paths only.
- Import and replay must be conservative because traces may contain sensitive or hostile data.
- Filesystem identity is tri-state: unavailable evidence is never treated as a verified match.

On Windows, `st_ctime` is creation time rather than POSIX inode-change time, so
snapshot checks cannot observe every metadata transition. Some Windows
filesystems also expose incomplete or unstable link counts. Tracord combines
mode, size, mtime, ctime, link count, containment, reparse-point checks, and
descriptor/path identity, but these checks reduce rather than eliminate
cross-platform race windows.

## Near-Term Direction

- Add model-call event types.
- Add adapters for common agent runners and MCP tools.
