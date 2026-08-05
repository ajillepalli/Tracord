"""Leaf-owned constants for stable CLI result contracts."""

from __future__ import annotations

import re


RECORD_RESULT_VERSION = "tracord.record-result.v0"
REPLAY_RESULT_VERSION = "tracord.replay-result.v0"
ASSERTION_RESULT_VERSION = "tracord.assertion-result.v0"
LIST_RESULT_VERSION = "tracord.list-result.v0"

COMMAND_RECORD = "record"
COMMAND_REPLAY = "replay"
COMMAND_ASSERT = "assert"
COMMAND_LIST = "list"
RESULT_COMMANDS = frozenset(
    {COMMAND_RECORD, COMMAND_REPLAY, COMMAND_ASSERT, COMMAND_LIST}
)

MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
MIN_PROCESS_EXIT_CODE = -(2**31)
MAX_PROCESS_EXIT_CODE = (2**32) - 1
MAX_TRACE_JSON_NESTING = 256

CI_RUN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
CI_RUN_ID_SCHEMA_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?![\s\S])"
CI_RUN_ID = re.compile(CI_RUN_ID_PATTERN, re.ASCII)

RECORD_ERROR_CODES = frozenset(
    {
        "record_command_required",
        "record_store_unwritable",
        "record_spawn_failed",
        "record_failed",
        "record_result_invalid",
    }
)
REPLAY_ERROR_CODES = frozenset(
    {
        "invalid_run_id",
        "replay_run_not_found",
        "replay_trace_missing",
        "replay_trace_unreadable",
        "replay_trace_invalid",
        "replay_run_identity_mismatch",
        "replay_run_identity_unverifiable",
        "replay_store_unwritable",
        "replay_spawn_failed",
        "replay_failed",
        "replay_result_invalid",
    }
)
LIST_ERROR_CODES = frozenset(
    {"list_store_unreadable", "list_failed", "list_result_invalid"}
)

ASSERTION_EXPECTATION_LOCATIONS = frozenset(
    {
        "status",
        "exit_code",
        "stdout_contains",
        "stderr_contains",
        "max_duration_ms",
        "no_timeout",
    }
)
ASSERTION_FILE_ERROR_CODES = frozenset(
    {
        "assertion_file_missing",
        "assertion_file_unreadable",
        "assertion_file_not_regular",
        "assertion_file_changed",
        "assertion_file_too_large",
        "assertion_file_bom",
        "assertion_file_invalid_utf8",
        "assertion_file_duplicate_key",
        "assertion_file_invalid_json",
        "assertion_file_schema_invalid",
        "case_not_found",
    }
)
ASSERTION_VALIDATION_ERROR_CODES = frozenset(
    {"assertion_value_invalid", "assertion_no_expectations"}
)
ASSERTION_RUN_ERROR_CODES = frozenset(
    {
        "invalid_run_id",
        "run_not_found",
        "trace_missing",
        "trace_unreadable",
        "trace_invalid",
        "run_identity_mismatch",
        "run_identity_unverifiable",
    }
)
ASSERTION_CLI_ERROR_CODES = frozenset(
    {"assertion_mode_conflict", "assert_failed", "assert_result_invalid"}
)
ASSERTION_ERROR_CODES = frozenset(
    ASSERTION_FILE_ERROR_CODES
    | ASSERTION_VALIDATION_ERROR_CODES
    | ASSERTION_RUN_ERROR_CODES
    | ASSERTION_CLI_ERROR_CODES
)

ASSERTION_MISMATCH_CODES = frozenset({"assertion_mismatch"})
ASSERTION_INDETERMINATE_CODES = frozenset(
    {
        "artifact_unreadable",
        "artifact_invalid_utf8",
        "artifact_decode_replaced",
        "artifact_decode_unknown",
        "artifact_changed",
        "scan_incomplete",
    }
)
ASSERTION_FAILURE_CODES = frozenset(
    ASSERTION_MISMATCH_CODES | ASSERTION_INDETERMINATE_CODES
)
ASSERTION_FAILURE_KINDS = frozenset({"mismatch", "indeterminate"})
ASSERTION_OUTCOMES = frozenset({"pass", "mismatch", "indeterminate", "error"})

TRACE_STATUSES = frozenset({"passed", "failed", "timeout"})
FILE_CHANGE_STATUSES = frozenset(
    {"captured", "unchanged", "skipped", "omitted", "error"}
)
FILE_CHANGE_REASONS = frozenset(
    {
        "git_unavailable",
        "repository_discovery_failed",
        "not_git_repository",
        "git_common_dir_failed",
        "store_contains_repository",
        "before_snapshot_failed",
        "capture_not_started",
        "diff_summary_timeout",
        "summary_size_limit",
        "diff_summary_failed",
        "after_snapshot_failed",
        "diff_generation_timeout",
        "size_limit",
        "diff_generation_failed",
        "size_limit_after_redaction",
    }
)
DECODE_REPLACEMENT_STATES = frozenset({"none", "present", "unknown"})

JSON_OUTPUT_FAILURE_EXIT_CODE = 4

