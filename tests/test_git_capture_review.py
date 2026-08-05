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
            (
                "from pathlib import Path; "
                "Path('tracked.txt').write_text('changed'); "
                "Path('[trace]/assertions.json').write_text('{}')"
            ),
        ],
        root=repo / "[trace]",
        capture_diff=True,
    )

    paths = {change["path"] for change in trace["file_changes"]["files"]}
    assert paths == {"[trace]/assertions.json", "tracked.txt"}
    assert not any(path.startswith("[trace]/runs/") for path in paths)


def test_repository_assertions_are_captured_but_runtime_stores_are_not(
    tmp_path: Path, monkeypatch
):
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text(
        "**/.tracord/*\n!**/.tracord/assertions.json\n",
        encoding="utf-8",
    )
    root_assertions = repo / ".tracord" / "assertions.json"
    root_assertions.parent.mkdir()
    root_assertions.write_text('{"before": true}\n', encoding="utf-8")
    active_runtime = repo / ".tracord" / "runs" / "fixture.txt"
    active_runtime.parent.mkdir()
    active_runtime.write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", ".tracord/assertions.json")
    _git(repo, "add", "--force", ".tracord/runs/fixture.txt")
    _git(repo, "commit", "-m", "Add assertion policy")
    monkeypatch.chdir(repo)

    trace = record_command(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "Path('.tracord/assertions.json').write_text('{\"after\": true}'); "
                "Path('.tracord/runs/fixture.txt').write_text('after'); "
                "nested = Path('nested/.tracord'); nested.mkdir(parents=True); "
                "(nested / 'assertions.json').write_text('{}'); "
                "runtime = nested / 'runs/foreign/trace.json'; "
                "runtime.parent.mkdir(parents=True); runtime.write_text('{}')"
            ),
        ],
        root=repo / ".tracord",
        capture_diff=True,
    )

    assert trace["file_changes"]["status"] == "captured", trace["file_changes"]
    assert trace["file_changes"]["files"] == [
        {"status": "M", "path": ".tracord/assertions.json"},
        {"status": "A", "path": "nested/.tracord/assertions.json"},
    ]


def test_capture_preserves_configured_global_ignores(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    global_ignore = tmp_path / "global-ignore"
    global_ignore.write_text("*.pem\n", encoding="utf-8")
    _git(repo, "config", "core.excludesFile", str(global_ignore))
    secret = repo / "private.pem"
    secret.write_text("before-secret\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    trace = record_command(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "Path('tracked.txt').write_text('changed'); "
                "Path('private.pem').write_text('after-secret')"
            ),
        ],
        root=repo / ".tracord",
        capture_diff=True,
    )

    assert trace["file_changes"]["files"] == [
        {"status": "M", "path": "tracked.txt"}
    ]
    patch = (
        run_dir(repo / ".tracord", str(trace["run_id"])) / "changes.patch"
    ).read_text(encoding="utf-8")
    assert "private.pem" not in patch
    assert "after-secret" not in patch


def test_capture_excludes_only_the_active_store_runtime(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    foreign_runtime = repo / "nested" / ".tracord" / "runs" / "fixture.txt"
    foreign_runtime.parent.mkdir(parents=True)
    foreign_runtime.write_text("before\n", encoding="utf-8")
    _git(repo, "add", "nested/.tracord/runs/fixture.txt")
    _git(repo, "commit", "-m", "Track nested runtime fixture")
    monkeypatch.chdir(repo)

    trace = record_command(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "Path('nested/.tracord/runs/fixture.txt').write_text('after'); "
                "Path('.tracord/assertions.json').write_text('{}')"
            ),
        ],
        root=repo / ".tracord",
        capture_diff=True,
    )

    assert trace["file_changes"]["files"] == [
        {"status": "A", "path": ".tracord/assertions.json"},
        {"status": "M", "path": "nested/.tracord/runs/fixture.txt"},
    ]


def test_repository_gitignore_owns_assertions_at_root_and_nested_depths(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    repository_gitignore = (
        Path(__file__).resolve().parents[1] / ".gitignore"
    ).read_text(encoding="utf-8")
    assert "\n**/.tracord/*\n!**/.tracord/assertions.json\n" in repository_gitignore
    (repo / ".gitignore").write_text(repository_gitignore, encoding="utf-8")
    candidates = {
        ".tracord/assertions.json": False,
        ".tracord/runs/example/trace.json": True,
        "packages/api/.tracord/assertions.json": False,
        "packages/api/.tracord/runs/example/trace.json": True,
    }
    for relative_path in candidates:
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    for relative_path, expected in candidates.items():
        assert _git_check_ignored(repo, relative_path) is expected


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


def _git_check_ignored(repo: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "--quiet", "--", path],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode in {0, 1}
    return result.returncode == 0
