# Security Policy

Tracord is intended to inspect agent activity, so it may process sensitive prompts, tool inputs, file paths, command output, environment details, trace bundles, and eventually model/tool-call data.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities or accidental exposure of private trace data. Use GitHub private vulnerability reporting when available, or open a minimal public issue that does not disclose exploit details and asks for a private contact path.

Useful reports include:

- what data or capability is exposed
- whether command execution, replay, archive import, export, redaction, or path handling is involved
- the commit or version tested
- a minimal reproduction that does not include secrets or private prompts
- whether the issue can affect imported bundles from untrusted sources

## Security expectations

- Default to local storage.
- Redact obvious secrets before exporting traces.
- Treat redaction summaries as best-effort signals, not proof that content is safe to share.
- Treat shell commands, network destinations, file paths, file diffs, prompts, and tool inputs as sensitive data.
- Avoid sending trace data to hosted services unless a user explicitly opts in.
- Make unsafe or destructive tool activity visible in the trace.
- Reject archive paths that are absolute, contain parent traversal, use Windows drives, or use backslashes.
- Treat replay as execution of untrusted historical instructions unless the run is known and reviewed.

Redaction summaries expose rule names and counts only. They do not include matched
values, offsets, lengths, or excerpts. Named assignments containing an
already-redacted placeholder are reported separately from live findings;
placeholders produced by full-match rules cannot be attributed afterward. Broad
encoded-secret candidates are advisory to avoid treating common repository
identifiers as confirmed secrets.

## Scope

In scope:

- Secret detection and redaction.
- Trace export and import.
- Replay behavior.
- Tool-call authorization and policy enforcement.
- MCP integration.
- Local file and shell command capture.
- Path traversal in bundles or artifacts.
- Private prompt, command output, or trace artifact disclosure.

Out of scope:

- Scanner-only reports with no working reproduction.
- Issues requiring already-compromised local administrator access.
- Test bundles or fixtures clearly marked as intentionally unsafe examples.
- Social engineering against maintainers or contributors.

## Supported versions

Tracord is pre-1.0. Security fixes land on `main`; there are no long-term support branches yet.
