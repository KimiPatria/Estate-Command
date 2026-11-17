"""Phase 5 of buildplan_forecast_mlops.md: versioned runs and the one refresh command.

  * paths resolve to a refresh in progress (FORECAST_RUN_DIR), else the published
    run (forecast/runs/current.json), else the legacy folders;
  * a step that fails leaves the published run served and keeps the failed run;
  * pruning never removes the run being served; rollback refuses a failed run.
Everything runs in a temporary runs folder: the real one is never touched.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "forecast"))

import estate_config as ec     # noqa: E402
import refresh                 # noqa: E402


@pytest.fixture
def runs(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    d.mkdir()
    monkeypatch.setattr(ec, "RUNS_DIR", str(d))
    monkeypatch.setattr(ec, "CURRENT_PATH", str(d / "current.json"))
    monkeypatch.setattr(ec, "_pointer_cache", {"mtime": None, "value": None})
    monkeypatch.delenv(ec.RUN_ENV, raising=False)
    return d


def _publish(runs, run_id, status="published"):
    (runs / run_id / "lab").mkdir(parents=True)
    (runs / run_id / "run.json").write_text(json.dumps({"run_id": run_id, "status": status}))


def _point(runs, run_id):
    (runs / "current.json").write_text(json.dumps({"run_id": run_id}))


def test_paths_are_legacy_before_the_first_run(runs):
    assert ec.lab_dir() == ec.LEGACY_LAB_DIR
    assert ec.series_dir("staging", "EC") == ec.legacy_series_dir("staging", "EC")


def test_paths_follow_the_published_run(runs):
    _publish(runs, "r1")
    _point(runs, "r1")
    assert ec.lab_dir() == str(runs / "r1" / "lab")
    assert ec.series_dir("live", "EB") == str(runs / "r1" / "series" / "live" / "EB")


def test_a_refresh_in_progress_sees_only_its_own_run(runs, monkeypatch):
    _publish(runs, "r1")
    _point(runs, "r1")
    monkeypatch.setenv(ec.RUN_ENV, str(runs / "r2.partial"))
    assert ec.lab_dir() == str(runs / "r2.partial" / "lab")


def test_a_failed_step_keeps_serving_the_published_run(runs, monkeypatch):
    _publish(runs, "r1")
    _point(runs, "r1")

    def boom(self, sid):
        raise refresh.StepFailed(f"extract {sid}", "gate failed: block file != daily total")

    monkeypatch.setattr(refresh.Run, "snapshot", lambda self: os.makedirs(self.dir))
    monkeypatch.setattr(refresh.Run, "extract", boom)
    assert refresh.main(["--source", "staging"]) == 1
    assert json.loads((runs / "current.json").read_text())["run_id"] == "r1"
    failed = [p for p in runs.iterdir() if p.name.endswith(".failed")]
    assert len(failed) == 1
    info = json.loads((failed[0] / "run.json").read_text())
    assert info["status"] == "failed" and info["failed_step"] == "extract staging"
    assert not [p for p in runs.iterdir() if p.name.endswith(".partial")]


def test_prune_keeps_the_run_being_served(runs):
    for i in range(1, 9):
        _publish(runs, f"2026010{i}-000000-staging")
    _point(runs, "20260101-000000-staging")                  # the oldest is being served
    dropped = refresh.prune(keep=3)
    left = sorted(p.name for p in runs.iterdir() if p.is_dir())
    assert "20260101-000000-staging" in left and "20260101-000000-staging" not in dropped
    assert len([n for n in left if n.startswith("2026")]) == 4


def test_rollback_points_back_and_refuses_a_failed_run(runs):
    _publish(runs, "r1")
    _publish(runs, "r2")
    _point(runs, "r2")
    refresh.rollback("r1")
    assert json.loads((runs / "current.json").read_text())["run_id"] == "r1"
    _publish(runs, "r3.failed", status="failed")
    with pytest.raises(SystemExit):
        refresh.rollback("r3.failed")


def test_a_killed_refresh_is_taken_over_and_its_folder_kept(runs, monkeypatch):
    _publish(runs, "r1")
    _point(runs, "r1")
    (runs / "r2-staging.partial").mkdir()
    (runs / refresh.LOCK_NAME).write_text(json.dumps({"pid": 999999999}))     # no such process
    killed = refresh.acquire_lock()
    try:
        assert killed == ["r2-staging.partial"]
        assert (runs / "r2-staging.killed").is_dir() and not (runs / "r2-staging.partial").exists()
        assert json.loads((runs / "current.json").read_text())["run_id"] == "r1"
    finally:
        refresh.release_lock()
    assert not (runs / refresh.LOCK_NAME).exists()


def test_a_live_lock_refuses_a_second_refresh(runs):
    (runs / refresh.LOCK_NAME).write_text(json.dumps({"pid": os.getpid(), "run_id": "busy"}))
    with pytest.raises(SystemExit, match="another refresh is running"):
        refresh.acquire_lock()
