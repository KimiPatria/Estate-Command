"""
Monitoring for the forecast pipeline, both sources (buildplan_forecast_mlops.md Phase 7.2).

Run by forecast/refresh.py inside every refresh (its health step), written to the
run's health.json, and shown on /model-health and the Compare sources view.
Distribution tests on features (KS, PSI) are not used: the only input is the
target series, and the realised-error checks cover what drift would.

Each check returns rows {"source", "estate", "check", "level", "detail"}, level
one of "alert" (someone should look), "ok", or "info" (cannot be judged yet).

  realised_error   from the shadow log, on the served model's resolved step-1 months:
                   the actual fell outside the drawn band 2 months running, or the
                   rolling 3-month error exceeds 1.5x the backtest figure
  chronos_vs_naive the served model's rolling 3-month step-1 MAE exceeds naive last-3's (D10)
  freshness        the source's data ends more than FRESH_DAYS before today
  volume           the newest full month moved more than VOLUME_YOY against the same
                   month a year earlier, or more than VOLUME_MOM against the month before
  column_drift     a declared column changed status since the previous run
  seasonal_shape   the month-of-year shape seasonal_profile fits on the pool (same
                   source and region) moved since the previous run: its peak by
                   PEAK_SHIFT_MONTHS or more, or the estate's log peak-to-trough
                   swing by more than AMP_CHANGE. chronos2_weekly_seasadj divides by that shape, so a
                   moved shape is a moved forecast. Judged only once the estate has
                   seen all 12 months of the year in both runs.
The drawn band used for realised_error is the band as measured now, applied to the
logged forecasts (the shadow log keeps the model's own interval, not the drawn band).
"""

import importlib.util
import os
from datetime import date, datetime

import pandas as pd

FRESH_DAYS = 40
VOLUME_YOY = 0.40
VOLUME_MOM = 0.60
ERROR_FACTOR = 1.5
MONTHS = 3
# One more month of data can move a fitted peak by one month; two is a real change.
PEAK_SHIFT_MONTHS = 2
AMP_CHANGE = 0.30

_sp = None


def _seasonal_profile():
    global _sp
    if _sp is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lab", "seasonal_profile.py")
        spec = importlib.util.spec_from_file_location("epms_monitor_seasonal_profile", path)
        _sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sp)
    return _sp


def row(source, estate, check, level, detail):
    return {"source": source, "estate": estate, "check": check, "level": level, "detail": detail}


def freshness(source, estate, last_day, today=None):
    if not last_day:
        return row(source, estate, "freshness", "info", "no data-through date in the extract")
    today = today or date.today()
    age = (today - datetime.strptime(last_day[:10], "%Y-%m-%d").date()).days
    level = "alert" if age > FRESH_DAYS else "ok"
    return row(source, estate, "freshness", level, f"data through {last_day}, {age} days ago"
               + (f" (more than {FRESH_DAYS})" if level == "alert" else ""))


def volume(source, estate, monthly: pd.DataFrame):
    """The newest full month against the same month a year earlier and the month before."""
    if monthly is None or monthly.empty:
        return [row(source, estate, "volume", "info", "no monthly file")]
    full = monthly[monthly["is_partial"] == 0].set_index("month")["bunches_total"].astype(float)
    if len(full) < 2:
        return [row(source, estate, "volume", "info", "fewer than 2 full months")]
    m = full.index[-1]
    p = pd.Period(m, "M")
    out = []
    prev = str(p - 1)
    if prev in full and full[prev]:
        ch = full[m] / full[prev] - 1
        out.append(row(source, estate, "volume", "alert" if abs(ch) > VOLUME_MOM else "ok",
                       f"{m} {ch:+.0%} against {prev}"))
    ly = str(p - 12)
    if ly in full and full[ly]:
        ch = full[m] / full[ly] - 1
        out.append(row(source, estate, "volume", "alert" if abs(ch) > VOLUME_YOY else "ok",
                       f"{m} {ch:+.0%} against {ly}"))
    return out or [row(source, estate, "volume", "info", f"{m}: no earlier month to compare")]


def column_drift(source, estate, now: dict, before: dict):
    if not before:
        return [row(source, estate, "column_drift", "info", "no earlier run to compare")]
    changed = [f"{c} {before[c]['status']} -> {v['status']}" for c, v in now.items()
               if c in before and before[c]["status"] != v["status"]]
    gone = [c for c in before if c not in now]
    if changed or gone:
        return [row(source, estate, "column_drift", "alert",
                    "; ".join(changed + [f"{c} no longer declared" for c in gone]))]
    return [row(source, estate, "column_drift", "ok", f"{len(now)} columns, statuses unchanged")]


def pool_months(monthly_by_estate: dict) -> dict:
    """{estate: monthly.csv frame} -> {estate: (periods, values)}, complete months only:
    the input seasonal_profile.fit takes."""
    out = {}
    for e, m in monthly_by_estate.items():
        if m is None or m.empty:
            continue
        full = m[m["is_partial"] == 0]
        if len(full):
            out[e] = (list(pd.PeriodIndex(full["month"].astype(str), freq="M")),
                      full["bunches_total"].astype(float).to_numpy())
    return out


def _shape(pool: dict, estate: str):
    """(peak month, seasonal swing, months of the year seen) for the estate, or None.

    The swing is the estate's own peak-to-trough ratio, exp(b_e * (max s - min s)):
    b_e alone is relative to the pooled shape, so a change every estate shares would
    move s and leave b_e near 1."""
    import numpy as np
    sp = _seasonal_profile()
    if estate not in pool:
        return None
    seen = len({p.month for p in pool[estate][0]})
    if seen < sp.MIN_MONTHS_OF_YEAR:
        return None, None, seen
    _, b, s = sp.fit(pool)
    s_f = sp.fill_circular(s)
    return sp.peak_month(s_f), float(np.exp(b[estate] * (s_f.max() - s_f.min()))), seen


def seasonal_shape(source, estate, pool_now: dict, pool_before: dict):
    """pool_*: pool_months() of the estate's pooling group in this run and the previous."""
    now = _shape(pool_now, estate)
    if now is None or now[0] is None:
        seen = 0 if now is None else now[2]
        return [row(source, estate, "seasonal_shape", "info",
                    f"season not identified yet ({seen} of 12 months of the year observed)")]
    import math
    peak, swing, _ = now
    before = _shape(pool_before or {}, estate)
    if before is None or before[0] is None:
        return [row(source, estate, "seasonal_shape", "info",
                    f"peak in month {peak}, swing {swing:.1f}x; no earlier identified shape to compare")]
    peak0, swing0, _ = before
    shift = min((peak - peak0) % 12, (peak0 - peak) % 12)
    # change of the log swing: 1.5x -> 2.0x is +71%, 4.0x -> 4.4x is +7%
    change = math.log(swing) / math.log(swing0) - 1 if swing0 > 1 else 0.0
    bad = shift >= PEAK_SHIFT_MONTHS or abs(change) > AMP_CHANGE
    return [row(source, estate, "seasonal_shape", "alert" if bad else "ok",
                f"peak in month {peak} (was {peak0}), swing {swing:.1f}x (was {swing0:.1f}x, "
                f"{change:+.0%} in log terms)")]


def realised_error(source, estate, resolved: pd.DataFrame, model: str, bands: dict, backtest_step1: float):
    """resolved: shadow rows with actual (estate, this model and naive), step 1 only."""
    r = resolved[(resolved["model"] == model) & (resolved["step"] == 1)].sort_values("period")
    if r.empty:
        return [row(source, estate, "realised_error", "info", "no resolved forecast month yet")]
    out = []
    band = bands.get(1)
    if band:
        outside = ((r["actual"] - r["pred"]).abs() > r["pred"].abs() * band / 100).tolist()
        run2 = len(outside) >= 2 and outside[-1] and outside[-2]
        out.append(row(source, estate, "realised_error", "alert" if run2 else "ok",
                       f"{sum(outside)} of {len(outside)} resolved months outside the +/-{band:.0f}% band"
                       + ("; the last two running" if run2 else "")))
    last = r.tail(MONTHS)
    if len(last) == MONTHS and backtest_step1:
        d = last["actual"].abs() + last["pred"].abs()
        smape = float((2 * (last["pred"] - last["actual"]).abs() / d).mean() * 100)
        bad = smape > ERROR_FACTOR * backtest_step1
        out.append(row(source, estate, "realised_error", "alert" if bad else "ok",
                       f"last {MONTHS} months {smape:.1f}% against {backtest_step1:.1f}% in the backtest"))
    return out


def chronos_vs_naive(source, estate, resolved: pd.DataFrame, model: str, naive="naive_trailing3"):
    a = resolved[(resolved["model"] == model) & (resolved["step"] == 1)].set_index("period")
    b = resolved[(resolved["model"] == naive) & (resolved["step"] == 1)].set_index("period")
    common = a.index.intersection(b.index)
    if len(common) < MONTHS:
        return [row(source, estate, "chronos_vs_naive", "info",
                    f"{len(common)} resolved month(s) with both; {MONTHS} needed")]
    last = sorted(common)[-MONTHS:]
    mae_m = float((a.loc[last, "pred"] - a.loc[last, "actual"]).abs().mean())
    mae_n = float((b.loc[last, "pred"] - b.loc[last, "actual"]).abs().mean())
    return [row(source, estate, "chronos_vs_naive", "alert" if mae_m > mae_n else "ok",
                f"last {MONTHS} months MAE {mae_m:,.0f} against naive {mae_n:,.0f}")]
