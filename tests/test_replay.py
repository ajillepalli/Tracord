import json
import sys
from pathlib import Path

import pytest

from tracord.recorder import record_command
from tracord.replay import ReplayError, replay_run
from tracord.storage import run_dir


def test_replay_run_records_new_run(tmp_path: Path):
    store = tmp_path / ".tracord"
    original = record_command(
        [sys.executable, "-c", "print('again')"],
        root=store,
        name="original",
    )

    replayed = replay_run(root=store, run_id=str(original["run_id"]))

    assert replayed["run_id"] != original["run_id"]
    assert replayed["name"] == f"replay of {original['run_id']}"
    assert replayed["command"] == original["command"]
    stdout_path = run_dir(store, str(replayed["run_id"])) / "stdout.log"
    assert stdout_path.read_text(encoding="utf-8") == "again\n"


@pytest.mark.parametrize(
    ("run_id", "code"),
    [("../escape", "invalid_run_id"), ("missing", "replay_run_not_found")],
)
def test_replay_access_failures_are_fixed_and_path_free(
    tmp_path: Path, run_id: str, code: str
) -> None:
    with pytest.raises(ReplayError) as exc_info:
        replay_run(root=tmp_path / "private-store", run_id=run_id)

    assert exc_info.value.code == code
    assert str(tmp_path) not in str(exc_info.value)


def test_replay_rejects_trace_directory_identity_mismatch(tmp_path: Path) -> None:
    store = tmp_path / ".tracord"
    original = record_command([sys.executable, "-c", "pass"], root=store)
    original_id = str(original["run_id"])
    trace_path = run_dir(store, original_id) / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["run_id"] = "other-run"
    trace_path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ReplayError) as exc_info:
        replay_run(root=store, run_id=original_id)
    assert exc_info.value.code == "replay_run_identity_mismatch"


def test_replay_rejects_mcp_proxy_trace(tmp_path: Path) -> None:
    store = tmp_path / ".tracord"
    original = record_command([sys.executable, "-c", "pass"], root=store)
    original_id = str(original["run_id"])
    trace_path = run_dir(store, original_id) / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["mcp_proxy"] = {}
    trace_path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ReplayError) as exc_info:
        replay_run(root=store, run_id=original_id)

    assert exc_info.value.code == "replay_trace_invalid"
