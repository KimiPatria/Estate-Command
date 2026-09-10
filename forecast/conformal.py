"""
Conformal calibration for the monthly FFB ensemble forecast (Stage-1 R&D).

Replaces the heuristic +/-sMAPE band with distribution-free prediction
intervals calibrated on multi-step walk-forward residuals (see
monthly_model.backtest_multih). Two methods, both benchmarked prequentially:

  * split — per-step split-conformal on absolute residuals: the interval
    half-width at step h is the finite-sample conformal quantile
    (the ceil((n+1)(1-alpha))-th order statistic) of all walk-forward
    |residuals| at that step.
  * aci   — Adaptive Conformal Inference (Gibbs & Candes, 2021): the working
    miscoverage alpha_t is updated online, alpha_{t+1} = alpha_t +
    gamma * (alpha - err_t), so the band inflates after misses and tightens
    after runs of hits. Robust to distribution shift; noisier on ~30 points.

Honest evaluation: prequential (online) coverage — the interval for fold t is
built from residuals of folds strictly before t, then scored against fold t.
That is the number to compare against the nominal level. NOTE on power: with
~20 scoreable folds the standard error of a 90% coverage estimate is ~7pp, so
anything in roughly [0.80, 1.00] is statistically consistent with nominal;
below 0.80 is the "investigate calibration" trigger.

A third quantity, step_smape(), is measured (not fit/assumed) on the same
resid frame: the plain sMAPE of actual-vs-predicted at each step of the
walk-forward backtest. This is what forecast_router currently serves as the
client-facing band — "the band widens with distance" is a measured curve,
not a formula. split/aci remain the statistically rigorous alternative and
still back the Investigator's band-breach trigger.

Serving contract: calibrate() returns a plain-dict bundle that is persisted
into monthly_model.joblib; forecast_router only reads served_widths /
served_method / prequential / step_smape / step_smape_n from it (no imports
from this module at serve time).
"""

import numpy as np
import pandas as pd

ALPHA = 0.10       # nominal miscoverage -> 90% prediction intervals
ACI_GAMMA = 0.08   # ACI learning rate
MIN_CALIB = 8      # residuals required before an interval is scored/served
_ALPHA_CLIP = (0.01, 0.5)  # keep the ACI working alpha in a sane range


def conformal_quantile(scores, alpha):
    """Finite-sample-valid conformal quantile of nonconformity scores.

    Returns the ceil((n+1)(1-alpha))-th order statistic, capped at the max
    (which makes small-n intervals conservative rather than invalid).
    """
    s = np.sort(np.asarray(scores, dtype=float))
    n = s.size
    if n == 0:
        return float("nan")
    k = int(np.ceil((n + 1) * (1 - alpha)))
    return float(s[min(max(k, 1), n) - 1])


def _monotone(widths):
    """Force half-widths to be non-decreasing in the forecast step."""
    prev = 0.0
    out = {}
    for h in sorted(widths):
        prev = max(prev, float(widths[h]))
        out[h] = round(prev, 1)
    return out


def split_widths(resid, alpha=ALPHA, max_step=None):
    """Per-step interval half-widths from ALL walk-forward residuals.

    Steps with fewer than MIN_CALIB residuals (long horizons) are filled by a
    linear fit of width vs step over the calibrated steps, floored at the
    widest calibrated width so extrapolation never narrows the band.
    """
    max_step = int(max_step or resid["step"].max())
    calib = {int(h): conformal_quantile(g["abs_err"], alpha)
             for h, g in resid.groupby("step") if len(g) >= MIN_CALIB}
    if not calib:
        return {}
    hs = np.array(sorted(calib), dtype=float)
    ws = np.array([calib[int(h)] for h in hs])
    slope, intercept = (np.polyfit(hs, ws, 1) if len(hs) >= 2 else (0.0, ws[0]))
    out = {}
    for h in range(1, max_step + 1):
        out[h] = calib.get(h, max(float(intercept + slope * h), float(ws.max())))
    return _monotone(out)


def _smape(actual, pred):
    a = np.asarray(actual, dtype=float)
    p = np.asarray(pred, dtype=float)
    d = np.abs(a) + np.abs(p)
    return float(np.mean(np.where(d == 0, 0.0, 2 * np.abs(p - a) / d)) * 100)


def step_smape(resid, max_step=None, min_calib=MIN_CALIB):
    """Per-step sMAPE measured directly on the multi-horizon walk-forward
    backtest (the same resid frame used for conformal calibration) — the
    actual observed error-growth curve, not an assumed formula.

    Steps with fewer than min_calib scored folds are not independently
    estimated (too noisy to trust on their own); they're floored at the
    widest calibrated step rather than extrapolating a shape. A monotone
    floor is applied throughout: on ~20-30 folds per step, a step-to-step dip
    is sampling noise, not evidence the model got more confident further out.

    Returns (widths, n_scored): widths is {step: sMAPE%}, n_scored is
    {step: fold count} so callers can show how much data backs each number.
    """
    max_step = int(max_step or resid["step"].max())
    calib, n_scored = {}, {}
    for h, g in resid.groupby("step"):
        h = int(h)
        n_scored[h] = int(len(g))
        if len(g) >= min_calib:
            calib[h] = _smape(g["actual"], g["pred"])
    if not calib:
        return {}, n_scored
    widest = max(calib.values())
    out = {h: calib.get(h, widest) for h in range(1, max_step + 1)}
    return _monotone(out), n_scored


def prequential(resid, alpha=ALPHA, gamma=ACI_GAMMA, min_calib=MIN_CALIB):
    """Online (honest) evaluation of both methods, per forecast step.

    Folds are processed in origin order; fold t is scored with an interval
    built only from folds < t. Returns per-step dicts with coverage, average
    half-width and the final adapted ACI alpha (the state to serve with).
    """
    out = {}
    for h, g in resid.groupby("step"):
        scores = g.sort_values("origin")["abs_err"].to_numpy()
        split_hits, split_w, aci_hits, aci_w = [], [], [], []
        alpha_t = alpha
        for t in range(len(scores)):
            if t < min_calib:
                continue
            past = scores[:t]
            q_split = conformal_quantile(past, alpha)
            split_hits.append(scores[t] <= q_split)
            split_w.append(q_split)
            q_aci = conformal_quantile(past, float(np.clip(alpha_t, *_ALPHA_CLIP)))
            hit = scores[t] <= q_aci
            aci_hits.append(hit)
            aci_w.append(q_aci)
            alpha_t += gamma * (alpha - (0.0 if hit else 1.0))
        out[int(h)] = {
            "n_scored": len(split_hits),
            "split_coverage": round(float(np.mean(split_hits)), 3) if split_hits else None,
            "aci_coverage": round(float(np.mean(aci_hits)), 3) if aci_hits else None,
            "split_avg_width": round(float(np.mean(split_w)), 1) if split_w else None,
            "aci_avg_width": round(float(np.mean(aci_w)), 1) if aci_w else None,
            "aci_alpha_final": round(float(np.clip(alpha_t, *_ALPHA_CLIP)), 4),
        }
    return out


def _coverage_gap(preq, key, alpha, steps=(1, 2, 3)):
    """Count-weighted |empirical - nominal| coverage over the headline steps."""
    num = den = 0.0
    for h in steps:
        st = preq.get(h) or {}
        if st.get(key) is not None and st.get("n_scored"):
            num += abs(st[key] - (1.0 - alpha)) * st["n_scored"]
            den += st["n_scored"]
    return (num / den) if den else float("inf")


def weighted_coverage(preq, key, steps=(1, 2, 3)):
    """Count-weighted empirical coverage over the given steps (or None)."""
    num = den = 0.0
    for h in steps:
        st = preq.get(h) or {}
        if st.get(key) is not None and st.get("n_scored"):
            num += st[key] * st["n_scored"]
            den += st["n_scored"]
    return round(num / den, 3) if den else None


def calibrate(resid, alpha=ALPHA, gamma=ACI_GAMMA, max_step=None):
    """Full Stage-1 calibration bundle to persist in monthly_model.joblib.

    resid: long DataFrame with columns origin, step, month, actual, pred
    (one row per scoreable walk-forward forecast). The served method is the
    one whose prequential coverage over steps 1-3 is closest to nominal.
    """
    resid = resid.copy()
    resid["abs_err"] = (resid["pred"] - resid["actual"]).abs()

    preq = prequential(resid, alpha=alpha, gamma=gamma)
    w_split = split_widths(resid, alpha=alpha, max_step=max_step)
    w_smape, n_smape = step_smape(resid, max_step=max_step)

    # ACI serving widths: quantile of ALL residuals at the final adapted alpha.
    w_aci = {}
    for h, w in w_split.items():
        st = preq.get(h) or {}
        a_h = st.get("aci_alpha_final", alpha) if st.get("n_scored") else alpha
        g = resid.loc[resid["step"] == h, "abs_err"]
        w_aci[h] = conformal_quantile(g, float(np.clip(a_h, *_ALPHA_CLIP))) \
            if len(g) >= MIN_CALIB else w
    w_aci = _monotone(w_aci)

    served = "split" if (_coverage_gap(preq, "split_coverage", alpha)
                         <= _coverage_gap(preq, "aci_coverage", alpha)) else "aci"
    cov_13 = weighted_coverage(preq, f"{served}_coverage")

    return {
        "alpha": alpha,
        "gamma": gamma,
        "nominal": round(1.0 - alpha, 2),
        "served_method": f"conformal_{served}",
        "served_widths": (w_split if served == "split" else w_aci),
        "widths_split": w_split,
        "widths_aci": w_aci,
        "step_smape": w_smape,
        "step_smape_n": n_smape,
        "prequential": preq,
        "coverage_h1_3": cov_13,
        "coverage_ok": (cov_13 is None) or (cov_13 >= 0.80),
        "n_residuals": {int(h): int(n) for h, n in resid.groupby("step").size().items()},
    }
