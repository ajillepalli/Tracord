from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracord import cli
from tracord.ci_output import serialize_json
from tracord.recorder import RecordError
from tracord.replay import ReplayError
from tracord.assertions import AssertionFailure
from tracord.run_listing import RunListing, RunListingError
from tracord.result_codes import ASSERTION_ERROR_CODES, JSON_OUTPUT_FAILURE_EXIT_CODE


def _command(entrypoint: str) -> list[str]:
    if entrypoint == "module":
        return [sys.executable, "-m", "tracord"]
    suffix = ".exe" if os.name == "nt" else ""
    user_script = (
        Path(site.getuserbase())
        / f"Python{sys.version_info.major}{sys.version_info.minor}"
        / "Scripts"
        / f"tracord{suffix}"
    )
    if user_script.exists():
        return [str(user_script)]
    pytest.skip("installed console entry point is unavailable")


def _run(entrypoint: str, *args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*_command(entrypoint), *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_entrypoints_emit_identical_empty_list_contract(
    entrypoint: str, tmp_path: Path
) -> None:
    completed = _run(
        entrypoint, "list", "--store", str(tmp_path / "missing"), "--json", cwd=tmp_path
    )
    expected = {
        "result_version": "tracord.list-result.v0",
        "command": "list",
        "ok": True,
        "exit_code": 0,
        "error": None,
        "runs": [],
        "skipped": 0,
        "truncated": False,
    }

    assert completed.returncode == 0
    assert completed.stdout == serialize_json(expected)
    assert completed.stderr == b""


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_record_list_assert_and_replay_json_are_exact_and_private(
    entrypoint: str, tmp_path: Path,
) -> None:
    store = tmp_path / "private-store"
    secret = "secret-child-output"
    record = _run(
        entrypoint,
        "record",
        "--store",
        str(store),
        "--json",
        "--",
        sys.executable,
        "-c",
        f"print('{secret}')",
        cwd=tmp_path,
    )
    payload = json.loads(record.stdout)
    assert record.returncode == 0
    assert record.stdout == serialize_json(payload)
    assert record.stderr == b""
    assert secret.encode() not in record.stdout
    assert str(tmp_path).encode() not in record.stdout
    assert b"python" not in record.stdout.lower()
    run_id = payload["run"]["run_id"]

    listing = _run(
        entrypoint, "list", "--store", str(store), "--json", cwd=tmp_path
    )
    list_payload = json.loads(listing.stdout)
    assert listing.returncode == 0
    assert listing.stdout == serialize_json(list_payload)
    assert list_payload["runs"][0]["run_id"] == run_id
    assert listing.stderr == b""

    asserted = _run(
        entrypoint,
        "assert",
        "--store",
        str(store),
        "--json",
        run_id,
        "--status",
        "passed",
        cwd=tmp_path,
    )
    assert asserted.returncode == 0
    assert json.loads(asserted.stdout)["outcome"] == "pass"
    assert asserted.stdout == serialize_json(json.loads(asserted.stdout))
    assert asserted.stderr == b""

    replayed = _run(
        entrypoint,
        "replay",
        "--store",
        str(store),
        "--json",
        run_id,
        cwd=tmp_path,
    )
    assert replayed.returncode == 0
    assert json.loads(replayed.stdout)["command"] == "replay"
    assert replayed.stdout == serialize_json(json.loads(replayed.stdout))
    assert replayed.stderr == b""
    assert secret.encode() not in replayed.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("--json", "list"),
        ("list", "--unknown"),
        ("record", "--json", "--timeout", "not-a-number"),
        ("replay", "--json", "--git-timeout", "zero", "run-a"),
        ("assert", "--json", "run-a", "--exit-code", "not-an-int"),
        ("list", "--json", "--unknown"),
    ],
)
def test_argparse_failures_remain_text_stderr(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    completed = _run("module", *args, cwd=tmp_path)

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr.startswith(b"usage:")


def test_parsed_usage_failures_are_json_stdout(tmp_path: Path) -> None:
    completed = _run("module", "record", "--json", cwd=tmp_path)
    payload = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert completed.stdout == serialize_json(payload)
    assert payload["error"] == "record_command_required"
    assert completed.stderr == b""


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("assert", "--json", "run-a"), "assertion_no_expectations"),
        (("assert", "--json", "../escape", "--status", "passed"), "invalid_run_id"),
        (("assert", "--json", "run-a", "--file", "cases.json"), "assertion_mode_conflict"),
        (
            ("assert", "--json", "run-a", "--case", "case-a", "--status", "passed"),
            "assertion_mode_conflict",
        ),
    ],
)
def test_assert_usage_failures_after_parsing_are_json(
    tmp_path: Path, args: tuple[str, ...], error: str
) -> None:
    completed = _run("module", *args, cwd=tmp_path)
    payload = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert completed.stdout == serialize_json(payload)
    assert payload["error"] == error
    assert completed.stderr == b""


def test_json_after_record_separator_is_a_child_argument_not_machine_mode(
    tmp_path: Path,
) -> None:
    completed = _run("module", "record", "--", "--json", cwd=tmp_path)

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr.startswith(b"tracord: record failed:")


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [("../private", "invalid_run_id"), ("missing", "run_not_found")],
)
def test_inspect_uses_safe_exact_trace_access(
    tmp_path: Path, run_id: str, expected: str
) -> None:
    completed = _run(
        "module",
        "inspect",
        "--store",
        str(tmp_path / "private-store"),
        run_id,
        cwd=tmp_path,
    )

    assert completed.returncode in {1, 2}
    assert completed.stdout == b""
    assert completed.stderr.decode().splitlines() == [
        f"tracord: inspect failed: {expected}"
    ]
    assert str(tmp_path).encode() not in completed.stderr


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_help_and_version_remain_text(tmp_path: Path, option: str) -> None:
    completed = _run("module", option, cwd=tmp_path)

    assert completed.returncode == 0
    assert completed.stdout
    assert not completed.stdout.startswith(b"{")
    assert completed.stderr == b""


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("record_store_unwritable", "record_store_unwritable"),
        ("record_spawn_failed", "record_spawn_failed"),
    ],
)
def test_record_runtime_errors_have_fixed_json_mappings(
    code: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RecordError(code)

    monkeypatch.setattr(cli, "record_command", fail)

    assert cli.main(["record", "--json", "--", "child"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == expected
    assert captured.err == ""


@pytest.mark.parametrize(
    ("target", "argv", "expected"),
    [
        ("record_command", ["record", "--json", "--", "child"], "record_failed"),
        ("scan_runs", ["list", "--json"], "list_failed"),
        ("replay_run", ["replay", "--json", "run-a"], "replay_failed"),
    ],
)
def test_unexpected_runtime_errors_have_fixed_json_mappings(
    target: str,
    argv: list[str],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("C:/private/secret")

    monkeypatch.setattr(cli, target, fail)
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == expected
    assert "private" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("invalid_run_id", 2),
        ("replay_run_not_found", 1),
        ("replay_trace_missing", 1),
        ("replay_trace_unreadable", 1),
        ("replay_trace_invalid", 1),
        ("replay_run_identity_mismatch", 1),
        ("replay_run_identity_unverifiable", 1),
        ("replay_store_unwritable", 1),
        ("replay_spawn_failed", 1),
    ],
)
def test_replay_runtime_errors_have_fixed_json_mappings(
    code: str,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ReplayError(code)

    monkeypatch.setattr(cli, "replay_run", fail)

    assert cli.main(["replay", "--json", "run-a"]) == exit_code
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == code
    assert captured.err == ""


def test_list_runtime_error_has_fixed_json_mapping(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_root: Path) -> object:
        raise RunListingError("list_store_unreadable")

    monkeypatch.setattr(cli, "scan_runs", fail)

    assert cli.main(["list", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "list_store_unreadable"
    assert captured.err == ""


def test_projection_failure_is_distinct_from_child_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "record_command",
        lambda *_args, **_kwargs: {"status": "passed", "run_id": "unsafe id"},
    )

    assert cli.main(["record", "--json", "--", "child"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "record_result_invalid"
    assert payload["run"] is None


@pytest.mark.parametrize("command", ["list", "replay"])
def test_list_and_replay_projection_failures_are_fixed(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if command == "list":
        monkeypatch.setattr(
            cli,
            "scan_runs",
            lambda _root: RunListing(
                runs=({"run_id": "unsafe id"},), skipped=0, truncated=False
            ),
        )
        argv = ["list", "--json"]
    else:
        monkeypatch.setattr(
            cli,
            "replay_run",
            lambda **_kwargs: {"status": "passed", "run_id": "unsafe id"},
        )
        argv = ["replay", "--json", "run-a"]

    assert cli.main(argv) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == f"{command}_result_invalid"


@pytest.mark.parametrize("code", sorted(ASSERTION_ERROR_CODES))
def test_every_assertion_error_code_has_a_fixed_json_mapping(
    code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    args = SimpleNamespace(
        json_output=True,
        run_id="run-a",
        case_name=None,
        file=None,
    )

    assert cli._assert_error(code, exit_code=1, args=args) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == code
    assert captured.err == ""


@pytest.mark.parametrize("failures", [[], [AssertionFailure("assertion_mismatch", "status")]])
def test_assertion_result_construction_failure_has_fixed_fallback(
    failures: list[AssertionFailure],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "evaluate_run", lambda *_args: ("run-a", failures))
    original = cli.build_assertion_result
    calls = 0

    def fail_once(**kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise cli.CIOutputError("invalid")
        return original(**kwargs)

    monkeypatch.setattr(cli, "build_assertion_result", fail_once)
    assert cli.main(["assert", "--json", "run-a", "--status", "passed"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "assert_result_invalid"


def test_missing_generated_status_is_a_result_failure_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "record_command",
        lambda *_args, **_kwargs: {"run_id": "run-a"},
    )

    assert cli.main(["record", "--json", "--", "child"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "record_result_invalid"
    assert captured.err == ""


def test_unexpected_assertion_failure_is_path_free_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "C:/private/secret"

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "evaluate_run", fail)

    assert cli.main(["assert", "--json", "run-a", "--status", "passed"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "assert_failed"
    assert secret not in captured.out
    assert captured.err == ""


def test_assertion_json_hides_non_ci_safe_operational_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    operational_id = "rún"
    monkeypatch.setattr(cli, "evaluate_run", lambda *_args: (operational_id, []))

    assert (
        cli.main(["assert", "--json", operational_id, "--status", "passed"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "pass"
    assert payload["run_id"] is None


def test_json_emission_failure_returns_four_without_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailedEmitter:
        def emit(self, _payload: object) -> bool:
            return False

    monkeypatch.setattr(cli, "JsonEmitter", FailedEmitter)

    assert cli.main(["list", "--json"]) == JSON_OUTPUT_FAILURE_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
