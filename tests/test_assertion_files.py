import json
import os
from pathlib import Path

import pytest

from tracord import assertion_files
from tracord.assertion_files import (
    MAX_ASSERTION_FILE_BYTES,
    MAX_EXPECTATION_TEXT_BYTES,
    AssertionFileError,
    load_assertion_case,
    parse_assertion_case,
)


def descriptor(cases: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": "tracord.assertions.v0",
            "cases": cases if cases is not None else {"smoke": {"status": "passed"}},
        }
    ).encode()


def assert_error(
    data: bytes, code: str, location: str | None = None, case_name: str = "smoke"
) -> None:
    with pytest.raises(AssertionFileError) as caught:
        parse_assertion_case(data, case_name)
    assert caught.value.code == code
    assert caught.value.location == location


def test_parse_selects_exact_case_and_returns_expectations():
    result = parse_assertion_case(
        descriptor(
            {
                "smoke": {
                    "status": "passed",
                    "exit_code": -9,
                    "stdout_contains": "ready",
                    "stderr_contains": "notice",
                    "max_duration_ms": 0,
                    "no_timeout": True,
                },
                "failure": {"status": "failed"},
            }
        ),
        "smoke",
    )

    assert result.status == "passed"
    assert result.exit_code == -9
    assert result.stdout_contains == "ready"
    assert result.stderr_contains == "notice"
    assert result.max_duration_ms == 0
    assert result.no_timeout is True


def test_parse_requires_exact_case_name():
    assert_error(descriptor(), "case_not_found", case_name="SMOKE")


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xef\xbb\xbf{}", "assertion_file_bom"),
        (b"\xff", "assertion_file_invalid_utf8"),
        (b"{", "assertion_file_invalid_json"),
        (b'{"schema_version":"x","schema_version":"y","cases":{}}', "assertion_file_duplicate_key"),
        (b'{"schema_version":"tracord.assertions.v0","cases":{"smoke":{"exit_code":NaN}}}', "assertion_file_invalid_json"),
        (b'{"schema_version":"tracord.assertions.v0","cases":{"smoke":{"exit_code":1e999}}}', "assertion_file_invalid_json"),
        (
            b'{"schema_version":"tracord.assertions.v0","cases":{"smoke":{"exit_code":'
            + b"1" * 129
            + b"}}}",
            "assertion_file_invalid_json",
        ),
        (b"[" * 2000 + b"]" * 2000, "assertion_file_invalid_json"),
    ],
)
def test_parse_rejects_invalid_encodings_and_json(data: bytes, code: str):
    assert_error(data, code)


def test_parse_rejects_data_over_file_limit():
    assert_error(b" " * (MAX_ASSERTION_FILE_BYTES + 1), "assertion_file_too_large")


@pytest.mark.parametrize(
    ("document", "location"),
    [
        ([], None),
        ({"cases": {}}, "schema_version"),
        ({"schema_version": "tracord.assertions.v0"}, "cases"),
        ({"schema_version": "wrong", "cases": {}}, "schema_version"),
        ({"schema_version": "tracord.assertions.v0", "cases": [],}, "cases"),
        ({"schema_version": "tracord.assertions.v0", "cases": {}, "extra": 1}, None),
    ],
)
def test_parse_rejects_invalid_top_level_shape(document: object, location: str | None):
    assert_error(json.dumps(document).encode(), "assertion_file_schema_invalid", location)


def test_parse_rejects_more_than_256_cases():
    cases = {f"case-{index:03d}": {"status": "passed"} for index in range(257)}
    assert_error(descriptor(cases), "assertion_file_schema_invalid", "cases")


@pytest.mark.parametrize("name", ["", ".smoke", "has space", "naive\N{LATIN SMALL LETTER I WITH DIAERESIS}", "a" * 129])
def test_parse_rejects_nonportable_case_names(name: str):
    assert_error(
        descriptor({name: {"status": "passed"}}),
        "assertion_file_schema_invalid",
        "cases",
    )


def test_parse_rejects_ascii_casefold_collision():
    assert_error(
        descriptor({"Smoke": {"status": "passed"}, "smoke": {"status": "passed"}}),
        "assertion_file_schema_invalid",
        "cases.smoke",
    )


@pytest.mark.parametrize(
    ("case", "location"),
    [
        ([], "cases.smoke"),
        ({}, "cases.smoke"),
        ({"unknown": 1}, "cases.smoke"),
        ({"status": "unknown"}, "cases.smoke.status"),
        ({"status": True}, "cases.smoke.status"),
        ({"status": []}, "cases.smoke.status"),
        ({"exit_code": True}, "cases.smoke.exit_code"),
        ({"stdout_contains": ""}, "cases.smoke.stdout_contains"),
        ({"stderr_contains": None}, "cases.smoke.stderr_contains"),
        ({"max_duration_ms": -1}, "cases.smoke.max_duration_ms"),
        ({"max_duration_ms": False}, "cases.smoke.max_duration_ms"),
        ({"no_timeout": False}, "cases.smoke.no_timeout"),
    ],
)
def test_parse_rejects_ineffective_or_invalid_expectations(
    case: object, location: str
):
    assert_error(
        descriptor({"smoke": case}), "assertion_file_schema_invalid", location
    )


def test_parse_applies_utf8_byte_limit_to_containment_values():
    valid = "x" * MAX_EXPECTATION_TEXT_BYTES
    assert parse_assertion_case(descriptor({"smoke": {"stdout_contains": valid}}), "smoke")

    oversized = "\N{EURO SIGN}" * ((MAX_EXPECTATION_TEXT_BYTES // 3) + 1)
    assert_error(
        descriptor({"smoke": {"stdout_contains": oversized}}),
        "assertion_file_schema_invalid",
        "cases.smoke.stdout_contains",
    )


def test_parse_validates_all_cases_before_selection():
    assert_error(
        descriptor(
            {
                "selected": {"status": "passed"},
                "z-invalid": {"no_timeout": False},
            }
        ),
        "assertion_file_schema_invalid",
        "cases.z-invalid.no_timeout",
        case_name="selected",
    )


def test_validation_order_is_ascii_case_then_frozen_field_order():
    assert_error(
        descriptor(
            {
                "z": {"status": "bad"},
                "A": {"exit_code": True, "max_duration_ms": -1},
            }
        ),
        "assertion_file_schema_invalid",
        "cases.A.exit_code",
        case_name="A",
    )


def test_parse_uses_shared_expectation_validation(monkeypatch: pytest.MonkeyPatch):
    seen = []

    def reject(expectations: object) -> None:
        seen.append(expectations)
        raise assertion_files.assertions.ExpectationValidationError(
            "assertion_value_invalid"
        )

    monkeypatch.setattr(
        assertion_files.assertions,
        "validate_expectations",
        reject,
    )

    assert_error(
        descriptor(), "assertion_file_schema_invalid", "cases.smoke"
    )
    assert len(seen) == 1


def test_load_reads_a_regular_single_link_file_safely(tmp_path: Path):
    path = tmp_path / "assertions.json"
    path.write_bytes(descriptor())

    assert load_assertion_case(path, "smoke").status == "passed"


def test_load_reports_missing_without_disclosing_path(tmp_path: Path):
    secret_path = tmp_path / "secret-name.json"
    with pytest.raises(AssertionFileError) as caught:
        load_assertion_case(secret_path, "smoke")

    assert caught.value.code == "assertion_file_missing"
    assert str(secret_path) not in str(caught.value)


def test_load_rejects_oversized_file(tmp_path: Path):
    path = tmp_path / "assertions.json"
    path.write_bytes(b" " * (MAX_ASSERTION_FILE_BYTES + 1))

    with pytest.raises(AssertionFileError) as caught:
        load_assertion_case(path, "smoke")
    assert caught.value.code == "assertion_file_too_large"


def test_load_rejects_a_symlinked_parent(tmp_path: Path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "assertions.json").write_bytes(descriptor())
    linked = tmp_path / "linked"
    try:
        os.symlink(external, linked, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(AssertionFileError) as caught:
        load_assertion_case(linked / "assertions.json", "smoke")
    assert caught.value.code == "assertion_file_unreadable"


def test_load_resolves_parent_segments_lexically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    assertion_path = tmp_path / "assertions.json"
    assertion_path.write_bytes(descriptor())
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)

    assert load_assertion_case(Path("../assertions.json"), "smoke").status == "passed"


def test_load_fails_closed_when_snapshot_identity_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "assertions.json"
    path.write_bytes(descriptor())
    monkeypatch.setattr(
        assertion_files.paths,
        "verify_opened_file",
        lambda opened: assertion_files.paths.IdentityComparison.UNAVAILABLE,
    )

    with pytest.raises(AssertionFileError) as caught:
        load_assertion_case(path, "smoke")
    assert caught.value.code == "assertion_file_unreadable"


def test_error_rejects_unapproved_codes_and_unsafe_locations():
    with pytest.raises(ValueError):
        AssertionFileError("surprise")
    with pytest.raises(ValueError):
        AssertionFileError("assertion_file_schema_invalid", "cases.secret value")


def test_repository_example_parses():
    example = Path("examples/.tracord/assertions.json").read_bytes()

    parsed = parse_assertion_case(example, "smoke")

    assert parsed.status == "passed"
    assert parsed.no_timeout is True


def test_schema_declares_the_frozen_format_and_fields():
    schema = json.loads(Path("schemas/assertions-v0.schema.json").read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"]["const"] == "tracord.assertions.v0"
    assert tuple(schema["$defs"]["case"]["properties"]) == (
        "status",
        "exit_code",
        "stdout_contains",
        "stderr_contains",
        "max_duration_ms",
        "no_timeout",
    )
