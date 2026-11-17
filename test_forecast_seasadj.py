"""The seasonally adjusted Chronos-2 arm (chronos2_weekly_seasadj), 2026-11-13.

  * the seasonal index never sees past the forecast's cutoff, in any estate;
  * it stays off until the estate has seen all 12 months of the year;
  * the weekly index is the profile's monthly index, averaged over each bin's days;
  * the rule in force never serves the arm, only reports it; the new dated rule
    can serve it and scores steps 1-6; the frozen 2026-11-10 rule ignores it;
  * the seasonal-shape monitor fires on a moved peak or amplitude only.

Everything runs on synthetic series or stub registries (no company data, no Chronos).
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).parent
_LAB = ROOT / "forecast" / "lab"
sys.path.insert(0, str(ROOT / "forecast"))

import monitor  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(f"test_seasadj_{name}", _LAB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sp = _load("seasonal_profile")
sel_mod = _load("select_source")
old_rule = _load("select_staging")

ESTATES = {"x": {"source": "staging", "region": "r"}, "y": {"source": "staging", "region": "r"}}


def _history(n_months, start="2025-01", seed=0, peak_month=5, amp=0.5):
    rng = np.random.default_rng(seed)
    ps = pd.period_range(start, periods=n_months, freq="M")
    shape = 1 + amp * np.cos(2 * np.pi * (ps.month - peak_month) / 12)
    return {e: pd.DataFrame({"period": ps, "context_ok": True,
                             "value": 1000 * (k + 1) * shape * rng.uniform(0.95, 1.05, n_months)})
            for k, e in enumerate(ESTATES)}


# ── the index ────────────────────────────────────────────────────────────

def test_last_complete_month_of_a_cutoff():
    assert sp.last_complete_month("2026-06-30") == pd.Period("2026-06", "M")
    assert sp.last_complete_month("2026-06-29") == pd.Period("2026-05", "M")


def test_index_is_off_until_all_twelve_months_of_the_year_are_seen():
    h = _history(11)
    off = sp.adjustment(ESTATES, lambda e: h[e], "x", "2025-11-30")
    assert not off["active"] and off["months_of_year"] == 11 and "off" in off["note"]
    h = _history(12)
    on = sp.adjustment(ESTATES, lambda e: h[e], "x", "2025-12-31")
    assert on["active"] and on["months_of_year"] == 12 and len(on["shape"]) == 12


def test_index_never_sees_past_the_cutoff_in_any_estate():
    full = _history(22)
    cutoff = pd.Timestamp("2026-02-27")              # mid-month: Feb is not complete yet
    seen = {e: h[h["period"] <= pd.Period("2026-01", "M")] for e, h in full.items()}
    # same months up to the cutoff, wildly different after it, in BOTH estates
    later = {e: h.assign(value=np.where(h["period"] > pd.Period("2026-01", "M"), 1e9, h["value"]))
             for e, h in full.items()}
    a = sp.adjustment(ESTATES, lambda e: later[e], "x", cutoff)
    b = sp.adjustment(ESTATES, lambda e: seen[e], "x", cutoff)
    assert a["active"] and np.isclose(a["amp"], b["amp"]) and np.allclose(a["shape"], b["shape"])


def test_index_recovers_the_peak_month():
    h = _history(24, peak_month=6)
    adj = sp.adjustment(ESTATES, lambda e: h[e], "x", "2026-12-31")
    assert sp.peak_month(adj["shape"]) == 6


def test_weekly_index_is_the_monthly_index_averaged_over_the_bins_days():
    shape = np.linspace(-0.5, 0.6, 12)
    amp = 0.8
    inside = sp.bin_index(["2026-03-14"], amp, shape)[0]               # 8-14 March
    assert np.isclose(inside, np.exp(amp * shape[2]))
    straddle = sp.bin_index(["2026-04-03"], amp, shape)[0]             # 28 Mar - 3 Apr
    assert np.isclose(straddle, (4 * np.exp(amp * shape[2]) + 3 * np.exp(amp * shape[3])) / 7)


def test_dividing_and_multiplying_by_the_index_round_trips():
    shape, amp = np.linspace(-0.3, 0.3, 12), 1.1
    ends = pd.date_range("2025-01-07", periods=80, freq="7D")
    y = np.random.default_rng(1).uniform(100, 200, len(ends))
    idx = sp.bin_index(ends, amp, shape)
    assert np.all(idx > 0) and np.allclose((y / idx) * idx, y)


# ── the rules ────────────────────────────────────────────────────────────

def _stub_reg(errors, months, max_step=6):
    """errors[model][estate] = callable(step) -> absolute error at that step."""
    periods = {e: pd.period_range("2025-01", periods=n, freq="M") for e, n in months.items()}

    def month_history(e):
        return pd.DataFrame({"period": periods[e], "value": 100.0,
                             "context_ok": True, "scoreable": True, "underrecorded": False})

    def backtest(model, e, grain):
        if e not in errors.get(model, {}):
            return None, "not built"
        ps, rows = periods[e], []
        for i in range(5, len(ps) - 1):
            for step in range(1, max_step + 1):
                if i + step < len(ps):
                    rows.append({"origin": str(ps[i]), "step": step, "period": str(ps[i + step]),
                                 "actual": 100.0, "pred": 100.0 + errors[model][e](step),
                                 "scoreable": True})
        return pd.DataFrame(rows), ""

    return SimpleNamespace(
        ESTATES={e: {"label": e.upper(), "source": "staging"} for e in months},
        MODELS=[{"id": m, "source": "artifact"} for m in errors if m != "naive_trailing3"],
        estates_of=lambda source: list(months),
        estate_grain_status=lambda e, g: (True, "", months[e]),
        month_history=month_history, backtest=backtest, period_str=lambda p, g: str(p))


# Plain weekly is better at steps 1-3, the adjusted arm far better at 4-6: the shape
# the 2026-11-13 experiment measured.
_ERRS = {"chronos2_estate_weekly": {e: (lambda s: 10 if s <= 3 else 100) for e in "ab"},
         "chronos2_weekly_seasadj": {e: (lambda s: 15) for e in "ab"},
         "naive_trailing3": {e: (lambda s: 50) for e in "ab"}}


def test_rule_in_force_reports_the_adjusted_arm_but_never_serves_it():
    out = sel_mod.decide(_stub_reg(_ERRS, {"a": 24, "b": 24}), "staging")
    assert out["rule"] == sel_mod.RULE_ID == "chronos-ctx12-evidence-2026-11-12"
    assert out["served"] == "chronos2_estate_weekly" and out["steps"] == [1, 2, 3]
    assert "chronos2_weekly_seasadj" in out["challengers"]
    assert "chronos2_weekly_seasadj" not in out["candidates"]


def test_rule_in_force_serves_plain_weekly_even_when_the_adjusted_arm_wins_steps_1_to_3():
    errs = dict(_ERRS, chronos2_weekly_seasadj={e: (lambda s: 1) for e in "ab"})
    out = sel_mod.decide(_stub_reg(errs, {"a": 24, "b": 24}), "staging")
    assert out["served"] == "chronos2_estate_weekly"


def test_new_rule_scores_steps_1_to_6_and_can_serve_the_adjusted_arm():
    out = sel_mod.decide(_stub_reg(_ERRS, {"a": 24, "b": 24}), "staging",
                         "chronos-seasadj-h6-2026-11-13")
    assert out["steps"] == [1, 2, 3, 4, 5, 6]
    assert out["served"] == "chronos2_weekly_seasadj"


def test_frozen_2026_11_10_rule_never_considers_the_adjusted_arm():
    assert "chronos2_weekly_seasadj" in old_rule.ADDED_LATER
    errs = dict(_ERRS, chronos2_weekly_seasadj={e: (lambda s: 0) for e in "ab"})
    out = old_rule.decide(_stub_reg(errs, {"a": 24, "b": 24}, max_step=3))
    assert out["selected"] == "chronos2_estate_weekly"
    assert "chronos2_weekly_seasadj" not in out["candidates"]


# ── the monitor ──────────────────────────────────────────────────────────

def _pool(n_months, peak_month=5, amp=0.5):
    h = _history(n_months, peak_month=peak_month, amp=amp)
    return {e: (list(d["period"]), d["value"].to_numpy()) for e, d in h.items()}


def test_seasonal_shape_is_info_before_a_full_year():
    r = monitor.seasonal_shape("staging", "x", _pool(10), _pool(9))
    assert r[0]["level"] == "info" and "10 of 12" in r[0]["detail"]


def test_seasonal_shape_ok_when_unchanged_and_alert_when_the_peak_moves():
    assert monitor.seasonal_shape("staging", "x", _pool(24), _pool(23))[0]["level"] == "ok"
    moved = monitor.seasonal_shape("staging", "x", _pool(24, peak_month=9), _pool(23, peak_month=5))
    assert moved[0]["level"] == "alert" and "was 5" in moved[0]["detail"]


def test_seasonal_shape_alerts_when_the_amplitude_changes_a_lot():
    r = monitor.seasonal_shape("staging", "x", _pool(24, amp=0.25), _pool(23, amp=0.6))
    assert r[0]["level"] == "alert"


def test_pool_months_keeps_complete_months_only():
    m = pd.DataFrame({"month": ["2026-01", "2026-02", "2026-03"], "bunches_total": [1.0, 2.0, 3.0],
                      "is_partial": [0, 0, 1]})
    ps, vs = monitor.pool_months({"x": m})["x"]
    assert [str(p) for p in ps] == ["2026-01", "2026-02"] and list(vs) == [1.0, 2.0]


@pytest.mark.parametrize("cutoff", ["2026-06-30", "2026-07-03"])
def test_adjustment_note_names_the_month_it_was_fitted_through(cutoff):
    h = _history(20)
    adj = sp.adjustment(ESTATES, lambda e: h[e], "x", cutoff)
    assert "2026-06" in adj["note"]
