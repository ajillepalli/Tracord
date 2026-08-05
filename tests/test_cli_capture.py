import argparse
import os
import site
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from tracord.cli import build_parser, console_main, positive_float, positive_int
from tracord.result_codes import JSON_OUTPUT_FAILURE_EXIT_CODE


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


class BrokenFlush:
    def flush(self) -> None:
        raise BrokenPipeError

    def fileno(self) -> int:
        raise OSError


def test_console_main_maps_broken_pipe_to_transport_exit(monkeypatch):
    monkeypatch.setattr("tracord.cli.main", lambda _argv: 0)
    monkeypatch.setattr(sys, "stdout", BrokenFlush())
    monkeypatch.setattr(sys, "stderr", BrokenFlush())

    assert console_main([]) == JSON_OUTPUT_FAILURE_EXIT_CODE


def test_console_main_preserves_normal_and_argparse_exit_codes(monkeypatch):
    monkeypatch.setattr("tracord.cli.main", lambda _argv: 7)
    assert console_main([]) == 7

    def parser_exit(_argv):
        raise SystemExit(2)

    monkeypatch.setattr("tracord.cli.main", parser_exit)
    assert console_main([]) == 2


def test_console_main_preserves_system_exit_message(monkeypatch, capsys):
    def message_exit(_argv):
        raise SystemExit("usage failed")

    monkeypatch.setattr("tracord.cli.main", message_exit)

    assert console_main([]) == 1
    assert capsys.readouterr().err == "usage failed\n"


def test_console_main_does_not_swallow_command_oserror(monkeypatch):
    def command_failure(_argv):
        raise PermissionError("store denied")

    monkeypatch.setattr("tracord.cli.main", command_failure)

    with pytest.raises(PermissionError, match="store denied"):
        console_main([])


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_process_entrypoints_suppress_broken_pipe_diagnostics(
    entrypoint: str, tmp_path: Path
):
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    console_path = Path(sysconfig.get_path("scripts")) / (
        "tracord.exe" if os.name == "nt" else "tracord"
    )
    if not console_path.exists():
        console_path = (
            Path(site.getuserbase())
            / f"Python{sys.version_info.major}{sys.version_info.minor}"
            / "Scripts"
            / ("tracord.exe" if os.name == "nt" else "tracord")
        )
    command = (
        [sys.executable, "-m", "tracord", "--version"]
        if entrypoint == "module"
        else [str(console_path), "--version"]
    )
    try:
        completed = subprocess.run(
            command,
            stdout=write_fd,
            stderr=subprocess.PIPE,
            cwd=tmp_path,
            check=False,
        )
    finally:
        os.close(write_fd)

    assert completed.returncode == JSON_OUTPUT_FAILURE_EXIT_CODE
    assert completed.stderr == b""
