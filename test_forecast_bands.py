"""Phase 2 of buildplan_forecast_mlops.md: the band, on both sources.

  * the band (and the headline accuracy) is measured on origins with the active
    rule's context, falling back to every origin when too few qualify;
  * the outer 80% range uses the model's own percentiles only when they held
    70-90% of the backtest actuals on enough rows;
  * the group sum never adds quantiles across estates.
"""
import numpy as np
import pandas as pd
import pytest

import forecast_sources as fs


def _bt(origins, steps=(1, 2, 3), err=0.1, lower=None, upper=None):
    rows = []
    for o in origins:
        for s in steps:
            r = {"origin": o, "step": s, "actual": 100.0, "pred": 100.0 * (1 + err)}
            if lower is not None:
                r["lower"], r["upper"] = lower, upper
            rows.append(r)
    return pd.DataFrame(rows).set_index(["origin", "step"])


def test_band_uses_only_origins_with_the_rules_context():
    origins = [f"2025-{m:02d}" for m in range(6, 13)] + ["2026-01", "2026-02", "2026-03"]
    ctx = {o: i + 6 for i, o in enumerate(origins)}            # 6 .. 15 months
    rows, info = fs._band_rows(_bt(origins), ctx, 12)
    kept = sorted(set(rows.index.get_level_values("origin")))
    assert kept == ["2025-12", "2026-01", "2026-02", "2026-03"]
    assert info["n_origins"] == 4 and not info["low_evidence"]


def test_band_falls_back_to_every_origin_when_too_few_qualify():
    origins = ["2025-11", "2025-12"]
    rows, info = fs._band_rows(_bt(origins), {"2025-11": 6, "2025-12": 7}, 12)
    assert len(rows) == 6 and info["low_evidence"] and "all 2" in info["rule"]


def test_old_rule_files_without_a_context_threshold_keep_every_origin():
    rows, info = fs._band_rows(_bt(["a", "b"]), {"a": 6, "b": 7}, 0)
    assert len(rows) == 6 and info["min_context_months"] is None


def test_step_bands_carry_the_empirical_80_percent_half_width():
    b = fs._step_bands(_bt(["a", "b", "c", "d", "e"], err=0.2), 3)
    assert b[1]["band80_pct"] == pytest.approx(16.7, abs=0.1)   # |100-120| / 120


def test_outer_band_uses_the_models_percentiles_when_they_held_70_to_90_percent():
    origins = [f"o{i}" for i in range(10)]
    bt = _bt(origins, steps=(1,), lower=90.0, upper=110.0)          # actual 100 always inside
    bt.iloc[:2, bt.columns.get_loc("upper")] = 95.0                  # 2 of 10 outside -> 80%
    out = fs._outer_method(bt, [(1, 2)], 1)
    assert out["method"] == "model_q10_q90" and out["coverage"] == pytest.approx(0.8)


def test_outer_band_is_empirical_when_the_models_percentiles_are_too_wide():
    bt = _bt([f"o{i}" for i in range(10)], steps=(1,), lower=50.0, upper=150.0)   # 100% held
    out = fs._outer_method(bt, [(1, 2)], 1)
    assert out["method"] == "empirical_q80" and "100%" in out["note"]


def test_outer_band_is_empirical_without_enough_rows_or_without_an_interval():
    few = _bt(["a", "b", "c"], steps=(1,), lower=90.0, upper=110.0)
    assert fs._outer_method(few, [(1, 2)], 1)["method"] == "empirical_q80"
    assert fs._outer_method(_bt(["a"] * 12, steps=(1,)), [(None, None)], 1)["method"] == "empirical_q80"


def test_group_sum_never_adds_quantiles():
    def part(label, v):
        bt = _bt(["2026-01", "2026-02"], steps=(1,), lower=v - 10, upper=v + 10)
        return {"label": label, "history": [{"month": "2026-02", "value": v, "underrecorded": False}],
                "steps": [("2026-03", float(v))], "bt": bt, "bn": bt[["actual", "pred"]],
                "stale_note": "", "generated_at": None, "last_day": None}
    g = fs._sum_bundles([part("A", 100), part("B", 200)])
    assert list(g["bt"].columns) == ["actual", "pred"]
    assert g["bounds"] == [(None, None)]


@pytest.mark.skipif(not fs.modeled_estates(fs.STAGING), reason="staging artifacts not built")
def test_staging_ec_band_is_measured_on_the_current_regime():
    d = fs.payload("ec", 3, source=fs.STAGING)
    if d["selection"]["rule"].startswith("chronos-ctx12"):
        assert [f["band_pct"] for f in d["forecast"]] == pytest.approx([14.5, 22.9, 28.5], abs=0.2)
        assert d["interval"]["measured_on"]["min_context_months"] == 12
    assert all(f["lower80"] <= f["lower"] and f["upper80"] >= f["upper"] or f["band80_pct"] < f["band_pct"]
               for f in d["forecast"])
