"""
Seasonal profile -- a statistical Forecast Lab challenger (not served), and the
seasonal index the Chronos arm chronos2_weekly_seasadj adjusts its input with
(adjustment / bin_index below).

Model, per estate e and month m (log monthly bunches):

    log y[e, m] = a_e + b_e * s[month-of-year(m)]

  s    one month-of-year shape shared by the estates of a pooling group
  a_e  the estate's level, b_e its seasonal amplitude (shrunk toward 1 when the
       estate has few months: weight n / (n + SHRINK_K))

Fitted by alternating least squares. Months of the year no estate has observed
yet are interpolated around the year. The forecast level is the mean of the
estate's last LEVEL_MONTHS deseasonalised months, so a level shift since last
season carries forward and the shape supplies the seasonal turn.

Pooling group = same source AND same seasonal region (registry.ESTATES "region").
Live pools with live and staging with staging, never across, so each source
stands on its own data. K3 is its own region: its harvest peaks Sep-Nov, while
EA, EB and EC all peak Apr-Jun (measured 2026-11-12), so sharing one shape
would distort both.

Leakage: at an origin every estate in the group is cut at the origin's calendar
month (period <= origin, complete months only). Nothing after the origin is
seen, in any estate.

Origin of the method: forecast/experiments/staging_accuracy_2026-11-12/
(classical.py, profile_pooled); see review_forecast_mlops.md section 3.2.
"""

import numpy as np
import pandas as pd

SHRINK_K = 6.0
LEVEL_MONTHS = 3
ALS_ITERATIONS = 50


def fit(series: dict):
    """series: {estate: (periods list[Period[M]], values array)}. Returns (a, b, s):
    a, b dicts per estate; s a length-12 array (NaN for unobserved months of the year)."""
    rows = [(e, p.month - 1, float(np.log(v)))
            for e, (ps, vs) in series.items() for p, v in zip(ps, vs) if v > 0]
    o = pd.DataFrame(rows, columns=["e", "moy", "ly"])
    a = o.groupby("e")["ly"].mean().to_dict()
    b = {e: 1.0 for e in a}
    s = np.zeros(12)
    for _ in range(ALS_ITERATIONS):
        o["r"] = o["ly"] - o["e"].map(a)
        o["b"] = o["e"].map(b)
        num = (o["b"] * o["r"]).groupby(o["moy"]).sum()
        den = (o["b"] ** 2).groupby(o["moy"]).sum()
        s = np.full(12, np.nan)
        s[num.index.to_numpy()] = (num / den).to_numpy()
        s = s - np.nanmean(s)
        o["s"] = s[o["moy"].to_numpy()]
        a = (o["ly"] - o["e"].map(b) * o["s"]).groupby(o["e"]).mean().to_dict()
        o["r"] = o["ly"] - o["e"].map(a)
        for e, g in o.groupby("e"):
            sxx = float((g["s"] ** 2).sum())
            bhat = float((g["s"] * g["r"]).sum() / sxx) if sxx > 1e-9 else 1.0
            w = len(g) / (len(g) + SHRINK_K)
            b[e] = w * bhat + (1 - w) * 1.0
    return a, b, s


def fill_circular(s):
    """Linear interpolation of unobserved months around the 12-month circle."""
    s = np.asarray(s, dtype=float).copy()
    idx = np.where(~np.isnan(s))[0]
    if len(idx) == 0:
        return np.zeros(12)
    if len(idx) == 1:
        return np.full(12, s[idx[0]])
    ext_x = np.concatenate([idx - 12, idx, idx + 12])
    ext_y = np.concatenate([s[idx]] * 3)
    return np.interp(np.arange(12), ext_x, ext_y)


def forecast(target: str, series: dict, horizon: int):
    """Point forecasts for the `horizon` months after the target estate's last month.

    series must already be cut at the origin for every estate (the caller's job,
    see group_context). Returns a list of floats."""
    a, b, s = fit(series)
    s_f = fill_circular(s)
    periods, values = series[target]
    seas = s_f[[p.month - 1 for p in periods]]
    level = float(np.mean((np.log(np.asarray(values, float)) - b[target] * seas)[-LEVEL_MONTHS:]))
    last = periods[-1]
    return [float(np.exp(level + b[target] * s_f[(last + k).month - 1])) for k in range(1, horizon + 1)]


def peak_month(s_f):
    """Month of the year (1-12) where a filled shape is highest."""
    return int(np.argmax(np.asarray(s_f, dtype=float))) + 1


# -- seasonal adjustment for a Chronos arm (chronos2_weekly_seasadj) -----------------------
# A zero-shot model that has seen fewer than two yearly cycles extrapolates the recent
# level through every seasonal turn. The adjusted arm divides the series by the index
# exp(b_e * s[month-of-year]) fitted here, lets Chronos forecast what is left, and
# multiplies the index back in. The index is only used once the estate has observed
# every month of the year: before that, part of the shape is interpolation and the
# experiment of 2026-11-13 measured the adjustment doing harm, so the arm forecasts
# the plain series instead and says so.
MIN_MONTHS_OF_YEAR = 12


def last_complete_month(cutoff):
    """The last calendar month that ends on or before `cutoff` (a day)."""
    return (pd.Timestamp(cutoff).normalize() + pd.Timedelta(days=1)).to_period("M") - 1


def adjustment(estates: dict, month_history, target: str, cutoff):
    """The seasonal index to apply for a forecast made at day `cutoff`.

    Returns {"active", "amp", "shape", "months_of_year", "note"}. The fit sees only
    complete months that end on or before the cutoff, in every estate of the pool
    (group_context), so nothing after the cutoff reaches the index. "active" is False
    when the target has observed fewer than MIN_MONTHS_OF_YEAR months of the year."""
    origin = last_complete_month(cutoff)
    ctx = group_context(estates, month_history, target, origin)
    seen = len({p.month for p in ctx[target][0]}) if target in ctx else 0
    if seen < MIN_MONTHS_OF_YEAR:
        return {"active": False, "amp": None, "shape": None, "months_of_year": seen,
                "note": (f"seasonal adjustment off: {seen} of 12 months of the year observed "
                         f"by {origin}, so the plain weekly series is forecast")}
    _, b, s = fit(ctx)
    s_f = fill_circular(s)
    return {"active": True, "amp": float(b[target]), "shape": s_f, "months_of_year": seen,
            "note": (f"seasonally adjusted: index from {len(ctx)} estate(s) through {origin}, "
                     f"amplitude {b[target]:.2f}, peak in month {peak_month(s_f)}")}


def day_index(days, amp, shape):
    """Multiplicative seasonal index per day: constant within a month, so a month's
    index is exactly the profile's."""
    return np.exp(amp * np.asarray(shape, dtype=float)[pd.DatetimeIndex(days).month - 1])


def bin_index(bin_end_days, amp, shape):
    """Mean daily index over each 7-day bin ending on the given days."""
    ends = pd.DatetimeIndex(bin_end_days).normalize()
    if len(ends) == 0:
        return np.zeros(0)
    days = (ends.values[:, None] - np.arange(6, -1, -1)[None, :] * np.timedelta64(1, "D"))
    return day_index(pd.DatetimeIndex(days.ravel()), amp, shape).reshape(len(ends), 7).mean(axis=1)


def group_context(estates: dict, month_history, target: str, origin):
    """{estate: (periods, values)} for target's pooling group, every estate cut at
    `origin` (a Period[M]) and restricted to complete months.

    estates is registry.ESTATES; month_history is registry.month_history."""
    spec = estates[target]
    group = [e for e, sp in estates.items()
             if sp["source"] == spec["source"] and sp.get("region") == spec.get("region")]
    out = {}
    for e in group:
        try:
            h = month_history(e)
        except FileNotFoundError:
            continue
        h = h[h["context_ok"] & (h["period"] <= origin)]
        if len(h):
            out[e] = (list(h["period"]), h["value"].to_numpy(dtype=float))
    return out
