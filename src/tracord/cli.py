"""Tracord command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .assertions import TraceExpectations, evaluate_trace
from .bundle import export_run, import_bundle
from .git_capture import DEFAULT_GIT_TIMEOUT_SECONDS, DEFAULT_MAX_DIFF_BYTES
from .recorder import record_command
from .replay import replay_run
from .storage import DEFAULT_HOME, list_runs, read_json, run_dir


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracord")
    parser.add_argument("--version", action="version", version=f"tracord {__version__}")

    subparsers = parser.add_subparsers(dest="command_name", required=True)

    record = subparsers.add_parser("record", help="record a local command run")
    record.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    record.add_argument("--name", help="human-readable run name")
    record.add_argument("--timeout", type=float, help="timeout in seconds")
    record.add_argument("--no-redact", action="store_true", help="store raw stdout and stderr")
    record.add_argument("--capture-diff", action="store_true", help="capture Git file changes")
    record.add_argument(
        "--max-diff-bytes",
        type=positive_int,
        default=DEFAULT_MAX_DIFF_BYTES,
        help="maximum patch artifact size",
    )
    record.add_argument(
        "--git-timeout",
        type=positive_float,
        default=DEFAULT_GIT_TIMEOUT_SECONDS,
        help="timeout in seconds for each Git capture operation",
    )
    record.add_argument("command", nargs=argparse.REMAINDER, help="command to run after --")
    record.set_defaults(handler=handle_record)

    list_cmd = subparsers.add_parser("list", help="list recorded runs")
    list_cmd.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    list_cmd.set_defaults(handler=handle_list)

    inspect = subparsers.add_parser("inspect", help="print one recorded trace")
    inspect.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    inspect.add_argument("run_id", help="run id to inspect")
    inspect.set_defaults(handler=handle_inspect)

    assert_cmd = subparsers.add_parser("assert", help="evaluate deterministic trace assertions")
    assert_cmd.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    assert_cmd.add_argument("run_id", help="run id to assert")
    assert_cmd.add_argument("--status", choices=["passed", "failed", "timeout"])
    assert_cmd.add_argument("--exit-code", type=int)
    assert_cmd.add_argument("--stdout-contains")
    assert_cmd.add_argument("--stderr-contains")
    assert_cmd.add_argument("--max-duration-ms", type=int)
    assert_cmd.add_argument("--no-timeout", action="store_true")
    assert_cmd.set_defaults(handler=handle_assert)

    export_cmd = subparsers.add_parser("export", help="export a run as a portable bundle")
    export_cmd.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    export_cmd.add_argument("--output", type=Path, help="bundle path to write")
    export_cmd.add_argument("--overwrite", action="store_true", help="replace an existing bundle")
    export_cmd.add_argument("run_id", help="run id to export")
    export_cmd.set_defaults(handler=handle_export)

    import_cmd = subparsers.add_parser("import", help="import a portable trace bundle")
    import_cmd.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    import_cmd.add_argument("--overwrite", action="store_true", help="replace an existing run")
    import_cmd.add_argument("bundle", type=Path, help="bundle path to import")
    import_cmd.set_defaults(handler=handle_import)

    replay = subparsers.add_parser("replay", help="re-run the command from a recorded trace")
    replay.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    replay.add_argument("--name", help="human-readable replay name")
    replay.add_argument("--timeout", type=float, help="override timeout in seconds")
    replay.add_argument("--no-redact", action="store_true", help="store raw stdout and stderr")
    replay.add_argument("--capture-diff", action="store_true", help="capture new Git file changes")
    replay.add_argument(
        "--max-diff-bytes",
        type=positive_int,
        default=DEFAULT_MAX_DIFF_BYTES,
        help="maximum patch artifact size",
    )
    replay.add_argument(
        "--git-timeout",
        type=positive_float,
        default=DEFAULT_GIT_TIMEOUT_SECONDS,
        help="timeout in seconds for each Git capture operation",
    )
    replay.add_argument("run_id", help="run id to replay")
    replay.set_defaults(handler=handle_replay)

    return parser


def handle_record(args: argparse.Namespace) -> int:
    command = strip_separator(args.command)
    if not command:
        print("tracord: record requires a command after --", file=sys.stderr)
        return 2

    trace = record_command(
        command,
        root=Path(args.store),
        name=args.name,
        timeout_seconds=args.timeout,
        redact=not args.no_redact,
        capture_diff=args.capture_diff,
        max_diff_bytes=args.max_diff_bytes,
        git_timeout_seconds=args.git_timeout,
    )
    print_record_result(Path(args.store), trace)
    return 0 if trace["status"] == "passed" else 1


def handle_list(args: argparse.Namespace) -> int:
    runs = list_runs(Path(args.store))
    if not runs:
        print("no runs recorded")
        return 0
    for run in runs:
        name = f" {run['name']}" if run.get("name") else ""
        print(
            f"{run['run_id']} {run['status']} exit={run['exit_code']} "
            f"{run['duration_ms']}ms{name}"
        )
    return 0


def handle_inspect(args: argparse.Namespace) -> int:
    path = run_dir(Path(args.store), args.run_id) / "trace.json"
    if not path.exists():
        print(f"tracord: run not found: {args.run_id}", file=sys.stderr)
        return 1
    print(json.dumps(read_json(path), indent=2, sort_keys=True))
    return 0


def handle_assert(args: argparse.Namespace) -> int:
    trace_directory = run_dir(Path(args.store), args.run_id)
    path = trace_directory / "trace.json"
    if not path.exists():
        print(f"tracord: run not found: {args.run_id}", file=sys.stderr)
        return 1

    expectations = TraceExpectations(
        status=args.status,
        exit_code=args.exit_code,
        stdout_contains=args.stdout_contains,
        stderr_contains=args.stderr_contains,
        max_duration_ms=args.max_duration_ms,
        no_timeout=args.no_timeout,
    )
    failures = evaluate_trace(read_json(path), trace_dir=trace_directory, expectations=expectations)
    if failures:
        for failure in failures:
            print(f"fail: {failure}", file=sys.stderr)
        return 1
    print(f"pass {args.run_id}")
    return 0


def handle_export(args: argparse.Namespace) -> int:
    try:
        bundle_path = export_run(
            root=Path(args.store),
            run_id=args.run_id,
            output_path=args.output,
            overwrite=args.overwrite,
        )
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"tracord: {exc}", file=sys.stderr)
        return 1
    print(f"exported {args.run_id} {bundle_path}")
    return 0


def handle_import(args: argparse.Namespace) -> int:
    try:
        trace = import_bundle(root=Path(args.store), bundle_path=args.bundle, overwrite=args.overwrite)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"tracord: {exc}", file=sys.stderr)
        return 1
    print(f"imported {trace['run_id']}")
    return 0


def handle_replay(args: argparse.Namespace) -> int:
    try:
        trace = replay_run(
            root=Path(args.store),
            run_id=args.run_id,
            name=args.name,
            timeout_seconds=args.timeout,
            redact=not args.no_redact,
            capture_diff=args.capture_diff,
            max_diff_bytes=args.max_diff_bytes,
            git_timeout_seconds=args.git_timeout,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"tracord: {exc}", file=sys.stderr)
        return 1
    print_record_result(Path(args.store), trace)
    return 0 if trace["status"] == "passed" else 1


def print_record_result(store: Path, trace: dict[str, object]) -> None:
    print(f"{trace['status']} {trace['run_id']} {trace['duration_ms']}ms")
    print(f"trace: {run_dir(store, str(trace['run_id'])) / 'trace.json'}")
    file_changes = trace.get("file_changes")
    if isinstance(file_changes, dict):
        changed_files = file_changes.get("changed_files", 0)
        print(f"file diff: {file_changes.get('status')} files={changed_files}")


def strip_separator(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())