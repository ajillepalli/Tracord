"""Small default redaction helpers for local trace capture."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


REDACTION = "[REDACTED]"
MAX_DISPLAY_LABEL_CHARS = 256


class ReplacementStrategy(StrEnum):
    """Supported ways to replace a rule match."""

    NAMED_ASSIGNMENT = "named_assignment"
    FULL_MATCH = "full_match"


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """A stable redaction rule and its replacement and gating semantics."""

    name: str
    pattern: re.Pattern[str]
    replacement: ReplacementStrategy | str
    gating: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "replacement", ReplacementStrategy(self.replacement))
        if (
            self.replacement is ReplacementStrategy.NAMED_ASSIGNMENT
            and self.pattern.groups < 2
        ):
            raise ValueError("named assignment rules require at least two groups")


@dataclass(frozen=True, slots=True)
class RuleRedactionSummary:
    """Count-only findings for one redaction rule."""

    rule: str
    gating: bool
    findings: int
    already_redacted: int


@dataclass(frozen=True, slots=True)
class RedactionSummary:
    """Count-only redaction findings with no matched content or locations."""

    rules: tuple[RuleRedactionSummary, ...]
    findings_total: int
    gating_total: int
    advisory_total: int
    already_redacted_total: int


REDACTION_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        name="named_secret_assignment",
        pattern=re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?([^\s'\";,]+)"
        ),
        replacement=ReplacementStrategy.NAMED_ASSIGNMENT,
        gating=True,
    ),
    RedactionRule(
        name="openai_api_key",
        pattern=re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        replacement=ReplacementStrategy.FULL_MATCH,
        gating=True,
    ),
    RedactionRule(
        name="github_token",
        pattern=re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        replacement=ReplacementStrategy.FULL_MATCH,
        gating=True,
    ),
    RedactionRule(
        name="encoded_secret_candidate",
        pattern=re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
        replacement=ReplacementStrategy.FULL_MATCH,
        gating=False,
    ),
)

# Retain the original pattern collection for callers that imported it directly.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    rule.pattern for rule in REDACTION_RULES
)


def _is_already_redacted(rule: RedactionRule, match: re.Match[str]) -> bool:
    return (
        rule.replacement is ReplacementStrategy.NAMED_ASSIGNMENT
        and match.group(2) == REDACTION
    )


def _replace_match(rule: RedactionRule, match: re.Match[str]) -> str:
    if rule.replacement is ReplacementStrategy.NAMED_ASSIGNMENT:
        key = match.group(1)
        value = match.group(2)
        if key is None or value is None:
            return REDACTION
        return f"{key}={REDACTION}"
    return REDACTION


def summarize_redactions(value: str) -> RedactionSummary:
    """Count potential secrets without retaining values, offsets, or excerpts.

    Rules inspect the original value independently, so one substring may count
    for more than one rule. Rules with no matches are omitted from ``rules``.
    Named assignments containing the placeholder are reported as already
    redacted and never count as findings; placeholders from full-match rules
    cannot be attributed to their original rule. Callers must bound untrusted
    input before invoking this function.
    """
    summaries: list[RuleRedactionSummary] = []
    findings_total = 0
    gating_total = 0
    advisory_total = 0
    already_redacted_total = 0

    for rule in REDACTION_RULES:
        findings = 0
        already_redacted = 0
        for match in rule.pattern.finditer(value):
            if _is_already_redacted(rule, match):
                already_redacted += 1
            else:
                findings += 1

        if findings or already_redacted:
            summaries.append(
                RuleRedactionSummary(
                    rule=rule.name,
                    gating=rule.gating,
                    findings=findings,
                    already_redacted=already_redacted,
                )
            )
        findings_total += findings
        already_redacted_total += already_redacted
        if rule.gating:
            gating_total += findings
        else:
            advisory_total += findings

    return RedactionSummary(
        rules=tuple(summaries),
        findings_total=findings_total,
        gating_total=gating_total,
        advisory_total=advisory_total,
        already_redacted_total=already_redacted_total,
    )


def redact_text(value: str) -> str:
    """Redact obvious secrets without pretending to be a full DLP engine.

    Runtime is linear in the input size. Callers processing untrusted data should
    apply an appropriate input bound before calling when throughput is a concern.
    """
    redacted = value
    for rule in REDACTION_RULES:
        redacted = rule.pattern.sub(
            lambda match, current_rule=rule: _replace_match(current_rule, match),
            redacted,
        )
    return redacted


def sanitize_label(value: str) -> str:
    """Redact and escape untrusted text before terminal or JSON display."""
    characters: list[str] = []
    for character in redact_text(value):
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            characters.append(f"\\u{ord(character):04x}")
        else:
            characters.append(character)
    sanitized = "".join(characters)
    if len(sanitized) > MAX_DISPLAY_LABEL_CHARS:
        return sanitized[:MAX_DISPLAY_LABEL_CHARS] + "...[TRUNCATED]"
    return sanitized
