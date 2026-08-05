"""Read-only, bounded safety preview for trace bundle exports."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .bundle import BUNDLE_VERSION, MANIFEST_FILE, TRACE_FILE
from .paths import is_link_or_junction, validate_relative_path
from .redaction import (
    REDACTION,
    RedactionSummary,
    RuleRedactionSummary,
    redact_text,
    summarize_redactions,
)
from .schema import validate_trace
from .storage import RUNS_DIR, run_dir


PREVIEW_VERSION = "tracord.export-preview.v0"
DEFAULT_MAX_SCAN_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_ARTIFACTS = 1024
MAX_PREVIEW_TOTAL_BYTES = 100 * 1024 * 1024
MAX_TEXT_FILE_ENTRIES = 50

_SECRET_CLI_FLAG = re.compile(
    r"(?i)^--(?:api[-_]?key|token|secret|password)$"
)

_EXPORT_BLOCKING_STATUSES = {
    "missing",
    "unsafe_path",
    "unreadable",
    "file_limit",
}


class ExportPreviewError(ValueError):
    """A fixed-code preview error safe to report without filesystem details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class _ScanResult:
    payload: dict[str, Any]
    summary: RedactionSummary | None = None
    data: bytes | None = None
    bytes_read: int = 0


def preview_export(
    *,
    root: Path,
    run_id: str,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
    max_artifacts: int = MAX_PREVIEW_ARTIFACTS,
    max_total_scan_bytes: int = MAX_PREVIEW_TOTAL_BYTES,
) -> dict[str, Any]:
    """Inspect an export without creating or modifying files."""
    _validate_options(run_id, max_scan_bytes, max_artifacts, max_total_scan_bytes)
    source_dir = run_dir(root, run_id)
    _validate_run_directory(root, source_dir)
    remaining_bytes = max_total_scan_bytes

    trace_result = _scan_file(
        source_dir=source_dir,
        relative_path=TRACE_FILE,
        file_id="trace",
        max_scan_bytes=max_scan_bytes,
        remaining_bytes=remaining_bytes,
    )
    if trace_result.payload["status"] == "missing":
        raise ExportPreviewError("run_not_found")
    if trace_result.payload["status"] != "scanned" or trace_result.data is None:
        raise ExportPreviewError("trace_scan_incomplete")

    try:
        trace = json.loads(trace_result.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExportPreviewError("invalid_trace_json") from None
    if not isinstance(trace, dict) or validate_trace(trace):
        raise ExportPreviewError("invalid_trace")
    if trace.get("run_id") != run_id:
        raise ExportPreviewError("trace_run_id_mismatch")

    command_summary = _summarize_command_secrets(trace.get("command"))
    if command_summary.findings_total or command_summary.already_redacted_total:
        trace_result.summary = _combine_summaries(
            [summary for summary in (trace_result.summary, command_summary) if summary]
        )
        trace_result.payload["findings"] = _summary_payload(trace_result.summary)

    remaining_bytes -= trace_result.bytes_read
    artifact_names, artifact_limit_exceeded = _artifact_names(trace, max_artifacts)
    files: list[dict[str, Any]] = [trace_result.payload]
    summaries = [trace_result.summary] if trace_result.summary is not None else []
    bytes_read = trace_result.bytes_read

    manifest_text = json.dumps(
        {
            "bundle_version": BUNDLE_VERSION,
            "run_id": run_id,
            "schema_version": trace.get("schema_version"),
        },
        sort_keys=True,
    )
    manifest_summary = summarize_redactions(manifest_text)
    manifest_bytes = len(manifest_text.encode("utf-8"))
    files.append(
        _file_payload(
            file_id="manifest",
            path=MANIFEST_FILE,
            status="scanned",
            reason=None,
            size_bytes=None,
            scanned_bytes=manifest_bytes,
            summary=manifest_summary,
        )
    )
    summaries.append(manifest_summary)

    for index, relative_path in enumerate(artifact_names):
        result = _scan_file(
            source_dir=source_dir,
            relative_path=relative_path,
            file_id=f"artifact:{index:04d}",
            max_scan_bytes=max_scan_bytes,
            remaining_bytes=remaining_bytes,
        )
        files.append(result.payload)
        if result.summary is not None:
            summaries.append(result.summary)
        bytes_read += result.bytes_read
        remaining_bytes = max(0, remaining_bytes - result.bytes_read)

    omitted_files = 1 if artifact_limit_exceeded else 0
    if artifact_limit_exceeded:
        files.append(
            _empty_file_payload(
                file_id="artifact-limit",
                path="[additional artifacts omitted]",
                status="file_limit",
                reason="file_limit",
                omitted_files=1,
            )
        )

    aggregate_findings = _aggregate_summaries(summaries)
    files_total = 2 + len(artifact_names) + omitted_files
    fully_scanned = sum(
        1 for file in files if file["status"] == "scanned"
    )
    reason_counts: Counter[str] = Counter()
    bytes_scanned = 0
    bytes_skipped = 0
    for file in files:
        bytes_scanned += int(file["scanned_bytes"])
        size = file["size_bytes"]
        if isinstance(size, int):
            bytes_skipped += max(0, size - int(file["scanned_bytes"]))
        if file["status"] != "scanned":
            count = int(file.get("omitted_files", 1))
            reason_counts[str(file.get("reason", file["status"]))] += count

    scan_complete = fully_scanned == files_total
    known_export_blocker = any(
        file["status"] in _EXPORT_BLOCKING_STATUSES - {"file_limit"} for file in files
    )
    if known_export_blocker:
        export_preflight = "blocked"
        export_would_succeed: bool | None = False
    elif omitted_files:
        export_preflight = "unknown"
        export_would_succeed = None
    else:
        export_preflight = "ready"
        export_would_succeed = True

    return {
        "preview_version": PREVIEW_VERSION,
        "run_id": run_id if _sanitize_label(run_id) == run_id else None,
        "run_id_display": _sanitize_label(run_id),
        "bundle_version": BUNDLE_VERSION,
        "trace_valid": True,
        "export_preflight": export_preflight,
        "export_would_succeed": export_would_succeed,
        "scan": {
            "complete": scan_complete,
            "max_scan_bytes": max_scan_bytes,
            "max_artifacts": max_artifacts,
            "max_total_scan_bytes": max_total_scan_bytes,
            "files_total": files_total,
            "files_scanned": fully_scanned,
            "files_skipped": files_total - fully_scanned,
            "bytes_read": bytes_read,
            "bytes_scanned": bytes_scanned,
            "bytes_skipped": bytes_skipped,
            "reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(reason_counts.items())
            ],
        },
        "findings": aggregate_findings,
        "files": files,
        "fail_reasons": [],
    }


def gate_reasons(
    preview: dict[str, Any], *, allow_incomplete_scan: bool = False
) -> list[str]:
    """Return deterministic reasons that should fail a strict preview gate."""
    reasons: list[str] = []
    findings = preview.get("findings", {})
    scan = preview.get("scan", {})
    if isinstance(findings, dict) and findings.get("gating_total", 0) > 0:
        reasons.append("gating_findings")
    if preview.get("export_would_succeed") is not True:
        reasons.append("export_blocked")
    if (
        isinstance(scan, dict)
        and scan.get("complete") is not True
        and not allow_incomplete_scan
    ):
        reasons.append("incomplete_scan")
    return reasons


def _validate_options(
    run_id: str,
    max_scan_bytes: int,
    max_artifacts: int,
    max_total_scan_bytes: int,
) -> None:
    limits = (max_scan_bytes, max_artifacts, max_total_scan_bytes)
    if any(not isinstance(limit, int) or isinstance(limit, bool) for limit in limits):
        raise ExportPreviewError("invalid_scan_limit")
    if not isinstance(run_id, str):
        raise ExportPreviewError("invalid_run_id")
    run_id_errors = validate_relative_path(run_id)
    if run_id_errors or "/" in run_id:
        raise ExportPreviewError("invalid_run_id")
    if (
        max_scan_bytes <= 0
        or max_scan_bytes > DEFAULT_MAX_SCAN_BYTES
        or max_artifacts <= 0
        or max_artifacts > MAX_PREVIEW_ARTIFACTS
        or max_total_scan_bytes <= 0
        or max_total_scan_bytes > MAX_PREVIEW_TOTAL_BYTES
        or max_scan_bytes > max_total_scan_bytes
    ):
        raise ExportPreviewError("invalid_scan_limit")


def _artifact_names(
    trace: dict[str, Any], max_artifacts: int
) -> tuple[list[str], bool]:
    artifacts = trace.get("artifacts")
    if not isinstance(artifacts, dict):
        return [], False
    names: list[str] = []
    seen: set[str] = set()
    for value in artifacts.values():
        if not isinstance(value, str) or value == TRACE_FILE or value in seen:
            continue
        if len(names) >= max_artifacts:
            return sorted(names), True
        seen.add(value)
        names.append(value)
    return sorted(names), False


def _scan_file(
    *,
    source_dir: Path,
    relative_path: str,
    file_id: str,
    max_scan_bytes: int,
    remaining_bytes: int,
) -> _ScanResult:
    if validate_relative_path(relative_path):
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path="[unsafe artifact path]",
                status="unsafe_path",
                reason="invalid_relative_path",
            )
        )

    display_path = _sanitize_label(relative_path)
    candidate = source_dir.joinpath(*PurePosixPath(relative_path).parts)
    containment = _check_containment(source_dir, candidate)
    if containment is not None:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unsafe_path",
                reason=containment,
            )
        )

    parent_reason = _check_parent_components(source_dir, relative_path)
    if parent_reason is not None:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason=parent_reason,
            )
        )

    try:
        initial = candidate.lstat()
    except FileNotFoundError:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="missing",
                reason="missing",
            )
        )
    except OSError:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="stat_failed",
            )
        )

    size = initial.st_size
    if is_link_or_junction(candidate, initial):
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="symlink",
                size_bytes=size,
            )
        )
    if not stat.S_ISREG(initial.st_mode):
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="not_regular_file",
                size_bytes=size,
            )
        )
    if initial.st_ino == 0:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="identity_unavailable",
                size_bytes=size,
            )
        )
    if remaining_bytes <= 0:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="aggregate_limit",
                reason="aggregate_limit",
                size_bytes=size,
            )
        )

    read_limit = min(size, max_scan_bytes, remaining_bytes)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="open_failed",
                size_bytes=size,
            )
        )

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_identity(initial, opened):
            return _ScanResult(
                _empty_file_payload(
                    file_id=file_id,
                    path=display_path,
                    status="unreadable",
                    reason="changed_during_scan",
                    size_bytes=size,
                )
            )
        stream_descriptor, descriptor = descriptor, -1
        with os.fdopen(stream_descriptor, "rb", closefd=True) as stream:
            data = stream.read(read_limit)
            final_descriptor = os.fstat(stream.fileno())
    except OSError:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="read_failed",
                size_bytes=size,
            )
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if (
        not _same_snapshot(initial, final_descriptor)
        or len(data) != read_limit
        or _check_containment(source_dir, candidate) is not None
    ):
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="changed_during_scan",
                size_bytes=size,
            ),
            bytes_read=len(data),
        )
    try:
        final_path = candidate.lstat()
    except OSError:
        final_path = None
    if final_path is None or not _same_snapshot(initial, final_path):
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="changed_during_scan",
                size_bytes=size,
            ),
            bytes_read=len(data),
        )

    if b"\x00" in data:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="skipped_binary",
                reason="binary_content",
                size_bytes=size,
            ),
            bytes_read=len(data),
        )

    text = data.decode("utf-8", errors="surrogateescape")
    summary = summarize_redactions(text)
    if size > read_limit:
        status = "truncated" if read_limit == max_scan_bytes else "aggregate_limit"
        reason = "aggregate_limit" if status == "aggregate_limit" else "max_scan_bytes"
    else:
        status = "scanned"
        reason = None
    payload = _file_payload(
        file_id=file_id,
        path=display_path,
        status=status,
        reason=reason,
        size_bytes=size,
        scanned_bytes=len(data),
        summary=summary,
    )
    return _ScanResult(payload, summary=summary, data=data, bytes_read=len(data))


def _validate_run_directory(root: Path, source_dir: Path) -> None:
    runs_directory = root / RUNS_DIR
    try:
        runs_info = runs_directory.lstat()
        source_info = source_dir.lstat()
    except FileNotFoundError:
        raise ExportPreviewError("run_not_found") from None
    except OSError:
        raise ExportPreviewError("run_directory_unreadable") from None
    if (
        is_link_or_junction(runs_directory, runs_info)
        or not stat.S_ISDIR(runs_info.st_mode)
        or is_link_or_junction(source_dir, source_info)
        or not stat.S_ISDIR(source_info.st_mode)
    ):
        raise ExportPreviewError("run_directory_unsafe")


def _sanitize_label(value: str) -> str:
    redacted = redact_text(value)
    characters: list[str] = []
    for character in redacted:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            characters.append(f"\\u{ord(character):04x}")
        else:
            characters.append(character)
    return "".join(characters)


def _summarize_command_secrets(command: object) -> RedactionSummary:
    findings = 0
    already_redacted = 0
    if isinstance(command, list):
        for index, argument in enumerate(command[:-1]):
            if not isinstance(argument, str) or not _SECRET_CLI_FLAG.fullmatch(argument):
                continue
            value = command[index + 1]
            if not isinstance(value, str):
                continue
            if value == REDACTION:
                already_redacted += 1
            else:
                findings += 1
    rules: tuple[RuleRedactionSummary, ...] = ()
    if findings or already_redacted:
        rules = (
            RuleRedactionSummary(
                rule="secret_cli_flag_value",
                gating=True,
                findings=findings,
                already_redacted=already_redacted,
            ),
        )
    return RedactionSummary(
        rules=rules,
        findings_total=findings,
        gating_total=findings,
        advisory_total=0,
        already_redacted_total=already_redacted,
    )


def _combine_summaries(summaries: list[RedactionSummary]) -> RedactionSummary:
    aggregate = _aggregate_summaries(summaries)
    return RedactionSummary(
        rules=tuple(
            RuleRedactionSummary(
                rule=rule["rule"],
                gating=rule["gating"],
                findings=rule["findings"],
                already_redacted=rule["already_redacted"],
            )
            for rule in aggregate["by_rule"]
        ),
        findings_total=aggregate["total"],
        gating_total=aggregate["gating_total"],
        advisory_total=aggregate["advisory_total"],
        already_redacted_total=aggregate["already_redacted_total"],
    )


def _check_containment(source_dir: Path, candidate: Path) -> str | None:
    try:
        root = source_dir.resolve(strict=False)
        target = candidate.resolve(strict=False)
        target.relative_to(root)
    except (OSError, ValueError):
        return "path_escape"
    return None


def _check_parent_components(source_dir: Path, relative_path: str) -> str | None:
    current = source_dir
    for part in PurePosixPath(relative_path).parts[:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return "missing_parent"
        except OSError:
            return "stat_failed"
        if is_link_or_junction(current, info):
            return "symlink_parent"
        if not stat.S_ISDIR(info.st_mode):
            return "parent_not_directory"
    return None


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_identity(first, second)
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _file_payload(
    *,
    file_id: str,
    path: str,
    status: str,
    reason: str | None,
    size_bytes: int | None,
    scanned_bytes: int,
    summary: RedactionSummary,
) -> dict[str, Any]:
    payload = {
        "id": file_id,
        "path": path,
        "size_bytes": size_bytes,
        "status": status,
        "scanned_bytes": scanned_bytes,
        "findings": _summary_payload(summary),
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def _empty_file_payload(
    *,
    file_id: str,
    path: str,
    status: str,
    reason: str,
    size_bytes: int | None = None,
    omitted_files: int | None = None,
) -> dict[str, Any]:
    payload = {
        "id": file_id,
        "path": path,
        "size_bytes": size_bytes,
        "status": status,
        "scanned_bytes": 0,
        "findings": _empty_findings(),
        "reason": reason,
    }
    if omitted_files is not None:
        payload["omitted_files"] = omitted_files
    return payload


def _summary_payload(summary: RedactionSummary) -> dict[str, Any]:
    return {
        "total": summary.findings_total,
        "gating_total": summary.gating_total,
        "advisory_total": summary.advisory_total,
        "already_redacted_total": summary.already_redacted_total,
        "by_rule": [
            {
                "rule": rule.rule,
                "gating": rule.gating,
                "findings": rule.findings,
                "already_redacted": rule.already_redacted,
            }
            for rule in sorted(summary.rules, key=lambda item: item.rule)
        ],
    }


def _empty_findings() -> dict[str, Any]:
    return {
        "total": 0,
        "gating_total": 0,
        "advisory_total": 0,
        "already_redacted_total": 0,
        "by_rule": [],
    }


def _aggregate_summaries(summaries: list[RedactionSummary]) -> dict[str, Any]:
    totals = _empty_findings()
    rule_counts: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        totals["total"] += summary.findings_total
        totals["gating_total"] += summary.gating_total
        totals["advisory_total"] += summary.advisory_total
        totals["already_redacted_total"] += summary.already_redacted_total
        for rule in summary.rules:
            current = rule_counts.setdefault(
                rule.rule,
                {
                    "rule": rule.rule,
                    "gating": rule.gating,
                    "findings": 0,
                    "already_redacted": 0,
                },
            )
            current["findings"] += rule.findings
            current["already_redacted"] += rule.already_redacted
    totals["by_rule"] = [rule_counts[name] for name in sorted(rule_counts)]
    return totals
