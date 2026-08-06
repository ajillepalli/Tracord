# Roadmap

Tracord is pre-1.0. The roadmap is intentionally biased toward reliability primitives over broad framework coverage.

## v0.1

- Command recording.
- Trace inspection.
- Deterministic assertions.
- Portable bundle export/import.
- Command replay.

## v0.2

- File diff capture. (implemented)
- Export preview with redaction summary. (implemented)
- Assertion files checked into repositories. (implemented)
- Versioned, privacy-safe CI JSON for record, replay, assert, and list. (implemented)

## v0.3

- Tool-call event schema. (implemented)
- MCP stdio proxy recording prototype. (implemented)
- Approval and permission events.
- Policy checks for unsafe tool use.

## v0.4

- Local trace viewer.
- Side-by-side replay comparison.
- Framework adapters for popular coding-agent workflows.

## Open Questions

- How much prompt content should be recorded by default?
- Which redaction profiles are safe enough for public bundle sharing?
- How should Tracord represent partially replayable agent runs?
- What belongs in the core trace format versus framework-specific extensions?
- How should a future command list or run all cases without weakening deterministic validation?
- When should bundle export require verified filesystem identity on every supported platform?
