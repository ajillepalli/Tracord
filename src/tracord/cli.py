"""Tracord command line interface."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import zipfile
from pathlib import Path
from typing import cast

from . import __version__
from .assertion_files import AssertionFileError, load_assertion_case
from .assertions import (
    AssertionRunError,
    ExpectationValidationError,
    TraceExpectations,
    evaluate_run,
    validate_expectations,
)
from .bundle import export_run, import_bundle, validate_run_id
from .export_preview import (
    DEFAULT_MAX_SCAN_BYTES,
    ExportPreviewError,
    MAX_TEXT_FILE_ENTRIES,
    PREVIEW_VERSION,
    gate_reasons,
    preview_export,
)
from .git_capture import DEFAULT_GIT_TIMEOUT_SECONDS, DEFAULT_MAX_DIFF_BYTES
from .recorder import record_command
from .redaction import sanitize_label
from .replay import replay_run
from .result_codes import JSON_OUTPUT_FAILURE_EXIT_CODE
from .storage import DEFAULT_HOME, list_runs, read_json, run_dir


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def console_main(argv: list[str] | None = None) -> int:
    """Run the CLI behind one process-level output transport boundary."""
    try:
        try:
            exit_code = main(argv)
        except SystemExit as exc:
            exit_code = _system_exit_code(exc.code)
    except BrokenPipeError:
        _silence_broken_standard_streams()
        return JSON_OUTPUT_FAILURE_EXIT_CODE
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        return exit_code
    except OSError:
        _silence_broken_standard_streams()
        return JSON_OUTPUT_FAILURE_EXIT_CODE


def _system_exit_code(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _silence_broken_standard_streams() -> None:
    """Prevent interpreter-shutdown retries after a process-level broken pipe."""
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                os.dup2(devnull_fd, stream.fileno())
            except (AttributeError, OSError, ValueError):
                continue
    finally:
        os.close(devnull_fd)


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
    assert_cmd.add_argument("--case", dest="case_name", help="repository assertion case")
    assert_cmd.add_argument("--file", type=Path, help="assertion file path")
    assert_cmd.set_defaults(handler=handle_assert)

    export_cmd = subparsers.add_parser("export", help="export a run as a portable bundle")
    export_cmd.add_argument("--store", default=DEFAULT_HOME, help="trace store directory")
    export_cmd.add_argument("--output", type=Path, help="bundle path to write")
    export_cmd.add_argument("--overwrite", action="store_true", help="replace an existing bundle")
    export_cmd.add_argument("--preview", action="store_true", help="inspect without writing a bundle")
    export_cmd.add_argument(
        "--json", dest="json_output", action="store_true", help="emit deterministic JSON"
    )
    export_cmd.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="exit 3 for gating findings, blocked export, or incomplete scan",
    )
    export_cmd.add_argument(
        "--allow-incomplete-scan",
        action="store_true",
        help="allow incomplete coverage, but not findings or blocked exports",
    )
    export_cmd.add_argument(
        "--max-scan-bytes",
        type=positive_int,
        help=f"maximum bytes scanned per file (default: {DEFAULT_MAX_SCAN_BYTES})",
    )
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
    inline_values = (
        args.status,
        args.exit_code,
        args.stdout_contains,
        args.stderr_contains,
        args.max_duration_ms,
        args.no_timeout,
    )
    file_mode = args.case_name is not None or args.file is not None
    if args.file is not None and args.case_name is None:
        return _assert_error("assertion_mode_conflict", exit_code=2)
    if file_mode and any(value is not None and value is not False for value in inline_values):
        return _assert_error("assertion_mode_conflict", exit_code=2)

    try:
        validate_run_id(args.run_id)
    except ValueError:
        return _assert_error("invalid_run_id", exit_code=2)

    if file_mode:
        assertion_path = args.file or (Path(args.store) / "assertions.json")
        try:
            expectations = load_assertion_case(assertion_path, args.case_name)
        except AssertionFileError as exc:
            return _assert_error(exc.code, exit_code=2, location=exc.location)
    else:
        expectations = TraceExpectations(
            status=args.status,
            exit_code=args.exit_code,
            stdout_contains=args.stdout_contains,
            stderr_contains=args.stderr_contains,
            max_duration_ms=args.max_duration_ms,
            no_timeout=args.no_timeout,
        )
        try:
            validate_expectations(expectations)
        except ExpectationValidationError as exc:
            return _assert_error(exc.code, exit_code=2)

    try:
        on_disk_run_id, failures = evaluate_run(
            Path(args.store), args.run_id, expectations
        )
    except AssertionRunError as exc:
        return _assert_error(
            exc.code,
            exit_code=2 if exc.code == "invalid_run_id" else 1,
            location=exc.location,
        )
    if failures:
        for failure in failures:
            _assert_error(failure.code, exit_code=1, location=failure.location)
        return 1
    print(f"pass {sanitize_label(on_disk_run_id)}")
    return 0


def _assert_error(code: str, *, exit_code: int, location: str | None = None) -> int:
    suffix = f" at {sanitize_label(location)}" if location else ""
    print(f"tracord: assert failed: {code}{suffix}", file=sys.stderr)
    return exit_code


def handle_export(args: argparse.Namespace) -> int:
    preview_only_options = (
        args.json_output,
        args.fail_on_findings,
        args.allow_incomplete_scan,
        args.max_scan_bytes is not None,
    )
    if not args.preview and any(preview_only_options):
        print("tracord: preview options require --preview", file=sys.stderr)
        return 2
    if args.preview and (args.output is not None or args.overwrite):
        print("tracord: --preview cannot be used with --output or --overwrite", file=sys.stderr)
        return 2
    if args.preview:
        try:
            preview = preview_export(
                root=Path(args.store),
                run_id=args.run_id,
                max_scan_bytes=args.max_scan_bytes or DEFAULT_MAX_SCAN_BYTES,
            )
        except ExportPreviewError as exc:
            if args.json_output:
                write_json_stdout(
                    {
                        "preview_version": PREVIEW_VERSION,
                        "trace_valid": False
                        if exc.code in {"invalid_trace", "invalid_trace_json"}
                        else None,
                        "error": exc.code,
                    }
                )
            print(f"tracord: export preview failed: {exc.code}", file=sys.stderr)
            return 2 if exc.code in {"invalid_run_id", "invalid_scan_limit"} else 1

        reasons = gate_reasons(
            preview,
            allow_incomplete_scan=args.allow_incomplete_scan,
        )
        preview["fail_reasons"] = reasons
        preview["gate_enforced"] = args.fail_on_findings
        if args.json_output:
            write_json_stdout(preview)
        else:
            print_export_preview(preview)
        if args.fail_on_findings and reasons:
            return 3
        return 0

    try:
        bundle_path = export_run(
            root=Path(args.store),
            run_id=args.run_id,
            output_path=args.output,
            overwrite=args.overwrite,
        )
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"tracord: {sanitize_label(str(exc))}", file=sys.stderr)
        return 1
    print(
        f"exported {sanitize_label(args.run_id)} {sanitize_label(str(bundle_path))}"
    )
    return 0


def print_export_preview(preview: dict[str, object]) -> None:
    scan = cast(dict[str, object], preview["scan"])
    findings = cast(dict[str, object], preview["findings"])
    coverage = "complete" if scan["complete"] else "incomplete"
    export_status = preview["export_preflight"]
    lines = [
        f"preview {preview['run_id_display']} export={export_status} scan={coverage}"
    ]
    lines.append(
        f"files total={scan['files_total']} scanned={scan['files_scanned']} "
        f"skipped={scan['files_skipped']} bytes={scan['bytes_scanned']}"
    )
    lines.append(
        f"findings gating={findings['gating_total']} "
        f"advisory={findings['advisory_total']} "
        f"already-redacted={findings['already_redacted_total']}"
    )
    files = cast(list[dict[str, object]], preview["files"])
    noteworthy = [
        file
        for file in files
        if file["status"] != "scanned"
        or cast(dict[str, object], file["findings"])["total"] != 0
        or file.get("identity_verified") is False
    ]
    for file in noteworthy[:MAX_TEXT_FILE_ENTRIES]:
        reason = f" reason={file['reason']}" if "reason" in file else ""
        identity = (
            " identity=unverified"
            if file.get("identity_verified") is False
            else ""
        )
        lines.append(f"{file['status']} {file['path']}{reason}{identity}")
    if len(noteworthy) > MAX_TEXT_FILE_ENTRIES:
        lines.append(
            f"additional noteworthy files={len(noteworthy) - MAX_TEXT_FILE_ENTRIES}"
        )
    fail_reasons = preview["fail_reasons"]
    if isinstance(fail_reasons, list) and fail_reasons:
        gate_state = "failed" if preview["gate_enforced"] else "would fail"
        lines.append(
            f"gate {gate_state}: " + ", ".join(str(reason) for reason in fail_reasons)
        )
    _write_stdout("\n".join(lines) + "\n")


def write_json_stdout(payload: dict[str, object]) -> None:
    """Write deterministic JSON with LF even when the Windows console translates text."""
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    _write_stdout(content)


def _write_stdout(content: str) -> None:
    """Write UTF-8 bytes when stdout exposes its binary stream."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(content)
        sys.stdout.flush()
        return
    sys.stdout.flush()
    buffer.write(content.encode("utf-8"))
    buffer.flush()


def handle_import(args: argparse.Namespace) -> int:
    try:
        trace = import_bundle(root=Path(args.store), bundle_path=args.bundle, overwrite=args.overwrite)
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        ValueError,
        RuntimeError,
        RecursionError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        print(f"tracord: {sanitize_label(str(exc))}", file=sys.stderr)
        return 1
    print(f"imported {sanitize_label(str(trace['run_id']))}")
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
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


if __name__ == "__main__":
    raise SystemExit(console_main())
