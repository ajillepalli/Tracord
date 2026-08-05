import json
import sys
import zipfile
from pathlib import Path

from tracord.bundle import import_bundle
from tracord.recorder import record_command
from tracord.storage import run_dir


def test_overwrite_repairs_run_with_corrupt_existing_trace(tmp_path: Path):
    source_store = tmp_path / "source"
    trace = record_command(
        [sys.executable, "-c", "print('repair')"],
        root=source_store,
    )
    source_dir = run_dir(source_store, str(trace["run_id"]))
    bundle = tmp_path / "repair.tracord.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", json.dumps(trace))
        archive.write(source_dir / "stdout.log", "stdout.log")
        archive.write(source_dir / "stderr.log", "stderr.log")

    target_store = tmp_path / "target"
    target_dir = run_dir(target_store, str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "trace.json").write_text("{not-json", encoding="utf-8")

    imported = import_bundle(root=target_store, bundle_path=bundle, overwrite=True)

    assert imported["run_id"] == trace["run_id"]
    assert json.loads((target_dir / "trace.json").read_text(encoding="utf-8"))["run_id"] == trace["run_id"]
