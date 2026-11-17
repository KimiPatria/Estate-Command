"""Data-quality watchdog (forecast/watchdog.py) and the model-health overview
(forecast_fleet.py, /forecast/fleet, /forecast/watchdog).

The watchdog tests build small synthetic series folders, one planted problem each,
so they need no run on disk. The API tests need a published run and are skipped
without one.
"""
import importlib.util
import json
import os
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("epms_watchdog_t", ROOT / "forecast" / "watchdog.py")
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)

TODAY = date(2026, 11, 16)


def make_series(tmp: Path, start="2024-06-01", end="2026-11-04", per_day=1000, blocks=20,
                drop_days=(), spike=None, drop_blocks_month=None, dup_day=None, fill=0.95,
                gate_ok=True, weight=None, tickets_per_day=40):
    """One estate's series folder in the contract's shape (meta, daily, monthly, oph)."""
    tmp.mkdir(parents=True, exist_ok=True)
    days = pd.date_range(start, end, freq="D")
    days = days[days.dayofweek != 6]                              # Sundays off
    days = days[~days.strftime("%Y-%m-%d").isin(drop_days)]
    rows = []
    for d in days:
        n_blocks = blocks // 3 if drop_blocks_month and d.strftime("%Y-%m") == drop_blocks_month else blocks
        for b in range(n_blocks):
            rows.append({"harvest_date": d.strftime("%Y-%m-%d"), "division_code": "1",
                         "block_code": f"{b:03d}", "bunches_total": per_day // blocks})
    oph = pd.DataFrame(rows)
    if spike:
        oph.loc[oph["harvest_date"] == spike, "bunches_total"] *= 5
    daily = oph.groupby("harvest_date", as_index=False)["bunches_total"].sum()
    daily["tickets"] = tickets_per_day
    if dup_day:
        daily = pd.concat([daily, daily[daily["harvest_date"] == dup_day]]).sort_values("harvest_date")
    dd = pd.to_datetime(daily["harvest_date"])
    m = daily.assign(month=dd.dt.to_period("M").astype(str)).groupby("month").agg(
        bunches_total=("bunches_total", "sum"), n_harvest_days=("harvest_date", "nunique")).reset_index()
    first, last = dd.min(), dd.max()
    m["days_in_month"] = [pd.Period(x, "M").days_in_month for x in m["month"]]
    m["days_covered"] = m["days_in_month"]
    m["is_partial"] = [int(x in (first.strftime("%Y-%m"), last.strftime("%Y-%m")) and
                           not (x == first.strftime("%Y-%m") and first.day == 1)) for x in m["month"]]
    daily.to_csv(tmp / "daily.csv", index=False)
    m.to_csv(tmp / "monthly.csv", index=False)
    oph.to_csv(tmp / "oph.csv", index=False)
    if weight is not None:
        pd.DataFrame(weight).to_csv(tmp / "weight_monthly.csv", index=False)
    full = m[m["is_partial"] == 0]
    meta = {"last_day": last.strftime("%Y-%m-%d"), "first_full_month": full["month"].min(),
            "last_full_month": full["month"].max(), "full_months": int(len(full)),
            "gate": {"ok": gate_ok, "checks": [{"name": "monthly file adds up to the daily totals",
                                                 "ok": gate_ok, "detail": "x"}]},
            "columns": {"ZMASAK": {"item": "bunches_total", "written": "bunches_total", "fill_rate": fill}}}
    (tmp / "meta.json").write_text(json.dumps(meta))
    return tmp


def levels(findings, check):
    return [f["level"] for f in findings if f["check"] == check]


def by(findings, check):
    return [f for f in findings if f["check"] == check]


# ── per-estate checks ───────────────────────────────────────────────────────

def test_clean_series_raises_nothing(tmp_path):
    d = make_series(tmp_path / "A")
    f = wd.check_estate("staging", "A", str(d), TODAY)
    bad = [x for x in f if x["level"] in ("alert", "warn")]
    assert not bad, [(x["check"], x["title"]) for x in bad]
    assert {x["dimension"] for x in f} <= set(wd.DIMENSIONS)
    assert all(x["title"] for x in f)


def test_stale_data_alerts_and_says_what_it_affects(tmp_path):
    d = make_series(tmp_path / "A", end="2026-05-29")
    f = by(wd.check_estate("live", "A", str(d), TODAY), "freshness")[0]
    assert f["level"] == "alert" and "171 days" in f["title"]
    assert "already past" in f["affects"]


def test_short_history_warns_then_alerts(tmp_path):
    warn = by(wd.check_estate("s", "A", str(make_series(tmp_path / "A", start="2025-06-01")), TODAY), "history_depth")
    alert = by(wd.check_estate("s", "B", str(make_series(tmp_path / "B", start="2026-04-01")), TODAY), "history_depth")
    assert warn[0]["level"] == "warn" and alert[0]["level"] == "alert"


def test_harvest_gap_is_found_with_its_months(tmp_path):
    gap = [str(x.date()) for x in pd.date_range("2025-08-04", "2025-08-20")]
    f = by(wd.check_estate("s", "A", str(make_series(tmp_path / "A", drop_days=gap)), TODAY), "harvest_gaps")
    assert f[0]["level"] == "alert" and f[0]["months"] == ["2025-08"]
    short = by(wd.check_estate("s", "A", str(tmp_path / "A"), TODAY), "short_months")
    assert short[0]["level"] == "warn" and short[0]["months"] == ["2025-08"]


def test_a_month_the_model_imputes_is_information_not_a_warning(tmp_path):
    gap = [str(x.date()) for x in pd.date_range("2025-08-04", "2025-08-20")]
    d = make_series(tmp_path / "A", drop_days=gap)
    f = wd.check_estate("live", "A", str(d), TODAY, handled={"2025-08"})
    for check in ("harvest_gaps", "short_months"):
        row = by(f, check)[0]
        assert row["level"] == "info" and "already treats 2025-08" in row["affects"], check


def test_one_inflated_day_is_a_spike(tmp_path):
    f = by(wd.check_estate("s", "A", str(make_series(tmp_path / "A", spike="2025-04-02")), TODAY), "daily_spikes")
    assert f[0]["level"] == "alert" and f[0]["days"] == ["2025-04-02"]
    assert "2025-04" in f[0]["affects"]


def test_batch_posting_is_reported_once_not_as_spikes(tmp_path):
    d = make_series(tmp_path / "A")
    daily = pd.read_csv(d / "daily.csv")
    # a big posting day every few days, small days in between (K3's pattern)
    daily["bunches_total"] = np.where(np.arange(len(daily)) % 5 == 0, 9000, 400)
    daily.to_csv(d / "daily.csv", index=False)
    f = by(wd.check_estate("live", "A", str(d), TODAY), "daily_spikes")
    assert len(f) == 1 and f[0]["level"] == "info" and "batches" in f[0]["title"]


def test_missing_blocks_and_duplicates(tmp_path):
    d = make_series(tmp_path / "A", drop_blocks_month="2026-03", dup_day="2026-02-10")
    f = wd.check_estate("s", "A", str(d), TODAY)
    blk = by(f, "block_coverage")
    assert blk[0]["level"] == "alert" and blk[0]["months"] == ["2026-03"]
    assert "alert" in levels(f, "duplicates")


def test_failed_gate_and_low_fill(tmp_path):
    f = wd.check_estate("s", "A", str(make_series(tmp_path / "A", gate_ok=False, fill=0.6)), TODAY)
    assert levels(f, "extract_gate") == ["alert"]
    assert levels(f, "target_fill") == ["alert"]


def test_weight_summed_per_row_is_caught(tmp_path):
    w = {"month": ["2026-09", "2026-10"], "trips": [300, 310], "tonnes": [2400.0, 39000.0]}
    f = by(wd.check_estate("staging", "A", str(make_series(tmp_path / "A", weight=w)), TODAY), "weight_per_trip")
    assert [x["months"] for x in f if x["level"] == "alert"] == [["2026-10"]]


def test_ticket_ratio_compares_a_year_earlier(tmp_path):
    d = make_series(tmp_path / "A")
    daily = pd.read_csv(d / "daily.csv")
    # tickets triple from 2026-01: same bunches, written per block now
    daily.loc[daily["harvest_date"] >= "2026-01-01", "tickets"] *= 3
    daily.to_csv(d / "daily.csv", index=False)
    f = by(wd.check_estate("s", "A", str(d), TODAY), "ticket_ratio")
    assert len(f) == 1 and f[0]["level"] == "warn" and "2026-01" in f[0]["months"]


# ── across sources ──────────────────────────────────────────────────────────

def test_cross_source_finds_the_day_behind_a_month(tmp_path):
    live = make_series(tmp_path / "live", spike="2025-04-02")
    stg = make_series(tmp_path / "stg")
    f = wd.cross_source("EC", str(live), str(stg))
    assert {x["source"] for x in f} == {"live", "staging"}
    row = f[0]
    assert row["level"] in ("warn", "alert") and "2025-04" in row["months"]
    assert "2025-04-02" in row["detail"] and "not from the model" in row["affects"]


def test_cross_source_agrees_on_identical_series(tmp_path):
    f = wd.cross_source("EC", str(make_series(tmp_path / "a")), str(make_series(tmp_path / "b")))
    assert {x["level"] for x in f} == {"ok"}


def test_summary_takes_the_worst_level():
    rows = [{"source": "s", "estate": "a", "level": "ok"}, {"source": "s", "estate": "a", "level": "warn"},
            {"source": "s", "estate": "b", "level": "info"}]
    s = wd.summary(rows)
    assert s["estates"]["s/a"]["level"] == "warn" and s["estates"]["s/b"]["level"] == "info"
    assert s["totals"] == {"alert": 0, "warn": 1, "ok": 1, "info": 1}


# ── the overview's banding (forecast_fleet) ─────────────────────────────────

def test_fleet_bands_match_the_detail_thresholds():
    import forecast_fleet as ff
    p = {"headline_mase": 0.75, "headline_smape": 12.0, "benchmark_label": "naive",
         "accuracy_multih": {"pooled_bias_pct": -6.0, "n_origins": 4},
         "track_record": {"n": 12, "within": 9, "tol_pct": 10}, "flags": []}
    c = ff._model_cells(p)
    assert c["accuracy"]["level"] == "ok" and c["error"]["level"] == "warn"
    assert c["bias"]["level"] == "alert" and c["track"]["level"] == "ok"
    assert c["evidence"]["level"] == "warn"          # few origins caps at watch, never action
    assert ff._serving_cell({"serving": {"mode": "override"}})["level"] == "warn"
    assert ff._serving_cell({"fallback": {"reason": "x"}})["level"] == "alert"
    assert ff.STATUS[ff._worst(["ok", "warn", "na"])] == "watch"
    assert ff.STATUS[ff._worst(["ok", "na", "info"])] == "healthy"


# ── the API, on the published run ───────────────────────────────────────────

needs_run = pytest.mark.skipif(not (ROOT / "forecast" / "runs" / "current.json").exists(),
                               reason="no published forecast run")


@pytest.fixture(scope="module")
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import forecast_router as fr
    app = FastAPI()
    app.include_router(fr.router)
    return TestClient(app)


@needs_run
def test_fleet_lists_every_forecast(client):
    import forecast_fleet as ff
    body = client.get("/forecast/fleet").json()
    want = {(s, e) for s, e, _l, _m in ff.forecasts()}
    assert {(c["source"], c["estate"]) for c in body["cards"]} == want
    for c in body["cards"]:
        assert c["status"] in ("action", "watch", "healthy", "not_forecast")
        assert set(c["cells"]) == {k["id"] for k in body["checks"]}
        assert c["href"] == f"/model-health?db={c['source']}&estate={c['estate']}"
    s = body["summary"]
    assert sum(s["counts"].values()) == len(body["cards"])


@needs_run
def test_detail_carries_the_estates_watchdog_findings(client):
    body = client.get("/forecast/model-health", params={"source": "staging", "estate": "ec"}).json()
    f = body["watchdog"]["findings"]
    assert f and {x["estate"] for x in f} == {"ec"} and {x["source"] for x in f} == {"staging"}
    grp = client.get("/forecast/model-health", params={"source": "staging", "estate": "all"}).json()
    assert {x["estate"] for x in grp["watchdog"]["findings"]} == {"ea", "eb", "ec"}


@needs_run
def test_watchdog_endpoints(client):
    r = client.get("/forecast/watchdog", params={"source": "live"}).json()
    assert r["findings"] and {f["source"] for f in r["findings"]} == {"live"}
    run = client.post("/forecast/watchdog/run").json()
    assert set(run["summary"]["totals"]) == {"alert", "warn", "ok", "info"}
