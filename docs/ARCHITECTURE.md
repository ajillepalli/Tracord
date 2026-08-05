# Architecture

Tracord is a local-first CLI for recording, inspecting, exporting, importing, replaying, and asserting command traces.

## Current Shape

```
src/tracord/
  cli.py          # argparse command surface
  recorder.py     # command execution and trace writing
  storage.py      # local run directory helpers
  schema.py       # lightweight trace validation
  assertions.py   # deterministic assertion evaluation
  bundle.py       # portable .tracord.zip import/export
  export_preview.py # bounded, read-only export safety inspection
  replay.py       # command replay
  redaction.py    # named redaction rules and count-only summaries
  paths.py        # safe relative path handling
  git_capture.py  # isolated Git before/after snapshots
```

Recorded runs live under `.tracord/runs/<run-id>/` by default.

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

## File Change Model

Git diff capture uses temporary indexes and a temporary object directory to compare the working tree immediately before and after a command. It does not mutate the repository index, refs, working tree, or persistent object database.

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

## Near-Term Direction

- Add model-call and tool-call event types.
- Add file-diff capture for coding-agent workflows.
- Add redaction profiles and export previews.
- Add adapters for common agent runners and MCP tools.
