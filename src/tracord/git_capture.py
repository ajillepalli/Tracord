"""Isolated Git working-tree capture for command traces."""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .redaction import redact_text


FILE_DIFF_ARTIFACT = "changes.patch"
DEFAULT_MAX_DIFF_BYTES = 10 * 1024 * 1024
DEFAULT_GIT_TIMEOUT_SECONDS = 30.0
_MAX_SUMMARY_BYTES = 4 * 1024 * 1024
_GIT_CONTEXT_VARIABLES = {
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_GLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_INDEX_FILE",
    "GIT_LITERAL_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_WORK_TREE",
}


@dataclass(frozen=True)
class _LimitedGitResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    exceeded_limit: bool
    observed_bytes: int


class GitDiffCapture:
    """Capture before/after Git trees without mutating repository state."""

    def __init__(
        self,
        *,
        cwd: Path,
        store: Path,
        max_diff_bytes: int,
        redact: bool,
        git_timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        if max_diff_bytes <= 0:
            raise ValueError("max_diff_bytes must be greater than zero")
        if not math.isfinite(git_timeout_seconds) or git_timeout_seconds <= 0:
            raise ValueError("git_timeout_seconds must be greater than zero")

        self.cwd = cwd.resolve()
        self.store = store.resolve()
        self.max_diff_bytes = max_diff_bytes
        self.git_timeout_seconds = git_timeout_seconds
        self.redact = redact
        self.repo_root: Path | None = None
        self.before_tree: str | None = None
        self._base_git_env = _sanitized_git_env()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._git_env: dict[str, str] | None = None
        self._exclude_path: str | None = None
        self._initial_result: dict[str, Any] | None = None

    def start(self) -> None:
        try:
            discovered = _git(
                self.cwd,
                ["rev-parse", "--show-toplevel"],
                env=self._base_git_env,
                timeout_seconds=self.git_timeout_seconds,
            )
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
            common_dir_result = _git(
                self.repo_root,
                ["rev-parse", "--git-common-dir"],
                env=self._base_git_env,
                timeout_seconds=self.git_timeout_seconds,
            )
            if common_dir_result.returncode != 0:
                self._initial_result = _git_error(
                    "git_common_dir_failed", common_dir_result, self.redact
                )
                return
            common_dir_text = common_dir_result.stdout.decode(
                "utf-8", errors="replace"
            ).strip()
            common_dir = Path(common_dir_text)
            if not common_dir.is_absolute():
                common_dir = self.repo_root / common_dir
            real_objects = common_dir.resolve() / "objects"

            self._temporary = tempfile.TemporaryDirectory(
                prefix="tracord-git-",
                ignore_cleanup_errors=True,
            )
            temporary_root = Path(self._temporary.name)
            temporary_objects = temporary_root / "objects"
            temporary_objects.mkdir()

            alternates = [str(real_objects)]
            inherited_alternates = self._base_git_env.get(
                "GIT_ALTERNATE_OBJECT_DIRECTORIES"
            )
            if inherited_alternates:
                alternates.append(inherited_alternates)
            self._git_env = self._base_git_env.copy()
            self._git_env.update(
                {
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.pathsep.join(alternates),
                    "GIT_OBJECT_DIRECTORY": str(temporary_objects),
                    "GIT_OPTIONAL_LOCKS": "0",
                }
            )
            self._exclude_path = _relative_store_path(self.repo_root, self.store)
            if self._exclude_path == ".":
                self._initial_result = _result(
                    "error", reason="store_contains_repository"
                )
                self.close()
                return
            self.before_tree = self._snapshot("before")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self._initial_result = _error_result(
                "before_snapshot_failed", exc, self.redact
            )
            self.close()

    def finish(self, output_dir: Path) -> dict[str, Any]:
        if self._initial_result is not None:
            return self._initial_result
        if self.repo_root is None or self.before_tree is None or self._git_env is None:
            return _result("error", reason="capture_not_started")

        try:
            after_tree = self._snapshot("after")
            summary_result = _git_limited(
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
                timeout_seconds=self.git_timeout_seconds,
                max_bytes=_MAX_SUMMARY_BYTES,
            )
            if summary_result.timed_out:
                return _result("error", reason="diff_summary_timeout")
            if summary_result.exceeded_limit:
                return _result(
                    "omitted",
                    reason="summary_size_limit",
                    max_summary_bytes=_MAX_SUMMARY_BYTES,
                    observed_bytes_at_least=summary_result.observed_bytes,
                    max_diff_bytes=self.max_diff_bytes,
                    repository_relative_cwd=_relative_cwd(
                        self.repo_root, self.cwd
                    ),
                )
            if summary_result.returncode != 0:
                return _git_error(
                    "diff_summary_failed", summary_result, self.redact
                )

            files = _parse_name_status(summary_result.stdout)
            base = {
                "changed_files": len(files),
                "files": files,
                "max_diff_bytes": self.max_diff_bytes,
                "git_timeout_seconds": self.git_timeout_seconds,
                "repository_relative_cwd": _relative_cwd(
                    self.repo_root, self.cwd
                ),
            }
            if not files:
                return _result("unchanged", **base)

            artifact_path = output_dir / FILE_DIFF_ARTIFACT
            patch_result = self._write_patch(
                self.before_tree, after_tree, artifact_path
            )
            return {**base, **patch_result}
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return _error_result("after_snapshot_failed", exc, self.redact)
        finally:
            self.close()

    def close(self) -> None:
        if self._temporary is not None:
            temporary = self._temporary
            self._temporary = None
            try:
                temporary.cleanup()
            except OSError:
                pass
        self._git_env = None

    def _snapshot(self, label: str) -> str:
        if self.repo_root is None or self._temporary is None or self._git_env is None:
            raise ValueError("capture is not initialized")

        index_path = Path(self._temporary.name) / f"index-{label}"
        env = {**self._git_env, "GIT_INDEX_FILE": str(index_path)}
        head = _git(
            self.repo_root,
            ["rev-parse", "--verify", "HEAD^{tree}"],
            env=env,
            timeout_seconds=self.git_timeout_seconds,
        )
        read_tree_args = (
            ["read-tree", "HEAD"]
            if head.returncode == 0
            else ["read-tree", "--empty"]
        )
        read_tree = _git(
            self.repo_root,
            read_tree_args,
            env=env,
            timeout_seconds=self.git_timeout_seconds,
        )
        if read_tree.returncode != 0:
            raise ValueError(
                _git_message(read_tree, self.redact) or "git read-tree failed"
            )

        pathspecs = ["."]
        if self._exclude_path is not None:
            pathspecs.append(f":(top,literal,exclude){self._exclude_path}")
        add = _git(
            self.repo_root,
            ["add", "-A", "--", *pathspecs],
            env=env,
            timeout_seconds=self.git_timeout_seconds,
        )
        if add.returncode != 0:
            raise ValueError(_git_message(add, self.redact) or "git add failed")

        write_tree = _git(
            self.repo_root,
            ["write-tree"],
            env=env,
            timeout_seconds=self.git_timeout_seconds,
        )
        if write_tree.returncode != 0:
            raise ValueError(
                _git_message(write_tree, self.redact) or "git write-tree failed"
            )
        tree = write_tree.stdout.decode("ascii", errors="replace").strip()
        if not tree:
            raise ValueError("git write-tree returned no tree")
        return tree

    def _write_patch(
        self,
        before_tree: str,
        after_tree: str,
        artifact_path: Path,
    ) -> dict[str, Any]:
        if self.repo_root is None or self._git_env is None:
            return _result("error", reason="capture_not_started")

        args = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
        ]
        if not self.redact:
            args.append("--binary")
        args.extend([before_tree, after_tree, "--"])

        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        patch_result = _git_limited(
            self.repo_root,
            args,
            env=self._git_env,
            timeout_seconds=self.git_timeout_seconds,
            max_bytes=self.max_diff_bytes,
            output_path=artifact_path,
        )
        if patch_result.timed_out:
            artifact_path.unlink(missing_ok=True)
            return _result("error", reason="diff_generation_timeout")
        if patch_result.exceeded_limit:
            artifact_path.unlink(missing_ok=True)
            return _result(
                "omitted",
                reason="size_limit",
                observed_bytes_at_least=patch_result.observed_bytes,
            )
        if patch_result.returncode != 0:
            artifact_path.unlink(missing_ok=True)
            return _result(
                "error",
                reason="diff_generation_failed",
                detail=_safe_detail(patch_result.stderr, self.redact),
            )

        if self.redact:
            patch_text = artifact_path.read_bytes().decode(
                "utf-8", errors="surrogateescape"
            )
            redacted_patch = redact_text(patch_text).encode(
                "utf-8", errors="surrogateescape"
            )
            artifact_path.write_bytes(redacted_patch)
            if artifact_path.stat().st_size > self.max_diff_bytes:
                artifact_path.unlink(missing_ok=True)
                return _result(
                    "omitted", reason="size_limit_after_redaction"
                )

        return _result(
            "captured",
            artifact=FILE_DIFF_ARTIFACT,
            bytes=artifact_path.stat().st_size,
            binary_content="omitted" if self.redact else "included",
            redacted=self.redact,
        )


def _sanitized_git_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _GIT_CONTEXT_VARIABLES
    }


def _git(
    cwd: Path,
    args: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
        env=env,
        timeout=timeout_seconds,
    )


def _git_limited(
    cwd: Path,
    args: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
    max_bytes: int,
    output_path: Path | None = None,
) -> _LimitedGitResult:
    output = bytearray()
    output_file: BinaryIO | None = None
    observed_bytes = 0
    exceeded_limit = False
    timed_out = threading.Event()

    if output_path is not None:
        output_file = output_path.open("wb")
    try:
        with tempfile.TemporaryFile() as error_file:
            with subprocess.Popen(
                ["git", "--no-optional-locks", "-C", str(cwd), *args],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=error_file,
            ) as process:

                def stop_on_timeout() -> None:
                    if process.poll() is None:
                        timed_out.set()
                        try:
                            process.kill()
                        except OSError:
                            pass

                timer = threading.Timer(timeout_seconds, stop_on_timeout)
                timer.daemon = True
                timer.start()
                if process.stdout is None:
                    raise RuntimeError("git stdout pipe is unavailable")
                try:
                    while chunk := process.stdout.read(64 * 1024):
                        observed_bytes += len(chunk)
                        if observed_bytes > max_bytes:
                            exceeded_limit = True
                            try:
                                process.kill()
                            except OSError:
                                pass
                            break
                        if output_file is None:
                            output.extend(chunk)
                        else:
                            output_file.write(chunk)
                    return_code = process.wait()
                finally:
                    timer.cancel()
            error_file.seek(0)
            error_output = error_file.read()
    finally:
        if output_file is not None:
            output_file.close()

    return _LimitedGitResult(
        returncode=return_code,
        stdout=bytes(output),
        stderr=error_output,
        timed_out=timed_out.is_set() and return_code != 0,
        exceeded_limit=exceeded_limit,
        observed_bytes=observed_bytes,
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
            changes.append(
                {"status": status, "old_path": old_path, "path": path}
            )
        else:
            path = fields[index]
            index += 1
            changes.append({"status": status, "path": path})
    return changes


def _result(status: str, **values: Any) -> dict[str, Any]:
    return {
        "status": status,
        **{key: value for key, value in values.items() if value != ""},
    }


def _git_error(
    reason: str,
    result: subprocess.CompletedProcess[bytes] | _LimitedGitResult,
    redact: bool,
) -> dict[str, Any]:
    return _result("error", reason=reason, detail=_git_message(result, redact))


def _error_result(
    reason: str,
    exc: BaseException,
    redact: bool,
) -> dict[str, Any]:
    return _result(
        "error", reason=reason, detail=_safe_detail(str(exc).encode(), redact)
    )


def _git_message(
    result: subprocess.CompletedProcess[bytes] | _LimitedGitResult,
    redact: bool,
) -> str:
    return _safe_detail(result.stderr or result.stdout, redact)


def _safe_detail(value: bytes, redact: bool) -> str:
    detail = value.decode("utf-8", errors="replace").strip()[:500]
    return redact_text(detail) if redact else detail
