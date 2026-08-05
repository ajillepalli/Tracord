# Agent Workflow Policy

This policy applies to AI-assisted work in this repository and is intended to be portable across GitHub projects.

## Required Workflow

All non-trivial work in a GitHub repository must move through this sequence:

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

## Rules

- Do not skip the issue step for feature, architecture, security, or behavior changes.
- Keep implementation work on a branch and merge through a pull request.
- Treat adversarial review as a real blocking review, not a formality.
- Resolve review concerns in code or explicitly document why they are not being acted on.
- Security, privacy, data-loss, path-traversal, command-execution, and replay-safety concerns block merge until resolved.
- Do not add AI-attribution trailers to commits, PR bodies, or docs.

## Small Changes

Typo fixes, metadata changes, and mechanical documentation cleanup may use a shorter path, but should still be committed through normal Git hygiene.
