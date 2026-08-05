import sys
from pathlib import Path

import pytest

from tracord import recorder
from tracord.redaction import REDACTION
from tracord.recorder import RecordError, record_command
from tracord.schema import validate_trace


def test_record_command_writes_trace(tmp_path: Path):
    trace = record_command(
        [sys.executable, "-c", "print('hello')"],
        root=tmp_path / ".tracord",
        name="unit-test",
    )

    assert trace["status"] == "passed"
    assert trace["exit_code"] == 0
    assert trace["kind"] == "command"
    assert trace["artifacts"] == {"stdout": "stdout.log", "stderr": "stderr.log"}
    assert [event["type"] for event in trace["events"]] == [
        "command.started",
        "command.finished",
    ]
    assert validate_trace(trace) == []
    trace_path = tmp_path / ".tracord" / "runs" / str(trace["run_id"]) / "trace.json"
    stdout_path = tmp_path / ".tracord" / "runs" / str(trace["run_id"]) / "stdout.log"
    assert trace_path.exists()
    assert stdout_path.read_text(encoding="utf-8") == "hello\n"


def test_record_command_redacts_stdout_and_stderr(tmp_path: Path):
    secret = "fixture-sensitive-value"
    trace = record_command(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"print('token={secret}'); "
                f"print('password={secret}', file=sys.stderr)"
            ),
        ],
        root=tmp_path / ".tracord",
    )

    run_path = tmp_path / ".tracord" / "runs" / str(trace["run_id"])
    stdout = (run_path / "stdout.log").read_text(encoding="utf-8")
    stderr = (run_path / "stderr.log").read_text(encoding="utf-8")
    assert stdout == f"token={REDACTION}\n"
    assert stderr == f"password={REDACTION}\n"
    assert secret not in stdout
    assert secret not in stderr


@pytest.mark.parametrize("failure_call", [1, 2])
def test_store_replacement_around_run_directory_publication_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    calls = 0

    def verify(_store: object) -> bool:
        nonlocal calls
        calls += 1
        return calls != failure_call

    monkeypatch.setattr(recorder, "verify_prepared_store", verify)
    with pytest.raises(RecordError) as exc_info:
        record_command([sys.executable, "-c", "pass"], root=tmp_path / ".tracord")
    assert exc_info.value.code == "record_store_unwritable"


@pytest.mark.parametrize("failure_call", [1, 2, 3, 4])
def test_identity_races_between_artifact_and_trace_publication_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    real_require = recorder._require_store_identity
    calls = 0

    def require(*args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise recorder.StoreSafetyError("changed")
        real_require(*args)

    monkeypatch.setattr(recorder, "_require_store_identity", require)
    with pytest.raises(RecordError) as exc_info:
        record_command([sys.executable, "-c", "pass"], root=tmp_path / ".tracord")
    assert exc_info.value.code == "record_store_unwritable"


@pytest.mark.parametrize("artifact", ["stdout.log", "stderr.log"])
def test_artifact_write_failures_are_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str
) -> None:
    original = Path.write_bytes

    def fail(path: Path, data: bytes) -> int:
        if path.name == artifact:
            raise OSError("private path")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(RecordError) as exc_info:
        record_command([sys.executable, "-c", "pass"], root=tmp_path / ".tracord")
    assert exc_info.value.code == "record_store_unwritable"
    assert str(tmp_path) not in str(exc_info.value)


def test_trace_write_failure_is_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("private path")

    monkeypatch.setattr(recorder, "write_json", fail)
    with pytest.raises(RecordError) as exc_info:
        record_command([sys.executable, "-c", "pass"], root=tmp_path / ".tracord")
    assert exc_info.value.code == "record_store_unwritable"
    assert str(tmp_path) not in str(exc_info.value)
