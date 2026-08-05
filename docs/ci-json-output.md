# CI JSON output

Tracord provides deterministic, versioned JSON results for `record`, `replay`,
`assert`, and `list`. Add `--json` to the command being invoked:

```bash
tracord record --json -- python agent.py
tracord replay --json <run-id>
tracord assert --json <run-id> --status passed
tracord list --json
```

For `record`, `--json` must appear before the child `--` separator. JSON flags
are subcommand-local, so `tracord --json list` is an argparse error.

## Wire behavior

When a parsed JSON-mode handler reaches an expected outcome, Tracord writes
exactly one compact UTF-8 JSON object followed by LF to stdout, flushes it, and
writes nothing to stderr. Object keys are sorted for deterministic bytes.

Every core result has these fields:

| Field | Meaning |
| --- | --- |
| `result_version` | Command-specific frozen result schema version |
| `command` | `record`, `replay`, `assert`, or `list` |
| `ok` | `true` exactly when the emitted process exit is zero |
| `exit_code` | The Tracord process result, not necessarily the child exit |
| `error` | A fixed path-free error code, or `null` |

The output/exit rules are:

| Invocation result | stdout | stderr | Exit |
| --- | --- | --- | --- |
| Parsed JSON handler outcome | One JSON object plus LF | Empty | Result-specific `0`, `1`, or `2` |
| Argparse failure | Empty | Text usage/error | `2` |
| `--help` or `--version` | Text | Empty | `0` |
| Standard-stream write or flush failure | May be complete, partial, or empty | No fallback JSON or traceback | `4` |

Exit `4` supersedes any payload result. A late process-level flush failure can
occur after a complete object was written, so consumers must treat that object
as unconfirmed when the observed process exit is `4`.

Consumers must know they invoked a parsed handler with `--json`. Do not infer
JSON mode merely because stdout is nonempty: help and version output are text.

## Result versions and schemas

| Command | Result version | Schema |
| --- | --- | --- |
| `record` | `tracord.record-result.v0` | [record-result-v0.schema.json](../src/tracord/schemas/record-result-v0.schema.json) |
| `replay` | `tracord.replay-result.v0` | [replay-result-v0.schema.json](../src/tracord/schemas/replay-result-v0.schema.json) |
| `assert` | `tracord.assertion-result.v0` | [assertion-result-v0.schema.json](../src/tracord/schemas/assertion-result-v0.schema.json) |
| `list` | `tracord.list-result.v0` | [list-result-v0.schema.json](../src/tracord/schemas/list-result-v0.schema.json) |

The schemas are strict and ship inside the Python package. Unknown fields are
rejected. Integers are bounded for interoperable JSON consumers.

## Privacy projection

Record and replay results expose only:

- run ID, status, process exit code, timeout state, and duration
- whether stored output was redacted
- stdout/stderr decoder replacement state: `none`, `present`, or `unknown`
- whether the recording store identity was verifiable

List entries omit decoder and store-identity metadata. These results never
include the child command, original working directory, captured stdout/stderr,
environment, trace/artifact paths, Git patch details, changed filenames, run
name, raw exceptions, or host/store paths.

`decode_replacement` reports decoder replacement provenance only. It does not
claim that an artifact preserves original bytes or identifies a universal
encoding. Recorded text uses the platform-preferred child-output decoding and
universal `\n` newlines. Content assertions are indeterminate when replacement
is `present` or `unknown`. Legacy completed traces without this metadata infer
`none`; legacy timeout traces infer `unknown`.

Output run IDs use the ASCII grammar
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. A filesystem-valid imported run with an ID
outside that grammar can still be operated on where supported, but its unsafe
ID is never copied into a CI result.

## List ordering and completeness

Text and JSON list modes use the same read-only, bounded scanner and therefore
have the same membership and order. Valid IDs are returned in descending ASCII
ordinal order. This places Tracord's timestamp IDs newest first, but a high-sorting
imported ID can displace a generated run at the output cap.

The scanner limits each observation to:

- 1,000 emitted runs
- 10,000 retained candidates plus one unopened overflow sentinel
- 16 MiB per `trace.json`
- 64 MiB aggregate attempted trace bytes
- 256 simultaneously open JSON containers, including the root

`skipped` counts entries that were considered and rejected as malformed,
unsafe, invalid, or unreadable. `truncated` is true when candidates were left
unprocessed because an output, candidate, or aggregate limit was reached. A
declared file larger than 16 MiB is skipped before the aggregate budget is
charged. The overflow sentinel is never opened and does not increment
`skipped`.

All entries under `runs/` are considered, including stray files and dotfiles;
unsafe or non-run debris increments `skipped` so it cannot silently weaken the
completeness oracle.

`list --json` is a completeness oracle only when `skipped == 0` and
`truncated == false`. Directory enumeration is not a transactional filesystem
snapshot, so concurrent publication can also change what a later invocation
observes.

Text list output sanitizes every displayed field. When scanning is incomplete,
its final line is:

```text
tracord: list incomplete: skipped=<count> truncated=<true|false>
```

## Safe trace access

Replay, assertions, inspect, and list validate portable IDs before constructing
run paths. They require exact requested/directory/embedded ID agreement, reject
case aliases, links, junctions, hard-linked trace files, replacements, duplicate
JSON keys, nonfinite numbers, oversized integers, invalid nesting, and identity
that cannot be verified. Replay remains fail-closed because it executes the
command stored in the trace.

`inspect` deliberately remains a raw trace viewer. On success it can print the
recorded command, working directory, artifact metadata, and other sensitive
trace content. Its lookup is safe and bounded, but its output is not the privacy
projection used by CI JSON results.

`export --preview --json` retains its existing `preview_version` payload. It is
not one of the four result schemas described here.
