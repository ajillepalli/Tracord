from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tracord import cli
from tracord.assertions import AssertionFailure, TraceExpectations
from tracord.recorder import record_command


def test_assert_parser_accepts_repository_case_and_file() -> None:
    args = cli.build_parser().parse_args(
        [
            "assert",
            "--store",
            "traces",
            "run-1",
            "--case",
            "ci",
            "--file",
            "checks.json",
        ]
    )

    assert args.case_name == "ci"
    assert args.file == Path("checks.json")


@pytest.mark.parametrize(
    "argv",
    [
        ["assert", "run-1"],
        ["assert", "run-1", "--file", "checks.json"],
        ["assert", "run-1", "--case", "ci", "--status", "passed"],
        ["assert", "run-1", "--case", "ci", "--exit-code", "0"],
        ["assert", "run-1", "--stdout-contains", ""],
        ["assert", "run-1", "--max-duration-ms", "-1"],
    ],
)
def test_assert_mode_and_value_errors_precede_file_io(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "load_assertion_case",
        lambda *_args, **_kwargs: pytest.fail("assertion file was read"),
    )
    monkeypatch.setattr(
        cli,
        "evaluate_run",
        lambda *_args, **_kwargs: pytest.fail("run was read"),
    )

    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert "tracord: assert failed:" in captured.err
    assert "run-1" not in captured.err


def test_assert_invalid_run_id_precedes_assertion_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unsafe = "bad\nrun"
    monkeypatch.setattr(
        cli,
        "load_assertion_case",
        lambda *_args, **_kwargs: pytest.fail("assertion file was read"),
    )

    assert cli.main(["assert", unsafe, "--case", "ci"]) == 2
    captured = capsys.readouterr()
    assert "invalid_run_id" in captured.err
    assert unsafe not in captured.err


def test_assert_default_file_follows_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "custom-store"
    observed: dict[str, object] = {}

    def load(path: Path, case_name: str) -> TraceExpectations:
        observed.update(path=path, case_name=case_name)
        return TraceExpectations(status="passed")

    monkeypatch.setattr(cli, "load_assertion_case", load)
    monkeypatch.setattr(cli, "evaluate_run", lambda *_args: ("run-1", []))

    assert cli.main(["assert", "--store", str(store), "run-1", "--case", "ci"]) == 0
    assert observed == {"path": store / "assertions.json", "case_name": "ci"}
    assert capsys.readouterr().out == "pass run-1\n"


def test_assert_explicit_file_is_cwd_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    observed: dict[str, object] = {}

    def load(path: Path, case_name: str) -> TraceExpectations:
        observed.update(path=path, case_name=case_name)
        return TraceExpectations(status="passed")

    monkeypatch.setattr(cli, "load_assertion_case", load)
    monkeypatch.setattr(cli, "evaluate_run", lambda *_args: ("run-1", []))

    assert (
        cli.main(["assert", "run-1", "--case", "ci", "--file", "checks.json"])
        == 0
    )
    assert observed == {"path": Path("checks.json"), "case_name": "ci"}


def test_assert_failure_output_uses_only_code_and_safe_location(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "expected-secret"
    monkeypatch.setattr(
        cli,
        "evaluate_run",
        lambda *_args: (
            "run-1",
            [AssertionFailure("assertion_mismatch", "stdout_contains")],
        ),
    )

    assert cli.main(["assert", "run-1", "--stdout-contains", secret]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "tracord: assert failed: assertion_mismatch at stdout_contains\n"
    assert secret not in captured.err


def test_assert_file_mode_runs_end_to_end_with_parent_relative_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "store"
    trace = record_command(
        [sys.executable, "-c", "print('ready')"],
        root=store,
    )
    (store / "assertions.json").write_text(
        json.dumps(
            {
                "schema_version": "tracord.assertions.v0",
                "cases": {
                    "smoke": {
                        "status": "passed",
                        "stdout_contains": "ready",
                        "no_timeout": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    exit_code = cli.main(
        [
            "assert",
            "--store",
            "../store",
            str(trace["run_id"]),
            "--case",
            "smoke",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"pass {trace['run_id']}\n"
    assert captured.err == ""


def test_assert_success_sanitizes_on_disk_run_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unsafe = "run\u2028id"
    monkeypatch.setattr(cli, "evaluate_run", lambda *_args: (unsafe, []))

    assert cli.main(["assert", unsafe, "--status", "passed"]) == 0
    assert capsys.readouterr().out == "pass run\\u2028id\n"
