"""Unit tests for the Forecast Lab's backtest plumbing and promotion statistics.

The leakage tests matter most: every lab backtest and forward forecast builds
its input through registry.context_until / block_weekly.weekly_bins, so if
those never reach past the origin, no lab model can see the months it is
scored on.
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_LAB = Path(__file__).parent / "forecast" / "lab"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"test_lab_{name}", _LAB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load("registry")
ev = _load("evaluate")


# ── leakage ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("grain", ["month", "week"])
def test_context_until_stops_at_the_origin(grain):
    h = reg.history("k3", grain)
    for i in reg.backtest_origins("k3", grain)[::7]:
        ctx = reg.context_until("k3", grain, i)
        expected = h.iloc[:i + 1]
        expected = expected.loc[expected["context_ok"], "value"].to_numpy(dtype=float)
        assert np.array_equal(ctx, expected)
        assert ctx[-1] == h["value"].iloc[i]


def test_weekly_bins_ignore_every_day_after_the_cutoff():
    daily = reg.daily_blocks("k3")
    cutoff = pd.Timestamp("2024-06-30")
    poisoned = daily.copy()
    poisoned.loc[poisoned.index > cutoff] = 1e9
    a = reg.bw.weekly_bins(daily, cutoff)
    b = reg.bw.weekly_bins(poisoned, cutoff)
    assert a.index[-1] == cutoff
    pd.testing.assert_frame_equal(a, b)


def test_backtest_rows_target_only_later_periods():
    i = reg.backtest_origins("k3", "month")[0]
    rows = reg.backtest_rows("k3", "month", i, list(range(reg.MAX_H)))
    origin = pd.Period(rows[0]["origin"], freq="M")
    for r in rows:
        assert pd.Period(r["period"], freq="M") == origin + r["step"]


# ── baselines ──────────────────────────────────────────────────────────────

def test_seasonal_naive_backtest_repeats_last_year():
    df, _ = reg.backtest("seasonal_naive", "k3", "month")
    y = reg.month_history("k3").set_index(
        reg.month_history("k3")["period"].astype(str))["value"]
    for r in df.sample(10, random_state=0).itertuples():
        assert r.pred == y[str(pd.Period(r.period, freq="M") - 12)]


def test_served_backtest_is_the_incumbents_walk_forward():
    df, why = reg.backtest("served_ensemble", "k3", "month")
    assert why == "" and set(df["step"]) == set(range(1, 13))
    assert df["scoreable"].all()   # multih already scores only scoreable months


# ── statistics ─────────────────────────────────────────────────────────────

def test_dm_hln_sign_favours_the_lower_loss():
    rng = np.random.default_rng(0)
    ref = rng.uniform(5, 15, 60)
    alt = ref - 2 + rng.normal(0, 0.5, 60)
    r = ev.dm_hln(ref, alt, h=3)
    assert r["DM_hln"] > 0 and r["p"] < 0.05
    assert ev.dm_hln(alt, ref, h=3)["DM_hln"] < 0


def test_dm_hln_needs_four_points():
    assert np.isnan(ev.dm_hln([1, 2, 3], [1, 2, 3])["p"])


def test_paired_joins_on_origin_and_step_only_scoreable():
    ref = pd.DataFrame({"origin": ["a", "a", "b"], "step": [1, 2, 1], "period": ["x", "y", "y"],
                        "actual": [10.0, 10.0, 10.0], "pred": [9.0, 8.0, 7.0],
                        "scoreable": [True, False, True]})
    alt = ref.assign(pred=[10.0, 10.0, 10.0])
    j = ev.paired(ref, alt, (1, 2, 3))
    assert list(zip(j["origin"], j["step"])) == [("a", 1), ("b", 1)]
