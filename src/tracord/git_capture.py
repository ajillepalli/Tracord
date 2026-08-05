"""Isolated Git working-tree capture for command traces."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from .redaction import redact_text


FILE_DIFF_ARTIFACT = "changes.patch"
DEFAULT_MAX_DIFF_BYTES = 10 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 30


class GitDiffCapture:
    """Capture before/after Git trees without mutating repository state."""

    def __init__(
        self,
        *,
        cwd: Path,
        store: Path,
        max_diff_bytes: int,
        redact: bool,
    ) -> None:
        if max_diff_bytes <= 0:
            raise ValueError("max_diff_bytes must be greater than zero")

        self.cwd = cwd.resolve()
        self.store = store.resolve()
        self.max_diff_bytes = max_diff_bytes
        self.redact = redact
        self.repo_root: Path | None = None
        self.before_tree: str | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._git_env: dict[str, str] | None = None
        self._exclude_path: str | None = None
        self._initial_result: dict[str, Any] | None = None

    def start(self) -> None:
        try:
            discovered = _git(self.cwd, ["rev-parse", "--show-toplevel"])
        except FileNotFoundError:
            self._initial_result = _result("skipped", reason="git_unavailable")
            return
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._initial_result = _error_result("repository_discovery_failed", exc, self.redact)
            return

        if discovered.returncode != 0:
            self._initial_result = _result("skipped", reason="not_git_repository")
            return

        repo_text = discovered.stdout.decode("utf-8", errors="replace").strip()
        if not repo_text:
            self._initial_result = _result("error", reason="repository_discovery_failed")
            return
        self.repo_root = Path(repo_text).resolve()

        try:
            common_dir_result = _git(self.repo_root, ["rev-parse", "--git-common-dir"])
            if common_dir_result.returncode != 0:
                self._initial_result = _git_error("git_common_dir_failed", common_dir_result, self.redact)
                return
            common_dir_text = common_dir_result.stdout.decode("utf-8", errors="replace").strip()
            common_dir = Path(common_dir_text)
            if not common_dir.is_absolute():
                common_dir = self.repo_root / common_dir
            real_objects = common_dir.resolve() / "objects"

            self._temporary = tempfile.TemporaryDirectory(prefix="tracord-git-")
            temporary_root = Path(self._temporary.name)
            temporary_objects = temporary_root / "objects"
            temporary_objects.mkdir()
            self._git_env = os.environ.copy()
            self._git_env.update(
                {
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(real_objects),
                    "GIT_OBJECT_DIRECTORY": str(temporary_objects),
                    "GIT_OPTIONAL_LOCKS": "0",
                }
            )
            self._exclude_path = _relative_store_path(self.repo_root, self.store)
            if self._exclude_path == ".":
                self._initial_result = _result("error", reason="store_contains_repository")
                self.close()
                return
            self.before_tree = self._snapshot("before")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self._initial_result = _error_result("before_snapshot_failed", exc, self.redact)
            self.close()

    def finish(self, output_dir: Path) -> dict[str, Any]:
        if self._initial_result is not None:
            return self._initial_result
        if self.repo_root is None or self.before_tree is None or self._git_env is None:
            return _result("error", reason="capture_not_started")

        try:
            after_tree = self._snapshot("after")
            summary_result = _git(
                self.repo_root,
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--find-renames",
                    "--name-status",
                    "-z",
                    self.before_tree,
                    after_tree,
                    "--",
                ],
                env=self._git_env,
            )
            if summary_result.returncode != 0:
                return _git_error("diff_summary_failed", summary_result, self.redact)

            files = _parse_name_status(summary_result.stdout)
            base = {
                "changed_files": len(files),
                "files": files,
                "max_diff_bytes": self.max_diff_bytes,
                "repository_relative_cwd": _relative_cwd(self.repo_root, self.cwd),
            }
            if not files:
                return _result("unchanged", **base)

            artifact_path = output_dir / FILE_DIFF_ARTIFACT
            patch_result = self._write_patch(self.before_tree, after_tree, artifact_path)
            return {**base, **patch_result}
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return _error_result("after_snapshot_failed", exc, self.redact)
        finally:
            self.close()

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._git_env = None

    def _snapshot(self, label: str) -> str:
        if self.repo_root is None or self._temporary is None or self._git_env is None:
            raise ValueError("capture is not initialized")

        index_path = Path(self._temporary.name) / f"index-{label}"
        env = {**self._git_env, "GIT_INDEX_FILE": str(index_path)}
        head = _git(self.repo_root, ["rev-parse", "--verify", "HEAD^{tree}"], env=env)
        read_tree_args = ["read-tree", "HEAD"] if head.returncode == 0 else ["read-tree", "--empty"]
        read_tree = _git(self.repo_root, read_tree_args, env=env)
        if read_tree.returncode != 0:
            raise ValueError(_git_message(read_tree, self.redact) or "git read-tree failed")

        pathspecs = ["."]
        if self._exclude_path is not None:
            pathspecs.extend(
                [
                    f":(top,exclude){self._exclude_path}",
                    f":(top,exclude){self._exclude_path}/**",
                ]
            )
        add = _git(self.repo_root, ["add", "-A", "--", *pathspecs], env=env)
        if add.returncode != 0:
            raise ValueError(_git_message(add, self.redact) or "git add failed")

        write_tree = _git(self.repo_root, ["write-tree"], env=env)
        if write_tree.returncode != 0:
            raise ValueError(_git_message(write_tree, self.redact) or "git write-tree failed")
        tree = write_tree.stdout.decode("ascii", errors="replace").strip()
        if not tree:
            raise ValueError("git write-tree returned no tree")
        return tree

    def _write_patch(self, before_tree: str, after_tree: str, artifact_path: Path) -> dict[str, Any]:
        if self.repo_root is None or self._git_env is None:
            return _result("error", reason="capture_not_started")

        args = [
            "git",
            "--no-optional-locks",
            "-C",
            str(self.repo_root),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
        ]
        if not self.redact:
            args.append("--binary")
        args.extend([before_tree, after_tree, "--"])

        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        with tempfile.TemporaryFile() as error_file:
            process = subprocess.Popen(
                args,
                cwd=self.repo_root,
                env=self._git_env,
                stdout=subprocess.PIPE,
                stderr=error_file,
            )
            timed_out = threading.Event()

            def stop_on_timeout() -> None:
                timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timer = threading.Timer(_GIT_TIMEOUT_SECONDS, stop_on_timeout)
            timer.daemon = True
            timer.start()
            assert process.stdout is not None
            try:
                with artifact_path.open("wb") as patch_file:
                    while chunk := process.stdout.read(64 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > self.max_diff_bytes:
                            try:
                                process.kill()
                            except OSError:
                                pass
                            break
                        patch_file.write(chunk)
                return_code = process.wait()
            finally:
                timer.cancel()
            error_file.seek(0)
            error_output = error_file.read()

        if timed_out.is_set():
            artifact_path.unlink(missing_ok=True)
            return _result("error", reason="diff_generation_timeout")
        if total_bytes > self.max_diff_bytes:
            artifact_path.unlink(missing_ok=True)
            return _result(
                "omitted",
                reason="size_limit",
                observed_bytes_at_least=total_bytes,
            )
        if return_code != 0:
            artifact_path.unlink(missing_ok=True)
            detail = _safe_detail(error_output, self.redact)
            return _result("error", reason="diff_generation_failed", detail=detail)

        if self.redact:
            patch_text = artifact_path.read_text(encoding="utf-8", errors="replace")
            artifact_path.write_text(redact_text(patch_text), encoding="utf-8")
            if artifact_path.stat().st_size > self.max_diff_bytes:
                artifact_path.unlink(missing_ok=True)
                return _result("omitted", reason="size_limit_after_redaction")

        return _result(
            "captured",
            artifact=FILE_DIFF_ARTIFACT,
            bytes=artifact_path.stat().st_size,
            binary_content="omitted" if self.redact else "included",
            redacted=self.redact,
        )


def _git(
    cwd: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
        env=env,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _relative_store_path(repo_root: Path, store: Path) -> str | None:
    try:
        return store.relative_to(repo_root).as_posix() or "."
    except ValueError:
        return None


def _relative_cwd(repo_root: Path, cwd: Path) -> str:
    try:
        relative = cwd.relative_to(repo_root).as_posix()
    except ValueError:
        return "."
    return relative or "."


def _parse_name_status(output: bytes) -> list[dict[str, str]]:
    fields = output.decode("utf-8", errors="replace").split("\0")
    if fields and fields[-1] == "":
        fields.pop()

    changes: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status or index >= len(fields):
            raise ValueError("git diff returned malformed name-status output")
        if status[0] in {"R", "C"}:
            if index + 1 >= len(fields):
                raise ValueError("git diff returned malformed rename output")
            old_path = fields[index]
            path = fields[index + 1]
            index += 2
            changes.append({"status": status, "old_path": old_path, "path": path})
        else:
            path = fields[index]
            index += 1
            changes.append({"status": status, "path": path})
    return changes


def _result(status: str, **values: Any) -> dict[str, Any]:
    return {"status": status, **{key: value for key, value in values.items() if value != ""}}


def _git_error(reason: str, result: subprocess.CompletedProcess[bytes], redact: bool) -> dict[str, Any]:
    return _result("error", reason=reason, detail=_git_message(result, redact))


def _error_result(reason: str, exc: BaseException, redact: bool) -> dict[str, Any]:
    return _result("error", reason=reason, detail=_safe_detail(str(exc).encode(), redact))


def _git_message(result: subprocess.CompletedProcess[bytes], redact: bool) -> str:
    return _safe_detail(result.stderr or result.stdout, redact)


def _safe_detail(value: bytes, redact: bool) -> str:
    detail = value.decode("utf-8", errors="replace").strip()[:500]
    return redact_text(detail) if redact else detail
