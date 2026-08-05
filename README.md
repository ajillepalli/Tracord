<p align="center">
  <img src="docs/images/tracord-logo.svg" alt="Tracord" width="520" />
</p>

<p align="center">
  <a href="#install-and-record-your-first-run"><strong>Quick start</strong></a> &middot;
  <a href="#common-workflows"><strong>Workflows</strong></a> &middot;
  <a href="#documentation"><strong>Documentation</strong></a> &middot;
  <a href="CONTRIBUTING.md"><strong>Contributing</strong></a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" /></a>
  <img alt="Python CLI" src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" />
  <img alt="Local first" src="https://img.shields.io/badge/local--first-agent%20traces-111827.svg" />
</p>

Tracord is a local-first flight recorder for agentic software. Wrap a command to capture its status, timing, output, and optional Git changes, then inspect, assert, export, or replay that evidence without sending it to a hosted service.

## Install and record your first run

Tracord currently installs from source and requires Python 3.11 or newer.

```bash
git clone https://github.com/ajillepalli/Tracord.git
cd Tracord
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS or Linux
source .venv/bin/activate
```

Install Tracord and record a command:

```bash
python -m pip install -e .
tracord record --name hello -- python -c "print('hello from tracord')"
tracord list
```

The record command prints a run ID and stores the trace under `.tracord/runs/<run-id>/`. Use that ID in the commands below.

## Common workflows

```bash
# Read the complete trace
tracord inspect <run-id>

# Check deterministic expectations
tracord assert <run-id> --status passed --stdout-contains tracord

# Preview an export without writing a bundle
tracord export <run-id> --preview
tracord export <run-id> --preview --json --fail-on-findings

# Export, import, and replay
tracord export <run-id> --output run.tracord.zip
tracord import run.tracord.zip
tracord replay <run-id>
```

Capture a before/after Git diff only when you need it, because patches can contain source code or sensitive data:

```bash
tracord record --capture-diff -- python agent.py
```

Repository-owned assertion cases live in `.tracord/assertions.json`. See the [assertion schema](schemas/assertions-v0.schema.json) for the file format and [Contributing](CONTRIBUTING.md) for the test setup.

## Documentation

| Document | What it covers |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Components, storage, safety boundaries, and near-term direction |
| [Trace v0](docs/trace-v0.md) | Human-readable trace contract |
| [Trace v0 JSON Schema](schemas/trace-v0.schema.json) | Machine-readable trace validation |
| [Assertion v0 JSON Schema](schemas/assertions-v0.schema.json) | Repository assertion-file contract |
| [Bundle v0](docs/bundle-v0.md) | Export, import, preview, replay, and archive safety |
| [File diff capture](docs/file-diff-capture.md) | Git capture model, privacy behavior, and limits |
| [Roadmap](docs/ROADMAP.md) | Planned releases and open product questions |
| [Changelog](CHANGELOG.md) | Notable shipped and unreleased changes |
| [Contributing](CONTRIBUTING.md) | Development setup, tests, workflow, and repository layout |
| [Security](SECURITY.md) | Private vulnerability reporting and trace-handling guidance |
| [Code of conduct](CODE_OF_CONDUCT.md) | Community expectations |
| [Agent workflow](AGENTS.md) | Required review sequence for repository agents |

The in-package schemas define the versioned CI result payloads under development: [record](src/tracord/schemas/record-result-v0.schema.json), [replay](src/tracord/schemas/replay-result-v0.schema.json), [assert](src/tracord/schemas/assertion-result-v0.schema.json), and [list](src/tracord/schemas/list-result-v0.schema.json).

## Security

Traces may contain prompts, command output, paths, tool inputs, patches, and secrets. Review recordings before sharing them, use export preview as a safety check rather than a guarantee, and report vulnerabilities through the private process in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
