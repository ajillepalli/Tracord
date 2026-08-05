import argparse

import pytest

from tracord.cli import build_parser, positive_float, positive_int


@pytest.mark.parametrize(
    ("parser", "value"),
    [(positive_int, "0"), (positive_int, "-1"), (positive_float, "0")],
)
def test_capture_numeric_options_require_positive_values(parser, value):
    with pytest.raises(argparse.ArgumentTypeError):
        parser(value)


def test_record_parser_accepts_custom_git_timeout():
    args = build_parser().parse_args(
        ["record", "--capture-diff", "--git-timeout", "120", "--", "python"]
    )

    assert args.capture_diff is True
    assert args.git_timeout == 120
