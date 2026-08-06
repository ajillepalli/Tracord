from pathlib import Path

import pytest

from tracord import storage


def test_prepare_run_checks_store_identity_before_and_after_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def verify(_store: object) -> bool:
        nonlocal calls
        calls += 1
        return calls < 2

    monkeypatch.setattr(storage, "verify_prepared_store", verify)
    with pytest.raises(storage.StoreSafetyError, match="run_create_failed"):
        storage.prepare_run_for_write(tmp_path / ".tracord", "run-1")


def test_prepare_run_normalizes_store_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_root: Path) -> object:
        raise OSError("private path")

    monkeypatch.setattr(storage, "prepare_store_for_write", fail)
    with pytest.raises(storage.StoreSafetyError, match="run_create_failed"):
        storage.prepare_run_for_write(tmp_path / ".tracord", "run-1")


def test_prepared_artifacts_are_exclusive(tmp_path: Path) -> None:
    run = storage.prepare_run_for_write(tmp_path / ".tracord", "run-1")
    storage.write_prepared_bytes(run, "stdout.log", b"first")

    with pytest.raises(storage.StoreSafetyError, match="write_failed"):
        storage.write_prepared_bytes(run, "stdout.log", b"second")
    assert (run.path / "stdout.log").read_bytes() == b"first"


def test_atomic_json_failure_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = storage.prepare_run_for_write(tmp_path / ".tracord", "run-1")

    def fail(_source: object, _target: object) -> None:
        raise OSError("private")

    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(storage.StoreSafetyError, match="write_failed"):
        storage.publish_prepared_json(run, "trace.json", {"safe": True})

    assert not (run.path / "trace.json").exists()
    assert list(run.path.glob("*.tmp")) == []


def test_atomic_json_uses_the_shared_exact_encoder(tmp_path: Path) -> None:
    run = storage.prepare_run_for_write(tmp_path / ".tracord", "run-1")
    value = {"nested": [{"snowman": "☃"}]}
    storage.publish_prepared_json(run, "trace.json", value)
    assert (run.path / "trace.json").read_bytes() == storage.encode_prepared_json(value)
