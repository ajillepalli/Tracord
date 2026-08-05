from dataclasses import asdict
import json
import re

import pytest

import tracord.redaction as redaction_module
from tracord.redaction import (
    REDACTION,
    REDACTION_RULES,
    SECRET_PATTERNS,
    RedactionRule,
    ReplacementStrategy,
    redact_text,
    summarize_redactions,
)


GOLDEN_REDACTIONS = (
    ("plain output", "plain output"),
    ("token=abc123", f"token={REDACTION}"),
    ("API-key: 'quoted-value'", f"API-key={REDACTION}'"),
    ("password = hunter2; next", f"password={REDACTION}; next"),
    ("value sk-abcdefghijklmnopqrstuvwxyz", f"value {REDACTION}"),
    ("token " + "ghp_" + "abcdefghijklmnopqrstuvwxyz", f"token {REDACTION}"),
    (
        "commit 0123456789abcdef0123456789abcdef01234567",
        f"commit {REDACTION}",
    ),
    (
        "token=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop",
        f"token={REDACTION}",
    ),
    (
        "before\ntoken=\nvalue\nafter",
        f"before\ntoken={REDACTION}\nafter",
    ),
    (f"token={REDACTION}", f"token={REDACTION}"),
)

LEGACY_PATTERN_SOURCES = [
    r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?([^\s'\";,]+)",
    r"\bsk-[A-Za-z0-9_-]{16,}\b",
    r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b",
    r"\b[A-Za-z0-9+/]{40,}={0,2}\b",
]

COMPATIBILITY_INPUTS = tuple(value for value, _expected in GOLDEN_REDACTIONS) + (
    "",
    "token=first password=second",
    "sk-abcdefghijklmnopqrstuvwxyz " + "ghp_" + "abcdefghijklmnopqrstuvwxyz",
)


def _legacy_redact(value: str) -> str:
    redacted = value
    for source in LEGACY_PATTERN_SOURCES:
        pattern = re.compile(source)
        if pattern.groups >= 2:
            redacted = pattern.sub(
                lambda match: f"{match.group(1)}={REDACTION}", redacted
            )
        else:
            redacted = pattern.sub(REDACTION, redacted)
    return redacted


@pytest.mark.parametrize(("value", "expected"), GOLDEN_REDACTIONS)
def test_redaction_matches_golden_corpus(value, expected):
    assert redact_text(value) == expected


def test_rule_order_and_replacement_strategies_are_explicit():
    assert [rule.name for rule in REDACTION_RULES] == [
        "named_secret_assignment",
        "openai_api_key",
        "github_token",
        "encoded_secret_candidate",
    ]
    assert [rule.replacement for rule in REDACTION_RULES] == [
        ReplacementStrategy.NAMED_ASSIGNMENT,
        ReplacementStrategy.FULL_MATCH,
        ReplacementStrategy.FULL_MATCH,
        ReplacementStrategy.FULL_MATCH,
    ]
    assert [pattern.pattern for pattern in SECRET_PATTERNS] == LEGACY_PATTERN_SOURCES
    assert [pattern.groups for pattern in SECRET_PATTERNS] == [2, 0, 0, 0]


@pytest.mark.parametrize("value", COMPATIBILITY_INPUTS)
def test_redact_text_matches_legacy_implementation(value):
    assert redact_text(value) == _legacy_redact(value)


def test_named_assignment_strategy_requires_capture_groups():
    with pytest.raises(ValueError, match="require at least two groups"):
        RedactionRule(
            name="invalid",
            pattern=re.compile(r"secret"),
            replacement=ReplacementStrategy.NAMED_ASSIGNMENT,
            gating=True,
        )


def test_string_replacement_strategy_is_normalized_and_validated():
    with pytest.raises(ValueError, match="require at least two groups"):
        RedactionRule(
            name="invalid",
            pattern=re.compile(r"secret"),
            replacement="named_assignment",
            gating=True,
        )

    rule = RedactionRule(
        name="valid",
        pattern=re.compile(r"secret"),
        replacement="full_match",
        gating=True,
    )
    assert rule.replacement is ReplacementStrategy.FULL_MATCH

    with pytest.raises(ValueError, match="not a valid ReplacementStrategy"):
        RedactionRule(
            name="invalid",
            pattern=re.compile(r"secret"),
            replacement="bogus",
            gating=True,
        )


def test_nonparticipating_named_groups_fail_closed(monkeypatch):
    rule = RedactionRule(
        name="optional",
        pattern=re.compile(r"(token)?(=value)?fallback"),
        replacement=ReplacementStrategy.NAMED_ASSIGNMENT,
        gating=True,
    )
    monkeypatch.setattr(redaction_module, "REDACTION_RULES", (rule,))

    assert redact_text("fallback") == REDACTION


def test_summary_classifies_already_redacted_assignments_separately():
    summary = summarize_redactions(f"token={REDACTION}")

    assert summary.findings_total == 0
    assert summary.gating_total == 0
    assert summary.advisory_total == 0
    assert summary.already_redacted_total == 1
    assert summary.rules[0].rule == "named_secret_assignment"
    assert summary.rules[0].findings == 0
    assert summary.rules[0].already_redacted == 1


@pytest.mark.parametrize(("value", "_expected"), GOLDEN_REDACTIONS)
def test_redacted_golden_corpus_has_no_gating_findings(value, _expected):
    assert summarize_redactions(redact_text(value)).gating_total == 0


def test_encoded_secret_candidate_is_advisory():
    summary = summarize_redactions(
        "commit 0123456789abcdef0123456789abcdef01234567"
    )

    assert summary.findings_total == 1
    assert summary.gating_total == 0
    assert summary.advisory_total == 1
    assert summary.rules[0].rule == "encoded_secret_candidate"
    assert summary.rules[0].gating is False


def test_overlapping_rules_are_counted_independently():
    secret = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop"
    summary = summarize_redactions(f"token={secret}")

    assert summary.findings_total == 2
    assert summary.gating_total == 1
    assert summary.advisory_total == 1
    assert summary.findings_total == summary.gating_total + summary.advisory_total


def test_summary_serialization_cannot_expose_secret_value():
    secret = "a-unique-secret-value"
    summary = summarize_redactions(f"token={secret}")
    serialized = json.dumps(asdict(summary), sort_keys=True)

    assert secret not in serialized
    assert secret not in repr(summary)


@pytest.mark.parametrize("value", COMPATIBILITY_INPUTS)
def test_summary_aggregate_counts_match_rule_counts(value):
    summary = summarize_redactions(value)

    assert sum(rule.findings for rule in summary.rules) == summary.findings_total
    assert (
        sum(rule.already_redacted for rule in summary.rules)
        == summary.already_redacted_total
    )
    assert summary.findings_total == summary.gating_total + summary.advisory_total
