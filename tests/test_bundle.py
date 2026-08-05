import json
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import tracord.bundle as bundle_module
from tracord.bundle import export_run, import_bundle
from tracord.cli import main
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


def test_export_atomically_overwrites_existing_bundle(tmp_path: Path):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('replace')"], root=store)
    bundle_path = tmp_path / "run.tracord.zip"
    bundle_path.write_bytes(b"old bundle")

    export_run(
        root=store,
        run_id=str(trace["run_id"]),
        output_path=bundle_path,
        overwrite=True,
    )

    with zipfile.ZipFile(bundle_path) as archive:
        assert json.loads(archive.read("trace.json"))["run_id"] == trace["run_id"]


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


@pytest.mark.parametrize("content", [b"not a zip", b"PK\x03\x04truncated"])
def test_import_cli_rejects_corrupt_archives_without_traceback(
    tmp_path: Path, capsys, content: bytes
):
    bundle = tmp_path / "corrupt.zip"
    bundle.write_bytes(content)

    exit_code = main(
        ["import", "--store", str(tmp_path / "store"), str(bundle)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "not a readable zip archive" in captured.err
    assert "Traceback" not in captured.err


def test_import_rejects_declared_metadata_size_before_creating_store(
    tmp_path: Path, monkeypatch
):
    bundle = tmp_path / "oversized.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", b"x" * 32)
    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_METADATA_BYTES", 16)
    store = tmp_path / "store"

    with pytest.raises(ValueError, match="metadata exceeds the size limit"):
        import_bundle(root=store, bundle_path=bundle)

    assert not store.exists()


def test_failed_export_does_not_publish_partial_or_replace_existing_bundle(
    tmp_path: Path, monkeypatch
):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=store)
    output = tmp_path / "run.zip"
    output.write_bytes(b"existing-good-bundle")
    original_open = bundle_module._open_export_file

    def fail_artifact(source_dir: Path, relative_path: str):
        if relative_path != "trace.json":
            raise FileNotFoundError("run artifact not found")
        return original_open(source_dir, relative_path)

    monkeypatch.setattr(bundle_module, "_open_export_file", fail_artifact)

    with pytest.raises(FileNotFoundError):
        export_run(
            root=store,
            run_id=str(trace["run_id"]),
            output_path=output,
            overwrite=True,
        )

    assert output.read_bytes() == b"existing-good-bundle"
    assert not any(path.suffix == ".partial" for path in tmp_path.iterdir())


def test_import_rejects_non_object_trace_without_traceback(tmp_path: Path):
    bundle = tmp_path / "non-object.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", "[]")

    with pytest.raises(ValueError, match="trace must be an object"):
        import_bundle(root=tmp_path / "store", bundle_path=bundle)


def test_import_cli_normalizes_corrupt_compressed_artifact(
    tmp_path: Path, capsys
):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('payload')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "corrupt-payload.zip",
    )
    with zipfile.ZipFile(bundle) as archive:
        info = archive.getinfo("stdout.log")
    content = bytearray(bundle.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", content, info.header_offset + 26)
    payload_offset = info.header_offset + 30 + name_length + extra_length
    content[payload_offset] ^= 0xFF
    bundle.write_bytes(content)

    exit_code = main(
        ["import", "--store", str(tmp_path / "target"), str(bundle)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "not a readable zip archive" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("existing", [False, True])
def test_failed_import_never_publishes_partial_run(
    tmp_path: Path, monkeypatch, existing: bool
):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('payload')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_store = tmp_path / "target"
    target_dir = run_dir(target_store, str(trace["run_id"]))
    if existing:
        target_dir.mkdir(parents=True)
        (target_dir / "sentinel.txt").write_text("preserve", encoding="utf-8")
    original_copy = bundle_module._copy_bounded
    calls = 0

    def fail_second_copy(source_stream, target_stream, limit):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected extraction failure")
        return original_copy(source_stream, target_stream, limit)

    monkeypatch.setattr(bundle_module, "_copy_bounded", fail_second_copy)

    with pytest.raises(ValueError, match="not a readable zip archive"):
        import_bundle(
            root=target_store,
            bundle_path=bundle,
            overwrite=existing,
        )

    if existing:
        assert (target_dir / "sentinel.txt").read_text(encoding="utf-8") == "preserve"
        assert not (target_dir / "trace.json").exists()
    else:
        assert not target_dir.exists()
    assert not any(".import-" in path.name for path in target_dir.parent.iterdir())


def test_import_rejects_aggregate_artifact_size_before_extraction(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('payload')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    with zipfile.ZipFile(bundle) as archive:
        metadata_size = archive.getinfo("trace.json").file_size + archive.getinfo(
            "manifest.json"
        ).file_size
    monkeypatch.setattr(
        bundle_module, "MAX_BUNDLE_UNCOMPRESSED_BYTES", metadata_size + 1
    )
    target = tmp_path / "target"

    with pytest.raises(ValueError, match="uncompressed size limit"):
        import_bundle(root=target, bundle_path=bundle)

    assert not target.exists()


@pytest.mark.parametrize(
    "artifact_path",
    ["manifest.json", "bundle-manifest.json", "stream:name.log", "trailing. "],
)
def test_export_rejects_nonportable_or_reserved_artifact_paths(
    tmp_path: Path, artifact_path: str
):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=store)
    trace_path = run_dir(store, str(trace["run_id"])) / "trace.json"
    trace["artifacts"]["hostile"] = artifact_path
    trace_path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact path|Windows|dot or space"):
        export_run(
            root=store,
            run_id=str(trace["run_id"]),
            output_path=tmp_path / "run.zip",
        )


def test_export_rejects_case_and_parent_artifact_collisions(tmp_path: Path):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=store)
    trace_path = run_dir(store, str(trace["run_id"])) / "trace.json"
    trace["artifacts"].update({"first": "Logs", "second": "logs/output.txt"})
    trace_path.write_text(json.dumps(trace), encoding="utf-8")

    with pytest.raises(ValueError, match="file and parent collision"):
        export_run(
            root=store,
            run_id=str(trace["run_id"]),
            output_path=tmp_path / "run.zip",
        )


def test_no_overwrite_export_preserves_concurrent_output(
    tmp_path: Path, monkeypatch
):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=store)
    output = tmp_path / "run.zip"
    original_link = os.link

    def competing_link(source, destination, **kwargs):
        Path(destination).write_bytes(b"concurrent-writer")
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(bundle_module.os, "link", competing_link)

    with pytest.raises(FileExistsError):
        export_run(
            root=store,
            run_id=str(trace["run_id"]),
            output_path=output,
        )

    assert output.read_bytes() == b"concurrent-writer"
    assert not any(path.suffix == ".partial" for path in tmp_path.iterdir())


def test_import_rejects_case_colliding_members(tmp_path: Path):
    bundle = tmp_path / "collision.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", "{}")
        archive.writestr("Log.txt", "first")
        archive.writestr("log.txt", "second")

    with pytest.raises(ValueError, match="case-insensitive"):
        import_bundle(root=tmp_path / "store", bundle_path=bundle)


def test_import_regenerates_untrusted_manifest(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    source_dir = run_dir(source, str(trace["run_id"]))
    bundle = tmp_path / "manifest.zip"
    hostile_manifest = "[" * 300 + '"untrusted"' + "]" * 300
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("manifest.json", hostile_manifest)
        archive.writestr("trace.json", json.dumps(trace))
        archive.write(source_dir / "stdout.log", "stdout.log")
        archive.write(source_dir / "stderr.log", "stderr.log")

    imported = import_bundle(root=tmp_path / "target", bundle_path=bundle)
    imported_manifest = json.loads(
        (run_dir(tmp_path / "target", str(imported["run_id"])) / "bundle-manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert imported_manifest["bundle_version"] == "tracord.bundle.v0"
    assert imported_manifest["run_id"] == trace["run_id"]
    assert "untrusted" not in json.dumps(imported_manifest)


def test_import_rejects_declared_oversized_manifest(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    source_dir = run_dir(source, str(trace["run_id"]))
    trace_bytes = json.dumps(trace).encode("utf-8")
    bundle = tmp_path / "oversized-manifest.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("manifest.json", b"x" * (len(trace_bytes) + 100))
        archive.writestr("trace.json", trace_bytes)
        archive.write(source_dir / "stdout.log", "stdout.log")
        archive.write(source_dir / "stderr.log", "stderr.log")
    monkeypatch.setattr(
        bundle_module, "MAX_BUNDLE_METADATA_BYTES", len(trace_bytes) + 10
    )

    with pytest.raises(ValueError, match="metadata exceeds the size limit"):
        import_bundle(root=tmp_path / "target", bundle_path=bundle)


def test_export_falls_back_when_hardlinks_are_unsupported(tmp_path: Path, monkeypatch):
    store = tmp_path / "store"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=store)
    output = tmp_path / "run.zip"

    def unsupported_link(*args, **kwargs):
        raise OSError("hardlinks unsupported")

    monkeypatch.setattr(bundle_module.os, "link", unsupported_link)

    export_run(
        root=store,
        run_id=str(trace["run_id"]),
        output_path=output,
    )

    with zipfile.ZipFile(output) as archive:
        assert archive.getinfo("trace.json").file_size > 0


@pytest.mark.parametrize("run_id", ["stream:name", "trailing.", "NUL"])
def test_import_rejects_nonportable_run_ids(tmp_path: Path, run_id: str):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    trace["run_id"] = run_id
    bundle = tmp_path / "hostile-run-id.zip"
    with zipfile.ZipFile(bundle, mode="w") as archive:
        archive.writestr("trace.json", json.dumps(trace))
        archive.writestr("stdout.log", "")
        archive.writestr("stderr.log", "")

    with pytest.raises(ValueError, match="invalid run id"):
        import_bundle(root=tmp_path / "target", bundle_path=bundle)


def test_import_rejects_case_alias_of_existing_run(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    runs = tmp_path / "target" / "runs"
    runs.mkdir(parents=True)
    (runs / str(trace["run_id"]).upper()).mkdir()

    with pytest.raises(ValueError, match="case-insensitive"):
        import_bundle(root=tmp_path / "target", bundle_path=bundle)


def test_publish_failure_restores_existing_run_and_cleans_transaction(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('new')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "sentinel.txt").write_text("old", encoding="utf-8")
    original_rename = os.rename
    rename_calls = 0

    def fail_publish(source_path, target_path):
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 2:
            raise OSError("injected publish failure")
        return original_rename(source_path, target_path)

    monkeypatch.setattr(bundle_module.os, "rename", fail_publish)

    with pytest.raises(OSError, match="injected publish failure"):
        import_bundle(
            root=tmp_path / "target",
            bundle_path=bundle,
            overwrite=True,
        )

    assert (target_dir / "sentinel.txt").read_text(encoding="utf-8") == "old"
    assert not any(
        ".import-" in path.name
        or ".backup-" in path.name
        or path.name.endswith(".transaction.json")
        for path in target_dir.parent.iterdir()
    )


def test_backup_cleanup_retries_before_import_succeeds(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('new')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "sentinel.txt").write_text("old", encoding="utf-8")
    original_rmtree = bundle_module.shutil.rmtree
    cleanup_calls = 0

    def fail_once(path, *args, **kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise OSError("transient cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(bundle_module.shutil, "rmtree", fail_once)

    import_bundle(
        root=tmp_path / "target",
        bundle_path=bundle,
        overwrite=True,
    )

    assert cleanup_calls == 2
    assert not any(
        ".backup-" in path.name or path.name.endswith(".transaction.json")
        for path in target_dir.parent.iterdir()
    )


def test_next_import_recovers_interrupted_backup_transaction(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('new')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "sentinel.txt").write_text("old", encoding="utf-8")
    staging_dir = target_dir.parent / (
        bundle_module._transaction_prefix(target_dir, "import") + "interrupted"
    )
    staging_dir.mkdir()
    (staging_dir / "partial.txt").write_text("partial", encoding="utf-8")
    backup_dir = target_dir.parent / (
        bundle_module._transaction_prefix(target_dir, "backup") + "interrupted"
    )
    journal_path = bundle_module._transaction_journal_path(target_dir)
    bundle_module._write_import_transaction(
        journal_path, staging_dir, backup_dir, target_dir
    )
    os.rename(target_dir, backup_dir)

    with pytest.raises(FileExistsError):
        import_bundle(root=tmp_path / "target", bundle_path=bundle)

    assert (target_dir / "sentinel.txt").read_text(encoding="utf-8") == "old"
    assert not staging_dir.exists()
    assert not backup_dir.exists()
    assert not journal_path.exists()


def test_portable_run_id_length_boundary():
    bundle_module.validate_run_id("r" * 128)

    with pytest.raises(ValueError, match="portable directory name"):
        bundle_module.validate_run_id("r" * 129)


def test_concurrent_process_cannot_recover_active_import(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_store = tmp_path / "target"
    target_dir = run_dir(target_store, str(trace["run_id"]))
    target_dir.parent.mkdir(parents=True)
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "from tracord.bundle import _import_run_lock\n"
        f"target = Path({str(target_dir)!r})\n"
        "with _import_run_lock(target):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"

        with pytest.raises(ValueError, match="already in progress"):
            import_bundle(root=target_store, bundle_path=bundle)
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert not target_dir.exists()


def test_journal_sync_failure_leaves_existing_run_untouched(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('new')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "sentinel.txt").write_text("old", encoding="utf-8")

    def fail_sync(descriptor):
        raise OSError("injected sync failure")

    monkeypatch.setattr(bundle_module.os, "fsync", fail_sync)

    with pytest.raises(OSError, match="injected sync failure"):
        import_bundle(
            root=tmp_path / "target", bundle_path=bundle, overwrite=True
        )

    assert (target_dir / "sentinel.txt").read_text(encoding="utf-8") == "old"
    assert not any(
        path.name.endswith((".partial", ".transaction.json"))
        or ".import-" in path.name
        or ".backup-" in path.name
        for path in target_dir.parent.iterdir()
    )


def test_backed_up_phase_failure_rolls_back_existing_run(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('new')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "sentinel.txt").write_text("old", encoding="utf-8")
    original_write = bundle_module._write_import_transaction

    def fail_backed_up(*args, phase="prepared", **kwargs):
        if phase == "backed_up":
            raise OSError("injected phase failure")
        return original_write(*args, phase=phase, **kwargs)

    monkeypatch.setattr(bundle_module, "_write_import_transaction", fail_backed_up)

    with pytest.raises(OSError, match="injected phase failure"):
        import_bundle(
            root=tmp_path / "target", bundle_path=bundle, overwrite=True
        )

    assert (target_dir / "sentinel.txt").read_text(encoding="utf-8") == "old"
    assert not any(
        path.name.endswith(".transaction.json")
        or ".import-" in path.name
        or ".backup-" in path.name
        for path in target_dir.parent.iterdir()
    )


def test_committed_cleanup_is_deferred_and_recovered(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('new')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_store = tmp_path / "target"
    target_dir = run_dir(target_store, str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "sentinel.txt").write_text("old", encoding="utf-8")
    original_rmtree = bundle_module.shutil.rmtree

    def persistent_failure(path, *args, **kwargs):
        raise OSError("persistent cleanup failure")

    monkeypatch.setattr(bundle_module.shutil, "rmtree", persistent_failure)

    imported = import_bundle(root=target_store, bundle_path=bundle, overwrite=True)

    assert imported["run_id"] == trace["run_id"]
    assert not (target_dir / "sentinel.txt").exists()
    assert any(path.name.endswith(".transaction.json") for path in target_dir.parent.iterdir())

    monkeypatch.setattr(bundle_module.shutil, "rmtree", original_rmtree)
    with pytest.raises(FileExistsError):
        import_bundle(root=target_store, bundle_path=bundle)

    assert not any(
        path.name.endswith(".transaction.json") or ".backup-" in path.name
        for path in target_dir.parent.iterdir()
    )


def test_committed_journal_unlink_failure_is_recovered(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('new')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_store = tmp_path / "target"
    target_dir = run_dir(target_store, str(trace["run_id"]))
    target_dir.mkdir(parents=True)
    (target_dir / "sentinel.txt").write_text("old", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_journal_unlink(path: Path, *args, **kwargs):
        if path.name.endswith(".transaction.json"):
            raise OSError("persistent journal unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)

    imported = import_bundle(root=target_store, bundle_path=bundle, overwrite=True)

    assert imported["run_id"] == trace["run_id"]
    assert any(path.name.endswith(".transaction.json") for path in target_dir.parent.iterdir())

    monkeypatch.setattr(Path, "unlink", original_unlink)
    with pytest.raises(FileExistsError):
        import_bundle(root=target_store, bundle_path=bundle)

    assert not any(
        path.name.endswith(".transaction.json") for path in target_dir.parent.iterdir()
    )


def test_import_rejects_symlinked_lock_without_touching_target(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.parent.mkdir(parents=True)
    external = tmp_path / "external-lock-target"
    external.write_bytes(b"preserve")
    lock_path = bundle_module._import_lock_path(target_dir)
    try:
        os.symlink(external, lock_path)
    except OSError:
        pytest.skip("file symlink creation is unavailable")

    with pytest.raises(ValueError, match="lock file is unsafe"):
        import_bundle(root=tmp_path / "target", bundle_path=bundle)

    assert external.read_bytes() == b"preserve"


def test_import_rejects_hardlinked_lock_without_touching_target(tmp_path: Path):
    source = tmp_path / "source"
    trace = record_command([sys.executable, "-c", "print('safe')"], root=source)
    bundle = export_run(
        root=source,
        run_id=str(trace["run_id"]),
        output_path=tmp_path / "run.zip",
    )
    target_dir = run_dir(tmp_path / "target", str(trace["run_id"]))
    target_dir.parent.mkdir(parents=True)
    external = tmp_path / "external-lock-target"
    external.write_bytes(b"preserve")
    lock_path = bundle_module._import_lock_path(target_dir)
    try:
        os.link(external, lock_path)
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(ValueError, match="lock file is unsafe"):
        import_bundle(root=tmp_path / "target", bundle_path=bundle)

    assert external.read_bytes() == b"preserve"


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming an open lock")
def test_failed_lock_initialization_preserves_replacement_path(tmp_path: Path, monkeypatch):
    target_dir = tmp_path / "runs" / "run-id"
    target_dir.parent.mkdir(parents=True)
    lock_path = bundle_module._import_lock_path(target_dir)
    original_path = lock_path.with_suffix(".original")

    def swap_and_fail(descriptor):
        os.rename(lock_path, original_path)
        lock_path.write_bytes(b"replacement")
        raise OSError("injected lock sync failure")

    monkeypatch.setattr(bundle_module.os, "fsync", swap_and_fail)

    with pytest.raises(OSError, match="injected lock sync failure"):
        bundle_module._open_import_lock(lock_path)

    assert lock_path.read_bytes() == b"replacement"
    assert original_path.read_bytes() == b"\0"
