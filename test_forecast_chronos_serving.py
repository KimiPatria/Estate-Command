"""Phase 1 of buildplan_forecast_mlops.md: Chronos-2 served on both sources.

  * the per-source rule can only serve a Chronos-2 arm, flags (never replaces)
    an estate where it loses, scores mature origins only and keeps low-evidence
    estates out of the score;
  * K3 falls back to the ensemble, labelled, when its Chronos forecast is
    missing or stale;
  * the seasonal-profile challenger never sees past the origin and never pools
    across sources;
  * the combination is the weighted mean of its members on their shared rows.

The rule tests run on a stub registry (no company data). The rest read the
built artifacts and skip when they are absent.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

_LAB = Path(__file__).parent / "forecast" / "lab"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"test_serving_{name}", _LAB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sel_mod = _load("select_source")
sp = _load("seasonal_profile")


# ── the rule, on a stub registry ──────────────────────────────────────────

def _stub_reg(errors: dict, months: dict):
    """errors[model][estate] = absolute error at every row; months[estate] = history length.
    Every estate starts 2025-01; every origin with >= 6 months of context is a backtest origin."""
    periods = {e: pd.period_range("2025-01", periods=n, freq="M") for e, n in months.items()}

    def month_history(e):
        return pd.DataFrame({"period": periods[e], "value": 100.0,
                             "context_ok": True, "scoreable": True, "underrecorded": False})

    def backtest(model, e, grain):
        if e not in errors.get(model, {}):
            return None, "not built"
        rows = []
        ps = periods[e]
        for i in range(5, len(ps) - 1):
            for step in (1, 2, 3):
                if i + step < len(ps):
                    rows.append({"origin": str(ps[i]), "step": step, "period": str(ps[i + step]),
                                 "actual": 100.0, "pred": 100.0 + errors[model][e],
                                 "scoreable": True})
        return pd.DataFrame(rows), ""

    return SimpleNamespace(
        ESTATES={e: {"label": e.upper(), "source": "staging"} for e in months},
        estates_of=lambda source: list(months),
        estate_grain_status=lambda e, g: (True, "", months[e]),
        month_history=month_history, backtest=backtest,
        period_str=lambda p, g: str(p))


def test_rule_serves_only_a_chronos_arm_even_when_a_challenger_is_better():
    errs = {"chronos2_estate_weekly": {"a": 20, "b": 20}, "chronos2_monthly": {"a": 30, "b": 30},
            "seasonal_profile": {"a": 1, "b": 1}, "combo_weekly_profile": {"a": 1, "b": 1},
            "naive_trailing3": {"a": 50, "b": 50}}
    out = sel_mod.decide(_stub_reg(errs, {"a": 20, "b": 20}), "staging")
    assert out["served"] == "chronos2_estate_weekly"
    assert out["served"] in sel_mod.SERVED_CANDIDATES
    assert set(out["challengers"]) == {"seasonal_profile", "combo_weekly_profile"}


def test_rule_flags_but_does_not_replace_an_estate_where_chronos_loses():
    errs = {"chronos2_estate_weekly": {"a": 10, "b": 80}, "naive_trailing3": {"a": 50, "b": 50}}
    out = sel_mod.decide(_stub_reg(errs, {"a": 20, "b": 20}), "staging")
    assert out["served"] == "chronos2_estate_weekly"
    assert {"estate": "b", "kind": "loses_to_yardstick"}.items() <= next(
        f for f in out["flags"] if f["estate"] == "b").items()


def test_rule_scores_only_origins_with_a_year_of_context():
    errs = {"chronos2_estate_weekly": {"a": 10}, "naive_trailing3": {"a": 50}}
    out = sel_mod.decide(_stub_reg(errs, {"a": 20}), "staging")
    origins = out["per_estate"]["a"]["origins"]
    assert origins and min(origins) == "2025-12"        # the 12th complete month
    assert not out["per_estate"]["a"]["low_evidence"]


def test_low_evidence_estate_is_served_and_flagged_but_left_out_of_the_score():
    # 'short' has 9 months: no origin with 12 months of context, so low evidence.
    # Monthly wins on 'long'; block sum only wins because of 'short'.
    errs = {"chronos2_monthly": {"long": 10, "short": 90}, "chronos2_blocks": {"long": 30, "short": 5},
            "naive_trailing3": {"long": 50, "short": 50}}
    reg = _stub_reg(errs, {"long": 24, "short": 9})
    in_force = sel_mod.decide(reg, "staging")
    assert in_force["rule"] == "chronos-ctx12-evidence-2026-11-12"
    assert in_force["served"] == "chronos2_monthly" and in_force["scored_estates"] == ["long"]
    assert any(f["estate"] == "short" and f["kind"] == "low_evidence" for f in in_force["flags"])
    first = sel_mod.decide(reg, "staging", "chronos-ctx12-2026-11-12")
    assert first["served"] == "chronos2_blocks"         # what the superseded rule did


def test_rule_excludes_k3_which_a_manual_promotion_governs():
    assert "k3" in sel_mod.EXCLUDED
    promo = _load("promotion")
    model, entry = promo.served_model("k3")
    assert model == "chronos2_monthly_cov" and entry["fallback"] == "served_ensemble"


# ── seasonal profile ─────────────────────────────────────────────────────

def _profile_estates():
    return {"x": {"source": "staging", "region": "r"}, "y": {"source": "staging", "region": "r"},
            "z": {"source": "live", "region": "r"}, "w": {"source": "staging", "region": "other"}}


def _profile_history(n_months=20, seed=0):
    rng = np.random.default_rng(seed)
    ps = pd.period_range("2025-01", periods=n_months, freq="M")
    shape = 1 + 0.4 * np.sin(2 * np.pi * (ps.month - 3) / 12)
    out = {}
    for k, e in enumerate(["x", "y", "z", "w"]):
        vals = 1000 * (k + 1) * shape * rng.uniform(0.9, 1.1, n_months)
        out[e] = pd.DataFrame({"period": ps, "value": vals, "context_ok": True})
    return out


def test_profile_never_sees_past_the_origin_in_any_estate():
    full = _profile_history(20)
    cut_at = pd.Period("2025-12", "M")
    short = {e: h[h["period"] <= cut_at] for e, h in full.items()}
    a = sp.forecast("x", sp.group_context(_profile_estates(), lambda e: full[e], "x", cut_at), 6)
    b = sp.forecast("x", sp.group_context(_profile_estates(), lambda e: short[e], "x", cut_at), 6)
    assert np.allclose(a, b)


def test_profile_pools_only_within_one_source_and_region():
    full = _profile_history(20)
    ctx = sp.group_context(_profile_estates(), lambda e: full[e], "x", pd.Period("2026-06", "M"))
    assert set(ctx) == {"x", "y"}                       # z is live, w another region


# ── derived model and K3 fallback, on the built artifacts ────────────────

reg = _load("registry")


def _built(model, estate):
    try:
        df, _ = reg.backtest(model, estate, "month")
    except (FileNotFoundError, KeyError, ValueError):      # no data (CI, a fresh clone)
        return False
    return df is not None and not df.empty


needs_artifacts = pytest.mark.skipif(
    not all(_built(m, "stg_ec") for m in ("chronos2_estate_weekly", "seasonal_profile")),
    reason="staging artifacts not built")


@needs_artifacts
def test_combination_is_the_weighted_mean_of_its_members_on_shared_rows():
    combo, _ = reg.backtest("combo_weekly_profile", "stg_ec", "month")
    a, _ = reg.backtest("chronos2_estate_weekly", "stg_ec", "month")
    b, _ = reg.backtest("seasonal_profile", "stg_ec", "month")
    j = a.merge(b, on=["origin", "step"], suffixes=("_a", "_b"))
    assert len(combo) == len(j)
    m = combo.merge(j, on=["origin", "step"])
    assert np.allclose(m["pred"], 0.5 * m["pred_a"] + 0.5 * m["pred_b"])


@needs_artifacts
def test_k3_falls_back_to_the_ensemble_with_a_label(monkeypatch):
    import forecast_router as fr
    import forecast_sources as fs

    def missing(*a, **k):
        raise fs.Unavailable("artifact missing")

    monkeypatch.setattr(fs, "payload", missing)
    monkeypatch.setattr(fr, "get_forecast", lambda **k: {"model_label": "Ensemble", "forecast": []})
    d = fr.served_forecast("k3")
    assert d["fallback"]["model"] == "served_ensemble" and d["model_label"].endswith("(fallback)")
    with pytest.raises(fs.Unavailable):                 # no fallback for other estates
        fr.served_forecast("ec")


@needs_artifacts
def test_k3_falls_back_when_its_chronos_forecast_is_stale(monkeypatch):
    import forecast_router as fr
    import forecast_sources as fs
    monkeypatch.setattr(fs, "payload", lambda *a, **k: {"stale": True, "stale_note": "old anchor"})
    monkeypatch.setattr(fr, "get_forecast", lambda **k: {"model_label": "Ensemble", "forecast": []})
    assert fr.served_forecast("k3")["fallback"]["reason"] == "old anchor"


@needs_artifacts
def test_old_staging_rule_still_selects_estate_weekly():
    old = _load("select_staging")
    assert old.decide(reg)["selected"] == "chronos2_estate_weekly"
