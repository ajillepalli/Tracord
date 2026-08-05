from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tracord import cli, run_listing
from tracord.run_listing import RunListing, scan_runs
from tracord.schema import SCHEMA_VERSION


def _trace(run_id: str, **overrides: object) -> dict[str, object]:
    trace: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "kind": "command",
        "name": None,
        "status": "passed",
        "command": ["private-command"],
        "cwd": "C:/private/workspace",
        "started_at": "2026-08-05T00:00:00Z",
        "finished_at": "2026-08-05T00:00:01Z",
        "duration_ms": 1,
        "exit_code": 0,
        "timed_out": False,
        "redacted": True,
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
        "events": [],
    }
    trace.update(overrides)
    return trace


def _write_trace(root: Path, run_id: str, **overrides: object) -> Path:
    run = root / "runs" / run_id
    run.mkdir(parents=True)
    path = run / "trace.json"
    path.write_text(json.dumps(_trace(run_id, **overrides)), encoding="utf-8")
    return path


def test_missing_store_is_empty_and_read_only(tmp_path: Path) -> None:
    store = tmp_path / "missing"

    assert scan_runs(store) == RunListing(runs=(), skipped=0, truncated=False)
    assert not store.exists()


def test_listing_is_descending_and_rejects_malformed_and_unsafe_ids(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    _write_trace(store, "run-a")
    _write_trace(store, "run-z")
    _write_trace(store, "run-m", status="unknown")
    _write_trace(store, "bad id")

    listing = scan_runs(store)

    assert [trace["run_id"] for trace in listing.runs] == ["run-z", "run-a"]
    assert listing.skipped == 2
    assert listing.truncated is False


def test_hard_linked_trace_is_skipped(tmp_path: Path) -> None:
    store = tmp_path / "store"
    trace_path = _write_trace(store, "run-a")
    try:
        os.link(trace_path, trace_path.with_name("linked.json"))
    except OSError:
        pytest.skip("hard links are unavailable")

    listing = scan_runs(store)

    assert listing.runs == ()
    assert listing.skipped == 1
    assert listing.truncated is False


def test_candidate_and_output_caps_keep_greatest_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    for run_id in ("run-a", "run-b", "run-c", "run-d"):
        _write_trace(store, run_id)
    monkeypatch.setattr(run_listing, "MAX_LIST_CANDIDATES", 3)
    monkeypatch.setattr(run_listing, "MAX_LIST_RUNS", 2)

    listing = scan_runs(store)

    assert [trace["run_id"] for trace in listing.runs] == ["run-d", "run-c"]
    assert listing.skipped == 0
    assert listing.truncated is True


def test_high_sorting_imports_trigger_real_thousand_run_cap(tmp_path: Path) -> None:
    store = tmp_path / "store"
    generated = "20260805T120000-deadbeef"
    _write_trace(store, generated)
    for index in range(1_001):
        _write_trace(store, f"z-import-{index:04d}")

    listing = scan_runs(store)
    ids = [trace["run_id"] for trace in listing.runs]

    assert len(ids) == 1_000
    assert ids[0] == "z-import-1000"
    assert ids[-1] == "z-import-0001"
    assert generated not in ids
    assert listing.skipped == 0
    assert listing.truncated is True


def test_file_cap_precedes_aggregate_and_oversize_charges_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    small_path = _write_trace(store, "run-a")
    large_path = _write_trace(store, "run-z", name="x" * 1_000)
    small_size = small_path.stat().st_size
    assert large_path.stat().st_size > small_size
    monkeypatch.setattr(run_listing, "MAX_LIST_TRACE_BYTES", small_size)
    monkeypatch.setattr(run_listing, "MAX_LIST_AGGREGATE_BYTES", small_size + 1)

    listing = scan_runs(store)

    assert [trace["run_id"] for trace in listing.runs] == ["run-a"]
    assert listing.skipped == 1
    assert listing.truncated is False


def test_aggregate_limit_truncates_without_counting_current_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    trace_path = _write_trace(store, "run-a")
    monkeypatch.setattr(
        run_listing, "MAX_LIST_AGGREGATE_BYTES", trace_path.stat().st_size - 1
    )

    listing = scan_runs(store)

    assert listing.runs == ()
    assert listing.skipped == 0
    assert listing.truncated is True


def test_text_list_sanitizes_name_and_reports_incompleteness(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _trace("run-a", name="unsafe\x1b]0;title\x07")
    monkeypatch.setattr(
        cli,
        "scan_runs",
        lambda _root: RunListing(runs=(trace,), skipped=2, truncated=True),
    )

    assert cli.main(["list"]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "unsafe\\u001b]0;title\\u0007" in output
    assert output.endswith(
        "tracord: list incomplete: skipped=2 truncated=true\n"
    )


def test_timeout_text_output_remains_compatible(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _trace(
        "run-timeout", status="timeout", exit_code=None, timed_out=True, duration_ms=25
    )
    monkeypatch.setattr(
        cli,
        "scan_runs",
        lambda _root: RunListing(runs=(trace,), skipped=0, truncated=False),
    )

    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == "run-timeout timeout exit=None 25ms\n"


@pytest.mark.parametrize(
    "overrides",
    [
        {"exit_code": True},
        {"duration_ms": True},
        {"redacted": 1},
        {"name": 42},
        {"status": "passed\x1b[2J"},
    ],
)
def test_malformed_or_unsafe_scalars_are_skipped(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    store = tmp_path / "store"
    _write_trace(store, "run-a", **overrides)

    listing = scan_runs(store)

    assert listing.runs == ()
    assert listing.skipped == 1
    assert listing.truncated is False
