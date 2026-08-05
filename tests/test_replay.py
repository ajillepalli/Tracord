import sys
from pathlib import Path

from tracord.recorder import record_command
from tracord.replay import replay_run
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
