import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import tracord.git_capture as git_capture
from tracord.bundle import import_bundle
from tracord.git_capture import GitDiffCapture
from tracord.recorder import record_command
from tracord.storage import run_dir


def test_redacted_patch_preserves_lf_and_non_utf8_bytes(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / "latin.txt").write_bytes(b"before\xff\n")
    _git(repo, "add", "latin.txt")
    _git(repo, "commit", "-m", "Add latin fixture")
    monkeypatch.chdir(repo)

    trace = record_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('latin.txt').write_bytes(b'after\\xff\\n')",
        ],
        root=repo / ".tracord",
        capture_diff=True,
    )

    patch = (run_dir(repo / ".tracord", str(trace["run_id"])) / "changes.patch").read_bytes()
    assert b"\r\n" not in patch
    assert b"after\xff" in patch


def test_capture_ignores_inherited_git_work_tree(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path, "repo")
    other = _init_repo(tmp_path, "other")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

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
    assert trace["file_changes"]["files"] == [{"status": "M", "path": "tracked.txt"}]


def test_literal_store_path_is_excluded(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    trace = record_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ],
        root=repo / "[trace]",
        capture_diff=True,
    )

    paths = {change["path"] for change in trace["file_changes"]["files"]}
    assert paths == {"tracked.txt"}


def test_summary_output_is_bounded(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(git_capture, "_MAX_SUMMARY_BYTES", 1)

    trace = record_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ],
        root=repo / ".tracord",
        capture_diff=True,
    )

    assert trace["file_changes"]["status"] == "omitted"
    assert trace["file_changes"]["reason"] == "summary_size_limit"
    assert "file_diff" not in trace["artifacts"]


def test_cleanup_error_is_non_fatal(tmp_path: Path):
    class FailingTemporary:
        def cleanup(self):
            raise PermissionError("busy")

    capture = GitDiffCapture(
        cwd=tmp_path,
        store=tmp_path / ".tracord",
        max_diff_bytes=1024,
        redact=True,
    )
    capture._temporary = FailingTemporary()  # type: ignore[assignment]
    capture._git_env = {}

    capture.close()

    assert capture._temporary is None
    assert capture._git_env is None


def test_unborn_repository_and_ignored_files(tmp_path: Path, monkeypatch):
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    trace = record_command(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "Path('kept.txt').write_text('kept'); "
                "Path('ignored.txt').write_text('ignored')"
            ),
        ],
        root=repo / ".tracord",
        capture_diff=True,
    )

    paths = {change["path"] for change in trace["file_changes"]["files"]}
    assert paths == {"kept.txt"}


def test_store_outside_repository_and_custom_timeout(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    trace = record_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ],
        root=tmp_path / "outside-store",
        capture_diff=True,
        git_timeout_seconds=5,
    )

    assert trace["file_changes"]["status"] == "captured"
    assert trace["file_changes"]["git_timeout_seconds"] == 5


def test_git_unavailable_is_reported_without_blocking_command(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    trace = record_command(
        [sys.executable, "-c", "print('ran')"],
        root=tmp_path / ".tracord",
        capture_diff=True,
    )

    assert trace["status"] == "passed"
    assert trace["file_changes"] == {"status": "skipped", "reason": "git_unavailable"}


def test_bundle_overwrite_removes_stale_optional_patch(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    source_store = repo / ".tracord"
    trace = record_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ],
        root=source_store,
        capture_diff=True,
    )
    source_dir = run_dir(source_store, str(trace["run_id"]))
    plain_trace = dict(trace)
    plain_trace.pop("file_changes")
    plain_trace["artifacts"] = {"stdout": "stdout.log", "stderr": "stderr.log"}
    plain_trace["events"] = [
        event for event in trace["events"] if event["type"] != "file.diff"
    ]
    bundle = tmp_path / "plain.tracord.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", json.dumps(plain_trace))
        archive.write(source_dir / "stdout.log", "stdout.log")
        archive.write(source_dir / "stderr.log", "stderr.log")

    target_store = tmp_path / "target"
    target_dir = run_dir(target_store, str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    (target_dir / "stdout.log").write_text("", encoding="utf-8")
    (target_dir / "stderr.log").write_text("", encoding="utf-8")
    (target_dir / "changes.patch").write_text("stale", encoding="utf-8")

    import_bundle(root=target_store, bundle_path=bundle, overwrite=True)

    assert not (target_dir / "changes.patch").exists()


def test_bundle_rejects_run_id_that_escapes_store(tmp_path: Path):
    trace = {
        "schema_version": "tracord.trace.v0",
        "run_id": "../escape",
        "kind": "command",
        "status": "passed",
        "command": ["python", "--version"],
        "cwd": "/repo",
        "started_at": "2026-08-05T00:00:00.000Z",
        "finished_at": "2026-08-05T00:00:00.100Z",
        "duration_ms": 100,
        "exit_code": 0,
        "timed_out": False,
        "redacted": True,
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
        "events": [],
    }
    bundle = tmp_path / "escape.tracord.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", json.dumps(trace))
        archive.writestr("stdout.log", "")
        archive.writestr("stderr.log", "")

    with pytest.raises(ValueError, match="invalid run id"):
        import_bundle(root=tmp_path / "store", bundle_path=bundle)


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
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
