from tracord.redaction import REDACTION, redact_text


def test_redacts_named_secret():
    assert redact_text("token=abc123") == f"token={REDACTION}"


def test_redacts_openai_style_secret():
    assert redact_text("value sk-abcdefghijklmnopqrstuvwxyz") == f"value {REDACTION}"
