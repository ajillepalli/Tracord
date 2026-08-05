import subprocess
import sys
from pathlib import Path

from tracord.recorder import record_command


def test_capture_supports_linked_git_worktree(tmp_path: Path, monkeypatch):
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init")
    _git(primary, "config", "user.name", "Tracord Tests")
    _git(primary, "config", "user.email", "tracord@example.invalid")
    (primary / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(primary, "add", "tracked.txt")
    _git(primary, "commit", "-m", "Initial commit")
    worktree = tmp_path / "linked"
    _git(primary, "worktree", "add", "-b", "capture-worktree", str(worktree))
    monkeypatch.chdir(worktree)

    trace = record_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ],
        root=worktree / ".tracord",
        capture_diff=True,
    )

    assert trace["file_changes"]["status"] == "captured"
    assert trace["file_changes"]["files"] == [{"status": "M", "path": "tracked.txt"}]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
