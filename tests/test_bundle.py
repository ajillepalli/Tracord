import os
import sys
import zipfile
from pathlib import Path

import pytest

from tracord.bundle import export_run, import_bundle
from tracord.recorder import record_command
from tracord.storage import run_dir, write_json


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

    with pytest.raises(ValueError, match="unsafe bundle member") as exc_info:
        import_bundle(root=tmp_path / "store", bundle_path=bundle_path)

    assert "../evil.txt" not in str(exc_info.value)


def test_export_refuses_to_overwrite_existing_bundle(tmp_path: Path):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('once')"], root=store)
    bundle_path = tmp_path / "run.tracord.zip"
    bundle_path.write_text("already here", encoding="utf-8")

    with pytest.raises(FileExistsError):
        export_run(root=store, run_id=str(trace["run_id"]), output_path=bundle_path)


def test_import_overwrite_rejects_symlinked_run_directory(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target = tmp_path / "target"
    runs = target / "runs"
    runs.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    linked_run = runs / str(trace["run_id"])
    try:
        os.symlink(external, linked_run, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(ValueError, match="real directory|path escapes root"):
        import_bundle(root=target, bundle_path=bundle, overwrite=True)

    assert list(external.iterdir()) == []


def test_nested_artifact_round_trips_without_parent_links(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    source_dir = run_dir(source, str(trace["run_id"]))
    nested = source_dir / "nested" / "artifact.log"
    nested.parent.mkdir()
    nested.write_text("nested content", encoding="utf-8")
    trace["artifacts"]["nested"] = "nested/artifact.log"
    write_json(source_dir / "trace.json", trace)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "nested.zip",
    )

    imported = import_bundle(root=tmp_path / "target", bundle_path=bundle)

    imported_nested = run_dir(tmp_path / "target", str(imported["run_id"])) / "nested" / "artifact.log"
    assert imported_nested.read_text(encoding="utf-8") == "nested content"


def test_import_rejects_symlinked_artifact_parent(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    source_dir = run_dir(source, str(trace["run_id"]))
    nested = source_dir / "nested" / "artifact.log"
    nested.parent.mkdir()
    nested.write_text("nested content", encoding="utf-8")
    trace["artifacts"]["nested"] = "nested/artifact.log"
    write_json(source_dir / "trace.json", trace)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "nested.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    external = tmp_path / "external-parent"
    external.mkdir()
    try:
        os.symlink(external, target_dir / "nested", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(ValueError, match="real directory|path escapes root"):
        import_bundle(root=tmp_path / "target", bundle_path=bundle, overwrite=True)

    assert list(external.iterdir()) == []
