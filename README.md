<p align="center">
  <img src="docs/images/tracord-logo.svg" alt="Tracord" width="520" />
</p>

<p align="center">
  <a href="docs/ARCHITECTURE.md"><strong>Architecture</strong></a> &middot;
  <a href="docs/ROADMAP.md"><strong>Roadmap</strong></a> &middot;
  <a href="CONTRIBUTING.md"><strong>Contributing</strong></a> &middot;
  <a href="SECURITY.md"><strong>Security</strong></a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" /></a>
  <img alt="Python CLI" src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" />
  <img alt="Local first" src="https://img.shields.io/badge/local--first-agent%20traces-111827.svg" />
</p>

Tracord is a local-first flight recorder for agentic software. It records command and agent-run evidence, writes portable traces, supports deterministic assertions, and turns known failures into replayable regression checks.

The project is aimed at developers building coding agents, MCP tools, and other tool-using AI systems who need to answer: what did the agent do, what changed, what failed, what did it cost, and can we replay or test that behavior later?

## What's here

The current MVP includes:

- **Command recording** - wrap a local command and capture status, timing, stdout, stderr, and trace metadata.
- **Git file-change capture** - opt in to an isolated before/after working-tree diff with structured file metadata.
- **Trace contract** - `tracord.trace.v0`, documented in [docs/trace-v0.md](docs/trace-v0.md) with a JSON Schema in [schemas/trace-v0.schema.json](schemas/trace-v0.schema.json).
- **Repository assertions** - check status, exit code, timeout behavior, duration, and artifact contents from versioned, reviewable cases.
- **Portable bundles** - export and import `.tracord.zip` bundles with path traversal protections.
- **Safe export preview** - inspect bundle contents with count-only secret findings and a strict CI gate before writing.
- **Replay** - re-run the command from a recorded trace and store the replay as a new run.
- **Local-first storage** - traces are stored under `.tracord/runs/` by default.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit together.

## Run it locally

```bash
git clone https://github.com/ajillepalli/Tracord.git
cd Tracord
python -m venv .venv
.venv/Scripts/activate  # or source .venv/bin/activate on macOS/Linux
python -m pip install -e .
tracord --version
```

You can also run from source without installing:

```bash
cd src
python -m tracord.cli --version
```

## Quick workflow

```bash
tracord record -- python -c "print('hello from tracord')"
tracord record --capture-diff -- python agent.py
tracord list
tracord inspect <run-id>
tracord assert <run-id> --status passed --stdout-contains tracord
tracord assert <run-id> --case smoke
tracord export <run-id> --preview
tracord export <run-id> --preview --json --fail-on-findings
tracord export <run-id> --output hello.tracord.zip
tracord import hello.tracord.zip
tracord replay <run-id>
```

Recorded runs are stored locally under `.tracord/runs/`. Repository assertion
cases live in `.tracord/assertions.json`, which is intentionally not ignored by
Git. Exported bundles are zip archives with a `.tracord.zip` suffix.

## Repository assertion files

The `tracord.assertions.v0` format stores named deterministic checks alongside
the repository:

```json
{
  "schema_version": "tracord.assertions.v0",
  "cases": {
    "smoke": {
      "status": "passed",
      "exit_code": 0,
      "stdout_contains": "ready",
      "no_timeout": true
    }
  }
}
```

`tracord assert <run-id> --case smoke` loads the default file from the selected
store. `--file PATH` selects another file, requires `--case`, and resolves a
relative path from the current directory. File mode cannot be mixed with inline
expectation flags. Both modes require at least one expectation.

Assertion files are strict UTF-8 JSON and are limited to 1 MiB and 256 cases.
Case names use portable ASCII letters, digits, `.`, `_`, and `-`, are unique
under ASCII case folding, and contain at most 128 characters. Containment values
are limited to 65,536 UTF-8 bytes. Evaluation reads `trace.json` through a
bounded 16 MiB descriptor and scans regular, single-link stdout and stderr
artifacts with 10 MiB per-file and 16 MiB aggregate coverage. A positive match
within verified coverage passes that field; a negative result that reaches a
coverage limit reports `scan_incomplete` rather than a plain mismatch.

Assertion commands return `0` when every expectation passes, `1` for a trace or
expectation failure, and `2` for invalid mode, values, files, or case selection.
Diagnostics use fixed codes and do not echo assertion contents, paths, run IDs,
unvalidated case input, or raw filesystem errors. Validated repository case
names may appear only inside bounded logical schema locations.

File diff capture is opt-in because patches may contain sensitive source and data. See [docs/file-diff-capture.md](docs/file-diff-capture.md) for scope, redaction behavior, and limits.

Export preview reads the raw trace and referenced artifacts without writing a
bundle. Its strict gate returns exit code `3` for live secret findings, a
blocked or unknown export preflight, or incomplete scan coverage. See
[docs/bundle-v0.md](docs/bundle-v0.md) for limits and security semantics.

## Testing

```bash
python -m pip install -e .
python -m pytest
```

The current suite covers recording, assertions, schema validation, bundle import/export, replay, and unsafe path rejection.

## Repository shape

```text
src/tracord/              # CLI and core recording code
tests/                    # unit tests
docs/                     # architecture, roadmap, trace and bundle docs
schemas/                  # machine-readable trace schemas
.github/                  # issue templates and PR checklist
.tracord/assertions.json  # repository-owned assertion cases
.tracord/runs/            # local runtime store, ignored by Git
```

## Contributing

Tracord uses a review-heavy workflow for meaningful changes: planning, design review, engineering review, adversarial review, issue filing, implementation, PR, another adversarial review, fixes, and merge. See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## Security

Tracord may process sensitive prompts, command output, file paths, tool inputs, and trace artifacts. Do not publish secrets or private traces in public issues. See [SECURITY.md](SECURITY.md).

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
