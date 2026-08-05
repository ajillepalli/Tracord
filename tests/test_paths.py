import stat
from pathlib import Path

import pytest

from tracord.paths import is_link_or_junction, safe_join, validate_relative_path


def test_validate_relative_path_accepts_nested_forward_slash_path():
    assert validate_relative_path("artifacts/stdout.log") == []


def test_validate_relative_path_rejects_parent_segments():
    assert validate_relative_path("../outside.txt")


@pytest.mark.parametrize("value", [".", "double//segment", "nested/./file"])
def test_validate_relative_path_rejects_raw_empty_and_current_segments(value):
    assert validate_relative_path(value)


def test_validate_relative_path_rejects_windows_drive():
    assert validate_relative_path("C:/outside.txt")


def test_safe_join_rejects_escaping_path(tmp_path: Path):
    with pytest.raises(ValueError):
        safe_join(tmp_path, "../outside.txt")


def test_windows_mount_point_reparse_tag_is_treated_as_junction(tmp_path: Path):
    class ReparseInfo:
        st_mode = stat.S_IFDIR
        st_reparse_tag = 0xA0000003

    assert is_link_or_junction(tmp_path, ReparseInfo()) is True


def test_non_link_cloud_reparse_tag_is_allowed(tmp_path: Path):
    class CloudInfo:
        st_mode = stat.S_IFREG
        st_reparse_tag = 0x9000001A

    assert is_link_or_junction(tmp_path, CloudInfo()) is False
