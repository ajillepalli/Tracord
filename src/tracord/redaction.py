"""Small default redaction helpers for local trace capture."""

from __future__ import annotations

import re


REDACTION = "[REDACTED]"

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?([^\s'\";,]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)


def redact_text(value: str) -> str:
    """Redact obvious secrets without pretending to be a full DLP engine."""
    redacted = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: f"{match.group(1)}={REDACTION}", redacted)
        else:
            redacted = pattern.sub(REDACTION, redacted)
    return redacted
