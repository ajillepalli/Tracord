from pathlib import Path

import pytest

from tracord.paths import safe_join, validate_relative_path


def test_validate_relative_path_accepts_nested_forward_slash_path():
    assert validate_relative_path("artifacts/stdout.log") == []


def test_validate_relative_path_rejects_parent_segments():
    assert validate_relative_path("../outside.txt")


def test_validate_relative_path_rejects_windows_drive():
    assert validate_relative_path("C:/outside.txt")


def test_safe_join_rejects_escaping_path(tmp_path: Path):
    with pytest.raises(ValueError):
        safe_join(tmp_path, "../outside.txt")
