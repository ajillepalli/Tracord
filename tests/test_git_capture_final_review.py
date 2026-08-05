import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tracord.cli import positive_float
from tracord.git_capture import GitDiffCapture
from tracord.recorder import record_command


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e400"])
def test_git_timeout_rejects_non_finite_cli_values(value: str):
    with pytest.raises(argparse.ArgumentTypeError, match="finite number greater than zero"):
        positive_float(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_git_timeout_rejects_non_finite_library_values(tmp_path: Path, value: float):
    with pytest.raises(ValueError, match="git_timeout_seconds"):
        GitDiffCapture(
            cwd=tmp_path,
            store=tmp_path / ".tracord",
            max_diff_bytes=1024,
            redact=True,
            git_timeout_seconds=value,
        )


def test_inherited_alternate_object_directory_is_preserved(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    alternate = tmp_path / "alternate-objects"
    alternate.mkdir()
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(alternate))

    capture = GitDiffCapture(
        cwd=repo,
        store=repo / ".tracord",
        max_diff_bytes=1024,
        redact=True,
    )
    capture.start()
    try:
        assert capture._initial_result is None
        assert capture._git_env is not None
        alternates = capture._git_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"].split(os.pathsep)
        assert str(alternate) in alternates
        assert str(repo / ".git" / "objects") in alternates
    finally:
        capture.close()


def test_literal_pathspec_environment_does_not_break_capture(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")

    trace = record_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ],
        root=repo / ".tracord",
        capture_diff=True,
    )

    assert trace["file_changes"]["status"] == "captured"


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Tracord Tests")
    _git(repo, "config", "user.email", "tracord@example.invalid")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
