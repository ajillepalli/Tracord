# Architecture

Tracord is a local-first CLI for recording, inspecting, exporting, importing, replaying, and asserting command traces.

## Current Shape

```
src/tracord/
  cli.py          # argparse command surface
  recorder.py     # command execution and trace writing
  storage.py      # local run directory helpers
  schema.py       # lightweight trace validation
  assertion_files.py # strict repository assertion-file loading
  assertions.py   # deterministic assertion evaluation
  bundle.py       # portable .tracord.zip import/export
  export_preview.py # bounded, read-only export safety inspection
  replay.py       # command replay
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
Stdout and stderr are scanned completely as strict UTF-8 with 10 MiB per-file
and 16 MiB aggregate limits; a match does not bypass size, identity, or snapshot
verification.

The CLI keeps result classes stable: `0` passes, `1` represents trace or
expectation failure, and `2` represents invocation, assertion-file, value, or
case-selection errors. Public diagnostics are closed fixed codes and never
include untrusted IDs, paths, values, content, or raw exceptions.

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

- Add model-call and tool-call event types.
- Add adapters for common agent runners and MCP tools.
