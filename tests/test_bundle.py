import sys
import zipfile
from pathlib import Path

import pytest

from tracord.bundle import export_run, import_bundle
from tracord.recorder import record_command
from tracord.storage import run_dir


def test_export_import_round_trip(tmp_path: Path):
    source_store = tmp_path / "source"
    target_store = tmp_path / "target"
    trace = record_command(
        [sys.executable, "-c", "print('portable')"],
        root=source_store,
        name="bundle-test",
    )
    bundle_path = export_run(
        root=source_store,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.tracord.zip",
    )

    imported = import_bundle(root=target_store, bundle_path=bundle_path)

    imported_dir = run_dir(target_store, str(trace["run_id"]))
    assert imported["run_id"] == trace["run_id"]
    assert (imported_dir / "trace.json").exists()
    assert (imported_dir / "stdout.log").read_text(encoding="utf-8") == "portable\n"
    assert (imported_dir / "stderr.log").exists()
    assert (imported_dir / "bundle-manifest.json").exists()


def test_import_rejects_unsafe_bundle_member(tmp_path: Path):
    bundle_path = tmp_path / "unsafe.tracord.zip"
    with zipfile.ZipFile(bundle_path, mode="w") as archive:
        archive.writestr("../evil.txt", "bad")

    with pytest.raises(ValueError, match="unsafe bundle member"):
        import_bundle(root=tmp_path / "store", bundle_path=bundle_path)


def test_export_refuses_to_overwrite_existing_bundle(tmp_path: Path):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('once')"], root=store)
    bundle_path = tmp_path / "run.tracord.zip"
    bundle_path.write_text("already here", encoding="utf-8")

    with pytest.raises(FileExistsError):
        export_run(root=store, run_id=str(trace["run_id"]), output_path=bundle_path)
