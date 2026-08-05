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
- **Trace contract** - `tracord.trace.v0`, documented in [docs/trace-v0.md](docs/trace-v0.md) with a JSON Schema in [schemas/trace-v0.schema.json](schemas/trace-v0.schema.json).
- **Deterministic assertions** - check status, exit code, timeout behavior, duration, and artifact contents.
- **Portable bundles** - export and import `.tracord.zip` bundles with path traversal protections.
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
tracord list
tracord inspect <run-id>
tracord assert <run-id> --status passed --stdout-contains tracord
tracord export <run-id> --output hello.tracord.zip
tracord import hello.tracord.zip
tracord replay <run-id>
```

Recorded runs are stored locally under `.tracord/runs/`. Exported bundles are zip archives with a `.tracord.zip` suffix.

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
.tracord/                 # local run store, ignored by Git
```

## Contributing

Tracord uses a review-heavy workflow for meaningful changes: planning, design review, engineering review, adversarial review, issue filing, implementation, PR, another adversarial review, fixes, and merge. See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## Security

Tracord may process sensitive prompts, command output, file paths, tool inputs, and trace artifacts. Do not publish secrets or private traces in public issues. See [SECURITY.md](SECURITY.md).

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).