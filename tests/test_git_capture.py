import subprocess
import sys
from pathlib import Path

from tracord.bundle import export_run, import_bundle
from tracord.recorder import record_command
from tracord.replay import replay_run
from tracord.schema import validate_trace
from tracord.storage import run_dir


def test_capture_records_worktree_delta_without_mutating_git_state(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / "rename-me.txt").write_text("rename content\n", encoding="utf-8")
    (repo / "delete-me.txt").write_text("delete content\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"before\x00binary")
    (repo / "preexisting.txt").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Add fixtures")

    (repo / "preexisting.txt").write_text("already staged\n", encoding="utf-8")
    _git(repo, "add", "preexisting.txt")
    index_before = _git(repo, "write-tree").stdout.strip()
    objects_before = _object_files(repo)
    monkeypatch.chdir(repo)

    script = (
        "from pathlib import Path; "
        "Path('tracked.txt').write_text('token=secretvalue\\n', encoding='utf-8'); "
        "Path('new.txt').write_text('new\\n', encoding='utf-8'); "
        "Path('rename-me.txt').rename('renamed.txt'); "
        "Path('delete-me.txt').unlink(); "
        "Path('binary.bin').write_bytes(b'after\\x00binary')"
    )
    trace = record_command(
        [sys.executable, "-c", script],
        root=repo / ".tracord",
        capture_diff=True,
    )

    file_changes = trace["file_changes"]
    assert file_changes["status"] == "captured"
    assert file_changes["changed_files"] == 5
    paths = {change["path"] for change in file_changes["files"]}
    assert paths == {"binary.bin", "delete-me.txt", "new.txt", "renamed.txt", "tracked.txt"}
    assert "preexisting.txt" not in paths
    assert not any(path.startswith(".tracord/") for path in paths)
    assert any(change["status"].startswith("R") for change in file_changes["files"])
    assert validate_trace(trace) == []
    assert [event["type"] for event in trace["events"]] == [
        "command.started",
        "command.finished",
        "file.diff",
    ]

    trace_directory = run_dir(repo / ".tracord", str(trace["run_id"]))
    patch = (trace_directory / "changes.patch").read_text(encoding="utf-8")
    assert "token=[REDACTED]" in patch
    assert "Binary files" in patch
    assert trace["artifacts"]["file_diff"] == "changes.patch"
    assert _git(repo, "write-tree").stdout.strip() == index_before
    assert _object_files(repo) == objects_before


def test_capture_skips_non_git_directory_without_failing_command(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    trace = record_command(
        [sys.executable, "-c", "print('still ran')"],
        root=tmp_path / ".tracord",
        capture_diff=True,
    )

    assert trace["status"] == "passed"
    assert trace["file_changes"] == {
        "status": "skipped",
        "reason": "not_git_repository",
    }
    assert "file_diff" not in trace["artifacts"]


def test_capture_omits_patch_over_size_limit(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    script = "from pathlib import Path; Path('tracked.txt').write_text('x' * 4096)"

    trace = record_command(
        [sys.executable, "-c", script],
        root=repo / ".tracord",
        capture_diff=True,
        max_diff_bytes=128,
    )

    assert trace["file_changes"]["status"] == "omitted"
    assert trace["file_changes"]["reason"] == "size_limit"
    assert "file_diff" not in trace["artifacts"]
    assert not (run_dir(repo / ".tracord", str(trace["run_id"])) / "changes.patch").exists()


def test_no_redact_capture_includes_binary_patch(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / "binary.bin").write_bytes(b"before\x00" * 100)
    _git(repo, "add", "binary.bin")
    _git(repo, "commit", "-m", "Add binary")
    monkeypatch.chdir(repo)
    script = "from pathlib import Path; Path('binary.bin').write_bytes(b'after\\x00' * 100)"

    trace = record_command(
        [sys.executable, "-c", script],
        root=repo / ".tracord",
        capture_diff=True,
        redact=False,
    )

    patch_path = run_dir(repo / ".tracord", str(trace["run_id"])) / "changes.patch"
    assert trace["file_changes"]["binary_content"] == "included"
    assert b"GIT binary patch" in patch_path.read_bytes()


def test_bundle_round_trips_captured_patch(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    trace = record_command(
        [sys.executable, "-c", "from pathlib import Path; Path('tracked.txt').write_text('changed')"],
        root=repo / ".tracord",
        capture_diff=True,
    )
    bundle = export_run(
        root=repo / ".tracord",
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "capture.tracord.zip",
    )

    imported = import_bundle(root=tmp_path / "imported", bundle_path=bundle)

    imported_patch = run_dir(tmp_path / "imported", str(imported["run_id"])) / "changes.patch"
    assert imported_patch.exists()
    assert imported_patch.read_bytes() == (
        run_dir(repo / ".tracord", str(trace["run_id"])) / "changes.patch"
    ).read_bytes()


def test_replay_requires_explicit_diff_capture(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    store = repo / ".tracord"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('tracked.txt').write_text('recorded')",
    ]
    original = record_command(command, root=store, capture_diff=True)

    replay_without_capture = replay_run(root=store, run_id=str(original["run_id"]))
    assert "file_changes" not in replay_without_capture

    (repo / "tracked.txt").write_text("reset", encoding="utf-8")
    replay_with_capture = replay_run(
        root=store,
        run_id=str(original["run_id"]),
        capture_diff=True,
    )
    assert replay_with_capture["file_changes"]["status"] == "captured"


def test_capture_configuration_error_does_not_change_command_status(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    trace = record_command(
        [sys.executable, "-c", "print('ran')"],
        root=repo,
        capture_diff=True,
    )

    assert trace["status"] == "passed"
    assert trace["file_changes"]["status"] == "error"
    assert trace["file_changes"]["reason"] == "store_contains_repository"


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


def _object_files(repo: Path) -> set[str]:
    objects = repo / ".git" / "objects"
    return {
        path.relative_to(objects).as_posix()
        for path in objects.rglob("*")
        if path.is_file()
    }
