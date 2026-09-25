"""
Forecast comparison statistics shared by the Forecast Lab and the promotion rule.

dm_hln and ape are the functions forecast/experiments/blend/run_blend_analysis.py
used for the 2026-09 blend analysis, copied unchanged so the lab and the
promotion gate apply the exact same test (that script lives in a gitignored
experiments folder, so tracked code cannot import it).
"""

import numpy as np
import pandas as pd
from scipy import stats


def ape(a, p):
    """Absolute percentage error, the DM loss function."""
    a, p = np.asarray(a, float), np.asarray(p, float)
    return np.abs(p - a) / np.abs(a) * 100.0


def smape(a, p):
    a, p = np.asarray(a, float), np.asarray(p, float)
    d = np.abs(a) + np.abs(p)
    return np.where(d == 0, 0.0, 200 * np.abs(p - a) / d)


def dm_hln(loss_ref, loss_alt, h=3):
    """Diebold-Mariano with the Harvey-Leybourne-Newbold small-sample
    correction. d_t = loss_ref - loss_alt, so a POSITIVE statistic favours
    `alt`. Long-run variance uses autocovariances to lag h-1 (h = forecast
    horizon), then the HLN factor sqrt((n+1-2h+h(h-1)/n)/n) and a t_{n-1}
    reference distribution."""
    d = np.asarray(loss_ref, float) - np.asarray(loss_alt, float)
    n = d.size
    if n < 4:
        return dict(n=n, dbar=np.nan, DM=np.nan, DM_hln=np.nan, p=np.nan)
    dbar = d.mean()
    dc = d - dbar
    gamma0 = float(np.sum(dc * dc) / n)
    acov = [float(np.sum(dc[k:] * dc[:-k]) / n) for k in range(1, min(h, n))]
    V = (gamma0 + 2.0 * sum(acov)) / n
    if V <= 0:
        return dict(n=n, dbar=float(dbar), DM=np.nan, DM_hln=np.nan, p=np.nan)
    dm = dbar / np.sqrt(V)
    corr = np.sqrt(max((n + 1 - 2 * h + h * (h - 1) / n) / n, 1e-12))
    dm_h = dm * corr
    p = float(2 * (1 - stats.t.cdf(abs(dm_h), df=n - 1)))
    return dict(n=n, dbar=float(dbar), DM=float(dm), DM_hln=float(dm_h), p=p)


def paired(ref, alt, steps):
    """Join two backtests on (origin, step) over `steps`, scoreable rows only.

    Returns one frame with actual, pred_ref, pred_alt sorted by origin then
    step -- the row order dm_hln's autocovariance assumes.
    """
    cols = ["origin", "step", "period", "actual", "pred"]
    a = ref[ref["scoreable"] & ref["step"].isin(steps)][cols]
    b = alt[alt["scoreable"] & alt["step"].isin(steps)][["origin", "step", "pred"]]
    j = a.merge(b, on=["origin", "step"], suffixes=("_ref", "_alt"))
    return j.sort_values(["origin", "step"]).reset_index(drop=True)


def by_step(j, which):
    """MAE and MAPE per step for one side ("ref" or "alt") of a paired frame."""
    e = (j[f"pred_{which}"] - j["actual"]).abs()
    return (pd.DataFrame({"step": j["step"], "ae": e, "ape": ape(j["actual"], j[f"pred_{which}"])})
            .groupby("step").agg(mae=("ae", "mean"), mape=("ape", "mean"), n=("ae", "size")))
