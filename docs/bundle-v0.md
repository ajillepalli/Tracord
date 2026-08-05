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

## Replay

`tracord replay <run-id>` re-runs the command from a command trace and records the result as a new run. Replay uses the current working directory, not the original trace directory.
