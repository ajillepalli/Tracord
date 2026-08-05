"""Read-only, bounded safety preview for trace bundle exports."""

from __future__ import annotations

import json
import re
import stat
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundle import (
    BUNDLE_VERSION,
    MANIFEST_FILE,
    TRACE_FILE,
    artifact_names as validated_artifact_names,
    build_manifest,
    validate_run_id,
)
from .paths import (
    IdentityComparison,
    SafePathError,
    is_link_or_junction,
    open_prepared_file,
    prepare_regular_file,
    validate_relative_path,
    verify_opened_file,
)
from .redaction import (
    REDACTION,
    RedactionSummary,
    RuleRedactionSummary,
    sanitize_label,
    summarize_redactions,
)
from .schema import validate_trace
from .storage import RUNS_DIR, run_dir


PREVIEW_VERSION = "tracord.export-preview.v0"
DEFAULT_MAX_SCAN_BYTES = 10 * 1024 * 1024
MAX_SCAN_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_ARTIFACTS = 1024
MAX_PREVIEW_TOTAL_BYTES = 100 * 1024 * 1024
MAX_TRACE_SCAN_BYTES = MAX_PREVIEW_TOTAL_BYTES
MAX_TEXT_FILE_ENTRIES = 50

_SECRET_CLI_FLAG = re.compile(
    r"(?i)^--?(?:[a-z0-9]+[-_])*(?:token|api[-_]?key|secret|password|passwd|pwd|auth|bearer|credential)$"
)

_EXPORT_BLOCKING_STATUSES = {
    "missing",
    "unsafe_path",
    "unreadable",
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
        max_scan_bytes=min(MAX_TRACE_SCAN_BYTES, max_total_scan_bytes),
        remaining_bytes=remaining_bytes,
    )
    if trace_result.payload["status"] == "missing":
        raise ExportPreviewError("run_not_found")
    if trace_result.payload["status"] != "scanned" or trace_result.data is None:
        raise ExportPreviewError("trace_scan_incomplete")

    try:
        trace = json.loads(trace_result.data.decode("utf-8"))
    except RecursionError:
        raise ExportPreviewError("invalid_trace") from None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ExportPreviewError("invalid_trace_json") from None
    if not isinstance(trace, dict) or validate_trace(trace):
        raise ExportPreviewError("invalid_trace")
    if trace.get("run_id") != run_id:
        raise ExportPreviewError("trace_run_id_mismatch")

    command_summary = _summarize_all_command_secrets(trace)
    if command_summary.findings_total or command_summary.already_redacted_total:
        trace_result.summary = _combine_summaries(
            [summary for summary in (trace_result.summary, command_summary) if summary]
        )
        trace_result.payload["findings"] = _summary_payload(trace_result.summary)

    remaining_bytes -= trace_result.bytes_read
    artifact_names, artifact_limit_exceeded, artifact_namespace_invalid = (
        _artifact_names(trace, max_artifacts)
    )
    files: list[dict[str, Any]] = [trace_result.payload]
    summaries = [trace_result.summary] if trace_result.summary is not None else []
    bytes_read = trace_result.bytes_read

    projected_files = [TRACE_FILE, *artifact_names]
    manifest = build_manifest(run_id=run_id, trace=trace, files=projected_files)
    manifest_text = json.dumps(manifest, sort_keys=True)
    manifest_data = manifest_text.encode("utf-8")
    manifest_bytes = len(manifest_data)
    manifest_scan_bytes = min(manifest_bytes, remaining_bytes)
    manifest_summary = summarize_redactions(
        manifest_data[:manifest_scan_bytes].decode("utf-8", errors="surrogateescape")
    )
    manifest_status = "scanned" if manifest_scan_bytes == manifest_bytes else "aggregate_limit"
    files.append(
        _file_payload(
            file_id="manifest",
            path=MANIFEST_FILE,
            status=manifest_status,
            reason=None if manifest_status == "scanned" else "aggregate_limit",
            size_bytes=None,
            scanned_bytes=manifest_scan_bytes,
            summary=manifest_summary,
        )
    )
    summaries.append(manifest_summary)
    remaining_bytes = max(0, remaining_bytes - manifest_scan_bytes)

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

    if artifact_namespace_invalid:
        files.append(
            _empty_file_payload(
                file_id="artifact-namespace",
                path="[unsafe artifact namespace]",
                status="unsafe_path",
                reason="invalid_artifact_namespace",
            )
        )

    aggregate_findings = _aggregate_summaries(summaries)
    files_total = (
        2 + len(artifact_names) + omitted_files + int(artifact_namespace_invalid)
    )
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
        file["status"] in _EXPORT_BLOCKING_STATUSES for file in files
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

    preview = {
        "preview_version": PREVIEW_VERSION,
        "run_id": run_id if sanitize_label(run_id) == run_id else None,
        "run_id_display": sanitize_label(run_id),
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
        "files_total_is_lower_bound": artifact_limit_exceeded,
        "gate_enforced": False,
        "fail_reasons": [],
    }
    preview["fail_reasons"] = gate_reasons(preview)
    return preview


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
    files = preview.get("files", [])
    if isinstance(files, list) and any(
        isinstance(file, dict) and file.get("identity_verified") is False
        for file in files
    ):
        reasons.append("identity_unverified")
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
    try:
        validate_run_id(run_id)
    except ValueError:
        raise ExportPreviewError("invalid_run_id")
    if (
        max_scan_bytes <= 0
        or max_scan_bytes > MAX_SCAN_BYTES
        or max_artifacts <= 0
        or max_artifacts > MAX_PREVIEW_ARTIFACTS
        or max_total_scan_bytes <= 0
        or max_total_scan_bytes > MAX_PREVIEW_TOTAL_BYTES
        or max_scan_bytes > max_total_scan_bytes
    ):
        raise ExportPreviewError("invalid_scan_limit")


def _artifact_names(
    trace: dict[str, Any], max_artifacts: int
) -> tuple[list[str], bool, bool]:
    artifacts = trace.get("artifacts")
    if not isinstance(artifacts, dict):
        return [], False, False
    try:
        validated_artifact_names(trace)
        namespace_invalid = False
    except ValueError:
        namespace_invalid = True
    names: list[str] = []
    seen: set[str] = set()
    for value in artifacts.values():
        if not isinstance(value, str) or value == TRACE_FILE or value in seen:
            continue
        if len(names) >= max_artifacts:
            return names, True, namespace_invalid
        seen.add(value)
        names.append(value)
    return names, False, namespace_invalid


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

    display_path = sanitize_label(relative_path)
    try:
        prepared = prepare_regular_file(source_dir, relative_path)
    except SafePathError as exc:
        status, reason = _preview_prepare_failure(exc.reason)
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status=status,
                reason=reason,
            )
        )

    size = prepared.initial.st_size
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
    try:
        opened = open_prepared_file(prepared)
    except SafePathError as exc:
        opened_but_changed = exc.reason == "changed"
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="changed_during_scan" if exc.reason == "changed" else "open_failed",
                size_bytes=size,
                identity_verified=False if opened_but_changed else None,
            )
        )

    data = b""
    try:
        with opened:
            data, short_read = _read_scan_bytes(opened.stream, read_limit)
            identity = verify_opened_file(opened)
    except SafePathError as exc:
        reason = "changed_during_scan" if exc.reason == "changed" else "read_failed"
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason=reason,
                size_bytes=size,
                identity_verified=False,
            ),
            bytes_read=len(data),
        )
    except OSError:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="read_failed",
                size_bytes=size,
                identity_verified=False,
            ),
            bytes_read=len(data),
        )

    if short_read:
        return _ScanResult(
            _empty_file_payload(
                file_id=file_id,
                path=display_path,
                status="unreadable",
                reason="read_failed",
                size_bytes=size,
                identity_verified=identity is IdentityComparison.VERIFIED,
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
                identity_verified=identity is IdentityComparison.VERIFIED,
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
        identity_verified=identity is IdentityComparison.VERIFIED,
    )
    return _ScanResult(payload, summary=summary, data=data, bytes_read=len(data))


def _preview_prepare_failure(reason: str) -> tuple[str, str]:
    if reason in {"invalid_relative_path", "path_escape"}:
        return "unsafe_path", reason
    if reason == "missing":
        return "missing", "missing"
    if reason == "parent_stat_failed":
        return "unreadable", "stat_failed"
    return "unreadable", reason


def _read_scan_bytes(stream: object, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    bytes_read = 0
    while bytes_read < limit:
        requested = limit - bytes_read
        chunk = stream.read(requested)
        if not chunk:
            return b"".join(chunks), True
        if len(chunk) > requested:
            chunks.append(chunk[:requested])
            return b"".join(chunks), True
        chunks.append(chunk)
        bytes_read += len(chunk)
    return b"".join(chunks), False


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


def _summarize_all_command_secrets(trace: object) -> RedactionSummary:
    findings = 0
    already_redacted = 0
    for command in _string_lists(trace):
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


def _string_lists(value: object) -> Iterator[list[object]]:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, list):
            if current and all(isinstance(item, str) for item in current):
                yield current
            pending.extend(reversed(current))
        elif isinstance(current, dict):
            pending.extend(reversed(tuple(current.values())))


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


def _file_payload(
    *,
    file_id: str,
    path: str,
    status: str,
    reason: str | None,
    size_bytes: int | None,
    scanned_bytes: int,
    summary: RedactionSummary,
    identity_verified: bool | None = None,
) -> dict[str, Any]:
    payload = {
        "id": file_id,
        "path": path,
        "size_bytes": size_bytes,
        "status": status,
        "scanned_bytes": scanned_bytes,
        "findings": _summary_payload(summary),
    }
    if identity_verified is not None:
        payload["identity_verified"] = identity_verified
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
    identity_verified: bool | None = None,
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
    if identity_verified is not None:
        payload["identity_verified"] = identity_verified
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
