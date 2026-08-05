# File Diff Capture

Tracord can record the Git working-tree changes made while a command runs:

```bash
tracord record --capture-diff -- python agent.py
```

Capture is opt-in because patches can contain source code, credentials, generated data, and other sensitive content. Replay also requires an explicit `--capture-diff`; capture behavior is never inherited from trace metadata.

## Capture Model

Tracord resolves the Git repository containing the command working directory and creates two temporary tree snapshots: one immediately before the command and one immediately after it. Each snapshot includes:

- tracked working-tree content, whether staged or unstaged
- non-ignored untracked files
- additions, modifications, deletions, mode changes, and renames
- submodule gitlink commits, but not dirty files inside submodules

Ignored files and empty directories are outside the capture scope.

Snapshots use temporary Git index and object directories. Tracord does not update the repository index, refs, working tree, or persistent object database. When the trace store is inside the repository, it is excluded from both snapshots.

Snapshotting uses normal Git attribute and clean-filter behavior. Capture should only be enabled in repositories whose Git configuration and filter commands are trusted.

## Trace Data

Captured traces add a top-level `file_changes` object and a `file.diff` event. The capture status is one of:

- `captured`: a patch artifact was written
- `unchanged`: the two snapshots are identical
- `skipped`: capture was unavailable, such as outside a Git repository
- `omitted`: changes were found but the patch exceeded the size limit
- `error`: snapshot or diff generation failed

The structured `files` list records Git status codes and repository-relative paths. Renames and copies include `old_path`.

When present, `artifacts.file_diff` points to `changes.patch`. Bundle export and import include this artifact automatically.

## Privacy And Limits

Default capture applies Tracord best-effort secret redaction to textual patches and omits binary payloads. `--no-redact` stores raw text and Git binary patch data.

The default patch limit is 10 MiB. Set a smaller or larger positive limit with `--max-diff-bytes`. Oversized patches are deleted, while the structured changed-file summary remains in the trace.

Redaction is not a complete data-loss prevention system. Review every trace before sharing or exporting it.
