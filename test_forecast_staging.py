"""The forecast pages' staging source: the extract's gate, the selection rule,
the accuracy arithmetic and the payload contract with the live source.

The first three groups run on made-up frames and need no database or artifacts.
The contract tests read the built staging artifacts and are skipped until they
exist (forecast/build_staging_series.py, build_artifacts.py --source staging,
select_staging.py)."""
import importlib.util
import types
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bss = _load("test_build_staging_series", ROOT / "forecast" / "build_staging_series.py")
sel = _load("test_select_staging", ROOT / "forecast" / "lab" / "select_staging.py")
import forecast_sources as fs  # noqa: E402


# ── extract gate ───────────────────────────────────────────────────────────

def _frames():
    days = pd.date_range("2026-01-28", "2026-03-03", freq="D")
    blocks = pd.DataFrame([{"harvest_date": d, "division_code": "1", "block_code": b, "bunches_total": v}
                           for d in days for b, v in (("001", 100.0), ("002", 50.0))])
    daily = blocks.groupby("harvest_date", as_index=False)["bunches_total"].sum()
    daily["tickets"] = 2
    return blocks, daily


def test_gate_passes_when_the_three_files_agree():
    blocks, daily = _frames()
    result = bss.gate(blocks, daily, bss.monthly(daily))
    assert result["ok"], result


def test_gate_catches_a_join_that_multiplies_the_total():
    """A block with two current BLK rows repeats its tickets: the classic fan-out."""
    blocks, daily = _frames()
    dup = blocks[blocks["block_code"] == "001"].assign(division_code="2")
    result = bss.gate(pd.concat([blocks, dup]), daily, bss.monthly(daily))
    assert not result["ok"]
    failed = {c["name"] for c in result["checks"] if not c["ok"]}
    assert "block file adds up to the estate total, day for day" in failed
    assert "no block sits in two divisions" in failed


def test_gate_catches_a_block_without_a_division():
    blocks, daily = _frames()
    blocks.loc[blocks["block_code"] == "002", "division_code"] = None
    assert not bss.gate(blocks, daily, bss.monthly(daily))["ok"]


def test_monthly_flags_months_the_window_only_partly_covers():
    _blocks, daily = _frames()
    m = bss.monthly(daily).set_index("month")
    assert list(m.index) == ["2026-01", "2026-02", "2026-03"]
    assert m.loc["2026-01", "is_partial"] == 1 and m.loc["2026-03", "is_partial"] == 1
    assert m.loc["2026-02", "is_partial"] == 0
    assert m.loc["2026-02", "bunches_total"] == 28 * 150
    assert m["bunches_total"].sum() == daily["bunches_total"].sum()


# ── selection rule ─────────────────────────────────────────────────────────

def _fake_registry(errors):
    """errors: {model: {estate: absolute error}} on a flat series of 1,000 a month."""
    estates = {"stg_a": {"label": "A", "source": "staging"}, "stg_b": {"label": "B", "source": "staging"}}

    def backtest(model, estate, grain):
        err = errors.get(model, {}).get(estate)
        if err is None:
            return None, "not built"
        rows = [{"origin": f"2026-0{o}", "step": s, "period": "x", "actual": 1000.0,
                 "pred": 1000.0 + err, "scoreable": True} for o in range(1, 6) for s in (1, 2, 3, 4)]
        return pd.DataFrame(rows), ""

    models = [{"id": m, "label": m, "source": "live" if m == "naive_trailing3" else "artifact"} for m in errors]
    return types.SimpleNamespace(ESTATES=estates, MODELS=models, ARTIFACT_DIR=".", backtest=backtest,
                                 estates_of=lambda source: list(estates))


def test_selection_takes_the_lowest_mean_ratio_among_models_that_win_everywhere():
    reg = _fake_registry({
        "naive_trailing3": {"stg_a": 100, "stg_b": 100},
        "good": {"stg_a": 60, "stg_b": 80},         # mean ratio 0.70, wins on both
        "spiky": {"stg_a": 20, "stg_b": 110},       # mean ratio 0.65, but loses on B
        "meh": {"stg_a": 90, "stg_b": 90},
    })
    out = sel.decide(reg)
    assert out["selected"] == "good"
    assert out["scores"]["spiky"]["beats_yardstick_everywhere"] is False
    assert out["per_estate"]["stg_a"]["n_rows"] == 15, "steps 1-3 only"


def test_selection_falls_back_to_the_baseline_when_nothing_wins_everywhere():
    reg = _fake_registry({"naive_trailing3": {"stg_a": 100, "stg_b": 100},
                          "spiky": {"stg_a": 20, "stg_b": 110}})
    assert sel.decide(reg)["selected"] == "naive_trailing3"


def test_selection_refuses_when_an_estate_has_no_backtest():
    reg = _fake_registry({"naive_trailing3": {"stg_a": 100, "stg_b": 100}, "half": {"stg_a": 50}})
    out = sel.decide(reg)
    assert out["selected"] is None and out["candidates"] == []


# ── accuracy arithmetic ────────────────────────────────────────────────────

def _bt(rows):
    return pd.DataFrame(rows, columns=["origin", "step", "actual", "pred"]).set_index(["origin", "step"])


def test_accuracy_compares_model_and_benchmark_on_shared_rows_only():
    bt = _bt([("2026-01", 1, 100.0, 110.0), ("2026-02", 1, 100.0, 90.0), ("2026-01", 2, 100.0, 120.0)])
    bn = _bt([("2026-01", 1, 100.0, 140.0), ("2026-02", 1, 100.0, 60.0)])       # no step-2 row
    acc = fs._accuracy(bt, bn, 3)
    assert acc["n_rows"] == 2 and acc["n_origins"] == 2
    assert acc["pooled_mase"] == 0.25                                           # 10 / 40
    assert [h["step"] for h in acc["per_horizon"]] == [1]


def test_step_bands_measure_each_step_and_fill_unscored_ones_with_the_widest():
    bt = _bt([("2026-01", 1, 100.0, 100.0), ("2026-02", 1, 100.0, 100.0), ("2026-01", 2, 100.0, 150.0)])
    bands = fs._step_bands(bt, 3)
    assert bands[1]["band_pct"] == 0.0 and bands[1]["n"] == 2
    assert bands[2]["band_pct"] == 40.0 and bands[2]["n"] == 1                  # 2*50/250
    assert bands[3] == {"band_pct": 40.0, "n": 0, "coverage": None}


def test_tile_compares_with_the_same_months_a_year_earlier_or_not_at_all():
    hist = [{"month": f"2025-{m:02d}", "value": 100} for m in (5, 6, 7)]
    fc = [{"month": f"2026-{m:02d}", "ensemble": 110} for m in (5, 6, 7)]
    t = fs.tile({"history": hist, "forecast": fc, "avg_band_pct": 9.5})
    assert t["total"] == 330 and t["yoy_total"] == 300 and t["yoy_pct"] == 10.0
    assert (t["first_month"], t["last_month"]) == ("2026-05", "2026-07")
    assert fs.tile({"history": hist[:2], "forecast": fc, "avg_band_pct": 9.5})["yoy_pct"] is None


def test_group_is_the_row_by_row_sum_of_the_estates():
    def part(label, scale, months):
        return {"label": label, "history": [{"month": m, "value": 10 * scale, "underrecorded": False,
                                             "n_harvest_days": 25} for m in months],
                "steps": [("2026-11", 5.0 * scale), ("2026-12", 6.0 * scale)],
                "bt": _bt([("2026-08", 1, 10.0 * scale, 12.0 * scale), ("2026-09", 1, 10.0 * scale, 9.0 * scale)]),
                "bn": _bt([("2026-08", 1, 10.0 * scale, 11.0 * scale)]),
                "stale_note": "", "generated_at": "2026-11-10T10:00:00", "last_day": f"2026-11-0{scale + 3}"}
    g = fs._sum_bundles([part("A", 1, ["2026-09", "2026-10"]), part("B", 2, ["2026-08", "2026-09", "2026-10"])])
    assert [h["month"] for h in g["history"]] == ["2026-09", "2026-10"], "only months every estate has"
    assert g["history"][0]["value"] == 30 and g["steps"] == [("2026-11", 15.0), ("2026-12", 18.0)]
    assert g["bt"].loc[("2026-08", 1), "pred"] == 36.0 and len(g["bn"]) == 1
    assert g["last_day"] == "2026-11-04", "the group's data runs to the earliest estate's last day"


def test_group_refuses_estates_that_end_on_different_months():
    def part(last):
        return {"label": last, "history": [{"month": last, "value": 1, "underrecorded": False, "n_harvest_days": 1}],
                "steps": [], "bt": _bt([]), "bn": _bt([]), "stale_note": "", "generated_at": None, "last_day": None}
    with pytest.raises(fs.Unavailable):
        fs._sum_bundles([part("2026-09"), part("2026-10")])


# ── payload contract with the live source (needs the built artifacts) ──────

def _staging_ready():
    try:
        return bool(fs.modeled_estates())
    except Exception:
        return False


needs_staging = pytest.mark.skipif(not _staging_ready(), reason="staging artifacts not built")


@needs_staging
@pytest.mark.parametrize("estate", ["all", "ea", "eb", "ec"])
def test_staging_payload_has_every_field_the_page_reads(estate):
    p = fs.payload(estate, 3)
    for key in ("history", "anchor", "forecast", "interval", "scope", "drivers", "smape", "headline_smape",
                "headline_mase", "accuracy_multih", "model_name", "model_label", "trained_through", "horizon",
                "total_forecast", "total_3m", "avg_band_pct", "last_band_pct", "generated_at",
                "source", "components", "benchmark_label", "horizon_choices", "capabilities"):
        assert key in p, key
    assert p["source"] == "staging" and p["components"] == [] and p["drivers"]["months"] == []
    assert p["anchor"]["month"] == p["history"][-1]["month"]
    assert len(p["forecast"]) == 3
    for i, f in enumerate(p["forecast"], start=1):
        assert f["step"] == i and f["lower"] <= f["ensemble"] <= f["upper"]
        assert f["month"] > p["anchor"]["month"]
    assert set(p["interval"]["per_step"]) == {"1", "2", "3"}


@needs_staging
def test_staging_serves_at_most_six_months():
    assert len(fs.payload("ec", 6)["forecast"]) == 6
    assert len(fs.payload("ec", 12)["forecast"]) == 3, "an unsupported horizon falls back to the default"


@needs_staging
def test_staging_group_forecast_is_the_sum_of_its_estates():
    group = fs.payload("all", 3)
    parts = [fs.payload(e, 3) for e in ("ea", "eb", "ec")]
    for i, f in enumerate(group["forecast"]):
        assert abs(f["ensemble"] - sum(p["forecast"][i]["ensemble"] for p in parts)) <= len(parts)
    assert group["scope"]["modeled_count"] == 3 and group["scope"]["group_is_sum"] is True
    tiles = {e["id"]: e["tile"]["total"] for e in group["scope"]["estates"]}
    assert tiles == {p["scope"]["series_estate"]: p["total_forecast"] for p in parts}


@needs_staging
def test_staging_history_never_runs_past_the_forecast_anchor():
    """No month the forecast covers may also be drawn as recorded history."""
    for estate in ("all", "ea", "eb", "ec"):
        p = fs.payload(estate, 6)
        assert max(h["month"] for h in p["history"]) < min(f["month"] for f in p["forecast"])


@needs_staging
def test_live_and_staging_payloads_share_one_shape():
    import forecast_router as fr
    live, staging = fr.page_payload("k3"), fr.page_payload("all", source="staging")
    only_live = {"mase", "n_folds", "band_pct", "avg_effective_mase", "last_effective_mase"}
    missing = set(live) - set(staging) - only_live
    assert not missing, f"the page reads these from live but staging lacks them: {sorted(missing)}"
    assert set(staging["forecast"][0]) <= set(live["forecast"][0])
    assert live["source"] == "live" and live["components"] and live["horizon_choices"] == [3, 6, 12]
    assert all("tile" in e for e in live["scope"]["estates"] if e["modeled"])


@needs_staging
def test_staging_model_health_matches_the_page_payload():
    h, p = fs.health("all"), fs.payload("all", 6)
    assert h["source"] == "staging" and h["lifecycle"]["served_model"] == p["model_name"]
    assert h["accuracy"]["smape_h1_3"] == p["headline_smape"]
    assert any(r["model"] == p["model_name"] for r in h["scoreboard"])
    assert [s["step"] for s in h["interval"]["per_step"]] == [1, 2, 3, 4, 5, 6]
