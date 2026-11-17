"""Phase 6 of buildplan_forecast_mlops.md: nothing trains inside a request.

The ensemble (K3's fallback, the Lab's incumbent, model health on the fallback) is
fitted by the live refresh and read from the published run. These tests make any
fit inside a request fail loudly, then exercise every route that used to fit it.
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import forecast_router as fr
from forecast import estate_config

PUBLISHED = estate_config.current_run() is not None and \
    fr.precomputed_path("k3", fr.HORIZON_DEFAULT).exists()
needs_run = pytest.mark.skipif(not PUBLISHED, reason="no published live run with a precomputed ensemble")


@pytest.fixture
def no_fitting(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the ensemble was fitted inside a request")
    monkeypatch.setattr(fr, "_compute", boom)
    fr._CACHE.clear()
    yield
    fr._CACHE.clear()


@pytest.fixture
def client():
    import experiment_router as er
    app = FastAPI()
    app.include_router(fr.router)
    app.include_router(er.router)
    return TestClient(app)


@needs_run
def test_no_route_fits_the_ensemble(no_fitting, client):
    for url in ["/forecast/data?estate=k3&source=live", "/forecast/data?estate=ec&source=live",
                "/forecast/model-health?source=live", "/forecast/compare-data",
                "/experiment/forecast/data?estate=k3&grain=month",
                "/experiment/forecast/data?estate=ec&grain=month"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:300])


@needs_run
def test_the_fallback_reads_the_precomputed_ensemble(no_fitting, monkeypatch):
    import forecast_sources as fs

    def missing(*a, **k):
        raise fs.Unavailable("Chronos artifact missing")
    monkeypatch.setattr(fs, "payload", missing)
    d = fr.served_forecast("k3")
    assert d["fallback"]["model"] == "served_ensemble" and d["forecast"]


@needs_run
def test_precomputed_payload_equals_a_fresh_fit():
    fr._CACHE.clear()
    stored = json.loads(fr.precomputed_path("k3", 3).read_text(encoding="utf-8"))
    fresh = json.loads(json.dumps(fr._compute(estate="k3", horizon=3)))
    for d in (stored, fresh):
        d.pop("generated_at", None)
        d.pop("scope", None)               # the roster is rebuilt per request
    assert stored == fresh


def test_without_a_published_run_the_legacy_path_still_fits(monkeypatch, tmp_path):
    monkeypatch.setattr(estate_config, "current_run", lambda: None)
    monkeypatch.delenv(estate_config.RUN_ENV, raising=False)
    monkeypatch.setattr(fr, "precomputed_path", lambda e, h: tmp_path / "absent.json")
    monkeypatch.setattr(fr, "_compute", lambda estate, horizon: {"fitted": True})
    fr._CACHE.clear()
    try:
        assert fr.get_forecast("k3") == {"fitted": True}
    finally:
        fr._CACHE.clear()


def test_with_a_published_run_a_missing_payload_is_refused_not_fitted(monkeypatch, tmp_path):
    import forecast_sources as fs
    monkeypatch.setattr(estate_config, "current_run", lambda: {"run_id": "r", "dir": str(tmp_path)})
    monkeypatch.setattr(fr, "precomputed_path", lambda e, h: tmp_path / "absent.json")
    monkeypatch.setattr(fr, "_compute", lambda **k: pytest.fail("fitted"))
    fr._CACHE.clear()
    try:
        with pytest.raises(fs.Unavailable, match="refresh.py --source live"):
            fr.get_forecast("k3")
    finally:
        fr._CACHE.clear()
