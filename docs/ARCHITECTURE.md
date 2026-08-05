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
  replay.py       # command replay
  redaction.py    # best-effort output redaction
  paths.py        # safe relative path handling
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
