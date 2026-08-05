import json

import pytest
from jsonschema import Draft202012Validator

from tracord.ci_output import (
    build_assertion_result,
    build_list_result,
    build_record_result,
    build_replay_result,
)
from tracord.result_codes import (
    ASSERTION_ERROR_CODES,
    ASSERTION_ERROR_LOCATION_SCHEMA_PATTERN,
    ASSERTION_FAILURE_CODES,
    CI_RUN_ID_SCHEMA_PATTERN,
    LIST_ERROR_CODES,
    MAX_LIST_RUNS,
    MAX_PROCESS_EXIT_CODE,
    MAX_SAFE_JSON_INTEGER,
    RECORD_ERROR_CODES,
    REPLAY_ERROR_CODES,
)
from tracord.schemas import schema_resource


SCHEMA_NAMES = {
    "record": "record-result-v0.schema.json",
    "replay": "replay-result-v0.schema.json",
    "assert": "assertion-result-v0.schema.json",
    "list": "list-result-v0.schema.json",
}


def load_schema(command: str) -> dict[str, object]:
    return json.loads(schema_resource(SCHEMA_NAMES[command]).read_text(encoding="utf-8"))


def trace(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "run_id": "run-1",
        "status": "passed",
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 1,
        "redacted": True,
        "decode_replacement": {"stdout": "none", "stderr": "none"},
        "store_identity_verified": True,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("command", SCHEMA_NAMES)
def test_packaged_result_schemas_are_valid_draft_2020_12(command: str):
    Draft202012Validator.check_schema(load_schema(command))


def test_all_constructor_results_validate_against_packaged_schemas():
    payloads = {
        "record": build_record_result(exit_code=0, run=trace()),
        "replay": build_replay_result(
            exit_code=1,
            run=trace(status="timeout", exit_code=None, timed_out=True),
        ),
        "assert": build_assertion_result(
            exit_code=1,
            outcome="indeterminate",
            run_id="run-1",
            source="inline",
            case=None,
            failures=[{"code": "scan_incomplete", "location": "stdout_contains"}],
        ),
        "list": build_list_result(
            exit_code=0,
            runs=[
                trace(
                    status="failed",
                    duration_ms=MAX_SAFE_JSON_INTEGER,
                    exit_code=MAX_PROCESS_EXIT_CODE,
                )
            ],
            skipped=MAX_SAFE_JSON_INTEGER,
            truncated=True,
        ),
    }
    for command, payload in payloads.items():
        Draft202012Validator(load_schema(command)).validate(payload)


def test_schema_error_vocabularies_equal_leaf_owned_sets():
    assert set(load_schema("record")["properties"]["error"]["enum"]) - {None} == set(
        RECORD_ERROR_CODES
    )
    assert set(load_schema("replay")["properties"]["error"]["enum"]) - {None} == set(
        REPLAY_ERROR_CODES
    )
    assert set(load_schema("assert")["properties"]["error"]["enum"]) - {None} == set(
        ASSERTION_ERROR_CODES
    )
    assert set(load_schema("list")["properties"]["error"]["enum"]) - {None} == set(
        LIST_ERROR_CODES
    )
    assert set(
        load_schema("assert")["$defs"]["failure"]["properties"]["code"]["enum"]
    ) == set(ASSERTION_FAILURE_CODES)


def test_schema_patterns_equal_leaf_owned_patterns():
    assert load_schema("record")["$defs"]["runId"]["pattern"] == CI_RUN_ID_SCHEMA_PATTERN
    assert load_schema("replay")["$defs"]["runId"]["pattern"] == CI_RUN_ID_SCHEMA_PATTERN
    assert (
        load_schema("list")["$defs"]["listRun"]["properties"]["run_id"]["pattern"]
        == CI_RUN_ID_SCHEMA_PATTERN
    )
    assertion = load_schema("assert")
    assert assertion["$defs"]["id"]["pattern"] == CI_RUN_ID_SCHEMA_PATTERN
    assert (
        assertion["properties"]["error_location"]["pattern"]
        == ASSERTION_ERROR_LOCATION_SCHEMA_PATTERN
    )
    assert load_schema("list")["properties"]["runs"]["maxItems"] == MAX_LIST_RUNS


@pytest.mark.parametrize("command", SCHEMA_NAMES)
def test_schemas_reject_unknown_properties(command: str):
    payload = {
        "record": build_record_result(exit_code=0, run=trace()),
        "replay": build_replay_result(exit_code=0, run=trace()),
        "assert": build_assertion_result(
            exit_code=0,
            outcome="pass",
            run_id="run-1",
            source="inline",
            case=None,
            failures=[],
        ),
        "list": build_list_result(exit_code=0, runs=[], skipped=0, truncated=False),
    }[command]
    payload["secret"] = "must not pass"
    assert list(Draft202012Validator(load_schema(command)).iter_errors(payload))
