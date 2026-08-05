# Changelog

All notable changes to Tracord will be documented in this file.

The format is based on Keep a Changelog, and this project will follow semantic versioning once releases begin.

## [Unreleased]

## [0.1.0.0] - 2026-08-05

- Created initial repository scaffold.
- Added minimal Python CLI for recording, listing, and inspecting local command runs.
- Documented `tracord.trace.v0` and added deterministic trace assertions.
- Added portable trace bundle export/import and command replay.
- Added Apache-2.0 license, GitHub templates, architecture docs, roadmap, and contributor workflow policy.
- Added opt-in, isolated Git file-diff capture with structured summaries, redaction, binary privacy defaults, and size limits.
- Added named redaction rules and count-only finding summaries for export safety checks.
- Added a read-only, bounded export preview with deterministic JSON and a strict CI gate.
- Added versioned repository assertion files, bounded safe evaluation, stable CLI result codes, and Git ownership rules that track assertions while excluding runtime runs.
- Added versioned, privacy-safe CI JSON results for record, replay, assert, and list, with strict packaged schemas and one-shot UTF-8 emission.
- Unified text and JSON run listing behind bounded, identity-safe scanning with deterministic ordering and explicit skipped/truncated completeness metadata.
- Hardened replay and inspect with exact run identity, bounded strict trace reads, fixed path-free errors, and fail-closed filesystem verification.
- Rejected redirected or unverifiable run stores across record, import, replay, inspect, assertions, and list; store failures remain fixed and path-free at the CLI.
- Made legacy timeout content assertions indeterminate when decode replacement provenance is unknown.
- Bounded text and JSON list output together at 1,000 runs with explicit incompleteness diagnostics.
- Added protocol-neutral tool-call start/finish events with explicit capture states, lifecycle validation, privacy-safe diagnostics, and JSON Schema coverage.
- Added a byte-transparent MCP stdio proxy with bounded tool-call observation, explicit capture policies, atomic trace publication, and cross-platform process-tree cleanup.
