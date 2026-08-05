"""Strict loading for versioned repository assertion descriptors."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

from . import assertions, paths
from .assertions import TraceExpectations


ASSERTION_FILE_VERSION = "tracord.assertions.v0"
MAX_ASSERTION_FILE_BYTES = 1024 * 1024
MAX_ASSERTION_CASES = 256
MAX_EXPECTATION_TEXT_BYTES = 64 * 1024
MAX_INTEGER_DIGITS = 128
MAX_JSON_NESTING = 64

_CASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_EXPECTATION_FIELDS = (
    "status",
    "exit_code",
    "stdout_contains",
    "stderr_contains",
    "max_duration_ms",
    "no_timeout",
)
_TOP_LEVEL_FIELDS = ("schema_version", "cases")
_ERROR_CODES = frozenset(
    {
        "assertion_file_not_found",
        "assertion_file_unsafe",
        "assertion_file_unreadable",
        "assertion_file_too_large",
        "assertion_file_invalid_utf8",
        "assertion_file_invalid_json",
        "assertion_file_invalid",
        "assertion_case_not_found",
    }
)
_SAFE_LOCATION = re.compile(
    r"(?:schema_version|cases(?:\.[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:\.(?:status|exit_code|stdout_contains|stderr_contains|max_duration_ms|no_timeout))?)?)",
    re.ASCII,
)


class AssertionFileError(ValueError):
    """A path-free, content-free assertion descriptor error."""

    def __init__(self, code: str, location: str | None = None) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("unknown assertion file error code")
        if location is not None and _SAFE_LOCATION.fullmatch(location) is None:
            raise ValueError("unsafe assertion file error location")
        self.code = code
        self.location = location
        message = code if location is None else f"{code}: {location}"
        super().__init__(message)


def load_assertion_case(path: Path, case_name: str) -> TraceExpectations:
    """Safely load and select one exact assertion case from *path*."""
    return parse_assertion_case(_read_assertion_file(path), case_name)


def parse_assertion_case(data: bytes, case_name: str) -> TraceExpectations:
    """Parse descriptor bytes and select a case after validating the whole file."""
    document = _decode_document(data)
    cases = _validate_document(document)
    selected = cases.get(case_name)
    if selected is None:
        raise AssertionFileError("assertion_case_not_found")
    return selected


def _read_assertion_file(path: Path) -> bytes:
    try:
        containment = paths.check_containment(path.parent, path)
    except (OSError, ValueError):
        raise AssertionFileError("assertion_file_unsafe") from None
    if not _containment_passed(containment):
        raise AssertionFileError("assertion_file_unsafe")

    try:
        path_before = paths.stat_regular_no_follow(path)
    except FileNotFoundError:
        raise AssertionFileError("assertion_file_not_found") from None
    except (OSError, ValueError):
        raise AssertionFileError("assertion_file_unsafe") from None
    _validate_file_snapshot(path_before)
    if path_before.st_size > MAX_ASSERTION_FILE_BYTES:
        raise AssertionFileError("assertion_file_too_large")

    try:
        opened = paths.open_regular_no_follow(path)
        with _binary_stream(opened) as stream:
            descriptor_before = os.fstat(stream.fileno())
            _validate_file_snapshot(descriptor_before)
            if not _same_snapshot(path_before, descriptor_before):
                raise AssertionFileError("assertion_file_unsafe")
            data = stream.read(MAX_ASSERTION_FILE_BYTES + 1)
            descriptor_after = os.fstat(stream.fileno())
    except AssertionFileError:
        raise
    except FileNotFoundError:
        raise AssertionFileError("assertion_file_not_found") from None
    except (OSError, ValueError):
        raise AssertionFileError("assertion_file_unreadable") from None

    if len(data) > MAX_ASSERTION_FILE_BYTES:
        raise AssertionFileError("assertion_file_too_large")
    _validate_file_snapshot(descriptor_after)
    if not _same_snapshot(descriptor_before, descriptor_after):
        raise AssertionFileError("assertion_file_unsafe")

    try:
        path_after = paths.stat_regular_no_follow(path)
        containment = paths.check_containment(path.parent, path)
    except (OSError, ValueError):
        raise AssertionFileError("assertion_file_unsafe") from None
    _validate_file_snapshot(path_after)
    if not _same_snapshot(descriptor_after, path_after) or not _containment_passed(
        containment
    ):
        raise AssertionFileError("assertion_file_unsafe")
    return data


def _binary_stream(opened: BinaryIO | int) -> BinaryIO:
    if isinstance(opened, int):
        return os.fdopen(opened, "rb", closefd=True)
    return opened


def _validate_file_snapshot(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size < 0
    ):
        raise AssertionFileError("assertion_file_unsafe")


def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return paths.same_file_snapshot(first, second) is paths.IdentityResult.SAME


def _containment_passed(result: object) -> bool:
    return result is None or result is True or result is paths.IdentityResult.SAME


def _decode_document(data: bytes) -> object:
    if len(data) > MAX_ASSERTION_FILE_BYTES:
        raise AssertionFileError("assertion_file_too_large")
    if data.startswith(b"\xef\xbb\xbf"):
        raise AssertionFileError("assertion_file_invalid_utf8")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise AssertionFileError("assertion_file_invalid_utf8") from None
    if _json_nesting_exceeds_limit(text):
        raise AssertionFileError("assertion_file_invalid_json")
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_nonfinite,
            parse_float=_finite_float,
            parse_int=_bounded_integer,
        )
    except (json.JSONDecodeError, _InvalidJsonValue, ValueError, RecursionError):
        raise AssertionFileError("assertion_file_invalid_json") from None


class _InvalidJsonValue(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJsonValue
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> float:
    raise _InvalidJsonValue


def _bounded_integer(value: str) -> int:
    if len(value.removeprefix("-")) > MAX_INTEGER_DIGITS:
        raise _InvalidJsonValue
    return int(value)


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise _InvalidJsonValue
    return result


def _json_nesting_exceeds_limit(text: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                return True
        elif character in "]}":
            depth = max(0, depth - 1)
    return False


def _validate_document(document: object) -> dict[str, TraceExpectations]:
    if not isinstance(document, Mapping):
        raise AssertionFileError("assertion_file_invalid")

    errors: list[AssertionFileError] = []
    keys = set(document)
    for field in _TOP_LEVEL_FIELDS:
        if field not in keys:
            errors.append(AssertionFileError("assertion_file_invalid", field))
    if keys.difference(_TOP_LEVEL_FIELDS):
        errors.append(AssertionFileError("assertion_file_invalid"))

    version = document.get("schema_version")
    if version != ASSERTION_FILE_VERSION:
        errors.append(AssertionFileError("assertion_file_invalid", "schema_version"))

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, Mapping):
        errors.append(AssertionFileError("assertion_file_invalid", "cases"))
        raise errors[0]
    if len(raw_cases) > MAX_ASSERTION_CASES:
        errors.append(AssertionFileError("assertion_file_invalid", "cases"))

    names = sorted(raw_cases)
    folded_names: set[str] = set()
    valid_names: list[str] = []
    for name in names:
        if not isinstance(name, str) or _CASE_NAME.fullmatch(name) is None:
            errors.append(AssertionFileError("assertion_file_invalid", "cases"))
            continue
        folded = name.lower()
        if folded in folded_names:
            errors.append(AssertionFileError("assertion_file_invalid", f"cases.{name}"))
        else:
            folded_names.add(folded)
        valid_names.append(name)

    parsed: dict[str, TraceExpectations] = {}
    for name in valid_names:
        expectation, case_errors = _validate_case(name, raw_cases[name])
        errors.extend(case_errors)
        if expectation is not None:
            parsed[name] = expectation

    if errors:
        raise errors[0]
    return parsed


def _validate_case(
    name: str, value: object
) -> tuple[TraceExpectations | None, list[AssertionFileError]]:
    location = f"cases.{name}"
    if not isinstance(value, Mapping):
        return None, [AssertionFileError("assertion_file_invalid", location)]

    errors: list[AssertionFileError] = []
    keys = set(value)
    if not keys or keys.difference(_EXPECTATION_FIELDS):
        errors.append(AssertionFileError("assertion_file_invalid", location))

    values: dict[str, Any] = {}
    for field in _EXPECTATION_FIELDS:
        if field not in value:
            continue
        field_location = f"{location}.{field}"
        raw = value[field]
        if not _valid_field(field, raw):
            errors.append(AssertionFileError("assertion_file_invalid", field_location))
        else:
            values[field] = raw

    if errors:
        return None, errors

    expectation = TraceExpectations(**values)
    if assertions.validate_expectations(expectation):
        errors.append(AssertionFileError("assertion_file_invalid", location))
        return None, errors
    return expectation, errors


def _valid_field(field: str, value: object) -> bool:
    if field == "status":
        return isinstance(value, str) and value in {"passed", "failed", "timeout"}
    if field == "exit_code":
        return isinstance(value, int) and not isinstance(value, bool)
    if field in {"stdout_contains", "stderr_contains"}:
        if not isinstance(value, str) or not value:
            return False
        try:
            return len(value.encode("utf-8", errors="strict")) <= MAX_EXPECTATION_TEXT_BYTES
        except UnicodeEncodeError:
            return False
    if field == "max_duration_ms":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if field == "no_timeout":
        return value is True
    raise AssertionError("unreachable expectation field")
