# Contributing

Thanks for your interest in Tracord.

Tracord is early, but the project touches sensitive ground: command execution, trace export, replay, secrets, and eventually model/tool-call data. Contributions should be small enough to review carefully and explicit about security and privacy tradeoffs.

## Getting it running

```bash
git clone https://github.com/ajillepalli/Tracord.git
cd Tracord
python -m venv .venv
.venv/Scripts/activate  # or source .venv/bin/activate on macOS/Linux
python -m pip install -e .
python -m pytest
```

## Required workflow

All non-trivial repository work follows this sequence:

1. Planning
2. Design review
3. Engineering review
4. Adversarial review, ideally with Claude Opus when available
5. Fix all review concerns
6. Issue filing
7. Implementation
8. Pull request
9. Adversarial review, ideally with Claude Opus when available
10. Fix all reviewer concerns
11. Merge

This applies to feature work, security-sensitive changes, trace schema changes, replay behavior, export/import behavior, CLI behavior, and architecture changes. Typo fixes and mechanical documentation cleanup can use a shorter path, but should still be committed cleanly.

## Useful early contributions

- Trace format proposals.
- Agent failure examples that should become replayable tests.
- CLI command design.
- Security and privacy review notes.
- Integration notes for agent frameworks and MCP servers.
- Fixtures that capture real failure modes without exposing private data.

## Development principles

- Prefer local-first behavior by default.
- Avoid collecting secrets, credentials, private prompts, or unnecessary environment data.
- Make traces useful to both humans and automation.
- Keep the core small before adding framework-specific integrations.
- Favor deterministic assertions before LLM-as-judge checks.
- Reject unsafe paths and suspicious archive contents by default.
- Treat replay as security-sensitive behavior.

## Before you open a PR

```bash
python -m pytest
```

A PR should include:

- A linked issue.
- A clear description of the change.
- Any relevant examples, fixtures, or bundles.
- Tests when behavior is added or changed.
- Documentation updates for user-facing behavior or trace format changes.
- Notes about privacy, security, or compatibility risks.

Use the PR template checklist. Review concerns are blocking unless they are explicitly documented as accepted tradeoffs.

## Repository shape

```text
src/tracord/              # CLI and core recording code
  cli.py                  # argparse command surface
  recorder.py             # command execution and trace writing
  storage.py              # local run directory helpers
  schema.py               # lightweight trace validation
  assertions.py           # deterministic assertion evaluation
  bundle.py               # portable .tracord.zip import/export
  replay.py               # command replay
  redaction.py            # named redaction rules and count-only summaries
  paths.py                # safe relative path handling
  git_capture.py          # isolated Git before/after snapshots
tests/                    # unit tests
docs/                     # architecture, roadmap, trace and bundle docs
schemas/                  # machine-readable trace schemas
.github/                  # issue templates and PR checklist
```

## Commit messages

Use short, imperative commit messages that say why the change exists when the reason is not obvious:

```text
Add bundle path traversal checks
Document trace v0 events
Fix replay timeout handling
```

Do not add AI-attribution trailers to commits, PR bodies, or docs.

## Reporting security issues

Do not open a public issue for a security concern. See [SECURITY.md](SECURITY.md).
