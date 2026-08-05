import io
import json

import pytest

from tracord.ci_output import (
    CIOutputError,
    JsonEmitter,
    build_assertion_result,
    build_list_result,
    build_record_result,
    build_replay_result,
    project_full_run,
    project_list_run,
    serialize_json,
)
from tracord.result_codes import MAX_PROCESS_EXIT_CODE, MAX_SAFE_JSON_INTEGER


def trace(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "run_id": "20260805T120000Z-abcd1234",
        "status": "passed",
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 12,
        "redacted": True,
        "decode_replacement": {"stdout": "none", "stderr": "present"},
        "store_identity_verified": True,
        "command": ["secret", "--token=value"],
        "cwd": "C:/private/repo",
        "name": "unsafe\nlabel",
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
    }
    value.update(overrides)
    return value


def test_full_run_projection_is_fixed_and_privacy_safe():
    assert project_full_run(trace()) == {
        "run_id": "20260805T120000Z-abcd1234",
        "status": "passed",
        "process_exit_code": 0,
        "timed_out": False,
        "duration_ms": 12,
        "redacted": True,
        "decode_replacement": {"stdout": "none", "stderr": "present"},
        "store_identity_verified": True,
    }


def test_list_projection_is_smaller_and_legacy_completed_metadata_is_none():
    legacy = trace()
    legacy.pop("decode_replacement")
    legacy.pop("store_identity_verified")
    assert project_full_run(legacy)["decode_replacement"] == {
        "stdout": "none",
        "stderr": "none",
    }
    assert project_full_run(legacy)["store_identity_verified"] is False
    assert set(project_list_run(legacy)) == {
        "run_id",
        "status",
        "process_exit_code",
        "timed_out",
        "duration_ms",
        "redacted",
    }


def test_legacy_timeout_decode_metadata_is_unknown():
    legacy_timeout = trace(status="timeout", exit_code=None, timed_out=True)
    legacy_timeout.pop("decode_replacement")

    assert project_full_run(legacy_timeout)["decode_replacement"] == {
        "stdout": "unknown",
        "stderr": "unknown",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "../secret"),
        ("status", "unknown"),
        ("exit_code", True),
        ("exit_code", MAX_PROCESS_EXIT_CODE + 1),
        ("duration_ms", True),
        ("duration_ms", MAX_SAFE_JSON_INTEGER + 1),
        ("redacted", 1),
        ("status", "timeout"),
        ("timed_out", True),
    ],
)
def test_projection_rejects_unsafe_or_noninteroperable_values(field: str, value: object):
    with pytest.raises(CIOutputError):
        project_full_run(trace(**{field: value}))


def test_record_and_replay_results_distinguish_child_and_tracord_failures():
    passed = build_record_result(exit_code=0, run=trace())
    assert passed["ok"] is True
    assert passed["error"] is None
    assert passed["run"]["status"] == "passed"

    failed = build_replay_result(
        exit_code=1,
        run=trace(status="failed", exit_code=23),
    )
    assert failed["ok"] is False
    assert failed["error"] is None
    assert failed["run"]["process_exit_code"] == 23

    internal = build_record_result(
        exit_code=1,
        run=None,
        error_code="record_result_invalid",
    )
    assert internal["run"] is None
    assert internal["error"] == "record_result_invalid"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exit_code": 0, "run": trace(status="failed")},
        {"exit_code": 1, "run": trace(), "error_code": "record_failed"},
        {"exit_code": 1, "run": None},
        {"exit_code": True, "run": trace()},
    ],
)
def test_record_result_rejects_inconsistent_states(kwargs: dict[str, object]):
    with pytest.raises(CIOutputError):
        build_record_result(**kwargs)


def test_assertion_result_separates_mismatch_indeterminate_and_error():
    mismatch = build_assertion_result(
        exit_code=1,
        outcome="mismatch",
        run_id="run-1",
        source="inline",
        case=None,
        failures=[{"code": "assertion_mismatch", "location": "status"}],
    )
    assert mismatch["error"] is None
    assert mismatch["failures"] == [
        {"code": "assertion_mismatch", "kind": "mismatch", "location": "status"}
    ]

    indeterminate = build_assertion_result(
        exit_code=1,
        outcome="indeterminate",
        run_id="run-1",
        source="file",
        case="smoke",
        failures=[{"code": "artifact_decode_unknown", "location": "stdout_contains"}],
    )
    assert indeterminate["failures"][0]["kind"] == "indeterminate"

    error = build_assertion_result(
        exit_code=2,
        outcome="error",
        run_id=None,
        source="file",
        case="smoke",
        failures=[],
        error_code="assertion_file_schema_invalid",
        error_location="cases.smoke.status",
    )
    assert error["error"] == "assertion_file_schema_invalid"
    assert error["failures"] == []


def test_assertion_result_rejects_crossed_outcome_or_unsafe_location():
    with pytest.raises(CIOutputError):
        build_assertion_result(
            exit_code=1,
            outcome="mismatch",
            run_id="run-1",
            source="inline",
            case=None,
            failures=[{"code": "scan_incomplete", "location": "stdout_contains"}],
        )
    with pytest.raises(CIOutputError):
        build_assertion_result(
            exit_code=2,
            outcome="error",
            run_id=None,
            source="file",
            case=None,
            failures=[],
            error_code="assertion_file_schema_invalid",
            error_location="cases.secret\npath",
        )


def test_assertion_result_can_hide_a_non_ci_safe_operational_id():
    result = build_assertion_result(
        exit_code=0,
        outcome="pass",
        run_id=None,
        source="inline",
        case=None,
        failures=[],
    )

    assert result["run_id"] is None
    assert result["outcome"] == "pass"


def test_list_result_bounds_counts_and_projects_runs():
    result = build_list_result(
        exit_code=0,
        runs=[trace()],
        skipped=3,
        truncated=True,
    )
    assert result["ok"] is True
    assert result["runs"] == [project_list_run(trace())]
    with pytest.raises(CIOutputError):
        build_list_result(exit_code=0, runs=[], skipped=True, truncated=False)


def test_json_serialization_is_compact_sorted_utf8_with_one_lf():
    assert serialize_json({"z": "é", "a": 1}) == b'{"a":1,"z":"\xc3\xa9"}\n'
    with pytest.raises(CIOutputError):
        serialize_json({"bad": float("nan")})


class PartialBytes(io.BytesIO):
    def write(self, data: bytes) -> int:
        super().write(data[:-1])
        return len(data) - 1


class FlushFailure(io.BytesIO):
    def flush(self) -> None:
        raise OSError("closed")


def test_json_emitter_is_one_shot_and_requires_complete_flush():
    stream = io.BytesIO()
    emitter = JsonEmitter(stream=stream)
    assert emitter.emit({"ok": True}) is True
    assert stream.getvalue() == b'{"ok":true}\n'
    assert emitter.emit({"ok": False}) is False
    assert stream.getvalue() == b'{"ok":true}\n'

    partial = JsonEmitter(stream=PartialBytes())
    assert partial.emit({"ok": True}) is False
    assert partial.emitted is False
    assert partial.emit({"ok": True}) is False

    flush_failure = JsonEmitter(stream=FlushFailure())
    assert flush_failure.emit({"ok": True}) is False
    assert flush_failure.emitted is False
