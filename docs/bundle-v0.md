# Tracord Bundle v0

`tracord.bundle.v0` is the first portable archive format for sharing a recorded run.

Bundles are zip archives, usually named with a `.tracord.zip` suffix.

## Files

A v0 bundle contains:

- `manifest.json` - bundle metadata.
- `trace.json` - the recorded trace.
- trace artifacts referenced by `trace.json`, such as `stdout.log` and `stderr.log`.

## Manifest

The manifest includes:

- `bundle_version`: must be `tracord.bundle.v0`.
- `run_id`: the run id contained in the bundle.
- `schema_version`: the trace schema version.
- `created_at`: UTC ISO-8601 timestamp for the export.
- `files`: bundle members included in the archive.

## Import Safety

Import rejects unsafe archive members, including absolute paths, Windows drive paths, parent-directory traversal, and paths using backslashes.

Imported files are written only under the target run directory.

## Export Preview

`tracord export <run-id> --preview` inventories and scans the files that a v0
bundle would contain without writing an archive or creating a missing store.
Add `--json` for the versioned `tracord.export-preview.v0` payload.

The preview scans the exact raw bytes of `trace.json`, a projected manifest, and
every unique artifact referenced by the trace. The projection omits `created_at`
because that safe timestamp is generated only when export begins. Preview also
checks adjacent values for secret-named command flags such as `--token VALUE`.
This covers common suffixes such as token, API key, password, auth, bearer, and
credential, but remains a best-effort heuristic rather than a complete CLI
credential grammar.
It reports rule names and counts only. It never reports
matched values, excerpts, offsets, lengths, absolute paths, raw filesystem
exceptions, content hashes, timestamps, or mtimes. Displayed relative paths and
run IDs pass through the same best-effort redactor. `run_id` is `null` when that
redaction changes it; `run_id_display` always contains the safe display value.
Artifact IDs are opaque ordinals over the deterministically sorted path set; they
never embed rejected or redacted paths and are unique within one preview.

Each artifact is scanned as text only when it is a regular, non-symlink file and its
bounded read contains no NUL byte. The default per-file limit is 10 MiB. Preview
also enforces hard limits of 1,024 unique artifacts and 100 MiB read in total.
`trace.json` may use the aggregate ceiling so large valid traces remain gateable.
Binary, truncated, missing, unsafe, unreadable, changed, or limit-skipped files
make scan coverage incomplete. The per-file limit can be lowered but not raised;
the artifact and aggregate ceilings are intentionally not CLI-configurable.

For CI, use:

```bash
tracord export <run-id> --preview --json --fail-on-findings
```

The strict gate fails on gating findings, a blocked/unknown export preflight, or
incomplete coverage. Broad encoded
secret candidates are advisory and already-redacted named assignments are
reported separately. `--allow-incomplete-scan` opts out of only the incomplete
coverage failure; it does not suppress live findings, unsafe paths, missing
files, unreadable files, or an unknown artifact-limit preflight.

JSON and library results always report `fail_reasons`, even when `gate_enforced` is false and the
command exits successfully. `export_preflight` is `ready`, `blocked`, or
`unknown`; `export_would_succeed` is respectively `true`, `false`, or `null`.
These fields describe source-side readiness only; output-path existence and
permissions are checked by the later export operation. When the artifact limit
is exceeded, `files_total_is_lower_bound` is true rather than claiming an exact
count for entries that were intentionally not collected.
Operational failures in JSON mode emit a minimal object containing
`preview_version`, `trace_valid`, and a fixed `error` code.

Exit codes are:

- `0` - preview completed and the requested gate passed.
- `1` - operational failure, such as a missing run or invalid trace.
- `2` - incompatible or invalid CLI usage.
- `3` - strict preview gate failure.

`bytes_read` counts bytes loaded from file descriptors. `bytes_scanned` counts
bytes interpreted as text by the finding rules, including the projected
manifest. `bytes_skipped` counts known file bytes that were not text-scanned.
Binary and changed files can therefore increase both `bytes_read` and
`bytes_skipped` without increasing `bytes_scanned`.

Preview describes files at scan time. A later export can observe changed files,
so a clean preview is not proof that a later bundle is safe. On Windows,
`O_NOFOLLOW` is unavailable; path and descriptor identity checks compensate but
cannot make cross-platform races impossible. Windows junctions and other reparse
link-like reparse points are rejected from `st_reparse_tag`, including on Python 3.11 where
`Path.is_junction()` is unavailable. On POSIX, no-follow protects the
final component, while a hostile parent-directory replacement remains a residual
race. Filesystems that do not provide stable inode identity are reported as
unreadable. Read-only opens can update file access times on some filesystems.
When inode identity is unavailable, preview scans with mode/size/mtime checks,
marks coverage incomplete as `identity_unverified`, and leaves export possible.

Normal export streams each source through the same descriptor used for its
snapshot verification. Import rejects linked run directories and parents and
writes members through no-follow descriptors. Hardlinks remain regular files and
are intentionally out of scope for link rejection.

Output includes exact file sizes, which are sensitive metadata. Do not publish
preview JSON for private traces. Content hashes are intentionally omitted because
they provide a substantially stronger offline guess-verification oracle for a
small secret-only artifact.

## Replay

`tracord replay <run-id>` re-runs the command from a command trace and records the result as a new run. Replay uses the current working directory, not the original trace directory.
