"""
Serving layer for the forecasting app (no Streamlit here, so it is unit-testable).

Ties the two production models together:
  * monthly  -> Phase 1 Ensemble (SARIMAX + Trailing3 + LightGBM), reliable (~11% sMAPE)
  * daily/weekly -> TTM foundation model on the harvest-day series, with wide
    p10/p90 bands (single-day production is near-unpredictable, CV ~152%).

All functions return plain DataFrames so the UI just renders them.
"""

import os

import joblib
import numpy as np
import pandas as pd

import monthly_model as mm
import daily_model as dm

# Served monthly model is selected by the backtest and persisted to the joblib
# (best-of base/weather/workdone ensembles). Read its feature set + headline
# metrics from there so the app always serves and reports the chosen model.
_MODEL_PATH = os.path.join(os.path.dirname(__file__), "monthly_model.joblib")
try:
    _META = joblib.load(_MODEL_PATH)
    PROD_FEATS = _META.get("features", mm.BASE_FEATS)
    _M_SMAPE = round(float(_META.get("served_smape", 10.7)), 1)
    _M_MASE = round(float(_META.get("served_mase", 0.61)), 2)
    _M_FOLDS = int(_META.get("n_folds", 30))
except Exception:
    PROD_FEATS, _M_SMAPE, _M_MASE, _M_FOLDS = mm.BASE_FEATS, 10.7, 0.61, 30

# accuracy headlines from the Phase 1/2 backtests (shown in the UI)
ACCURACY = {
    "monthly": {"model": "Ensemble", "smape": _M_SMAPE, "mase": _M_MASE,
                "note": f"Reliable: {_M_FOLDS}-fold walk-forward backtest."},
    "weekly":  {"model": "TTM (foundation)", "smape": 55.0,
                "note": "Indicative: daily noise partly averages out over a week."},
    "daily":   {"model": "TTM (foundation)", "smape": 76.0,
                "note": "Indicative only: single-day production is near-unpredictable "
                        "(CV ~152%); use the band, not the point."},
}


# ── Data ─────────────────────────────────────────────────────────────────────
def load_all():
    est = mm.load_estate()
    wm = mm.build_weather_monthly()
    daily = dm.load_daily()
    return est, wm, daily


def daily_actuals(daily, lookback=60):
    """Recent harvest-day actuals as a tidy frame."""
    s = daily.tail(lookback)
    return pd.DataFrame({"date": s.index, "actual": s.values})


def monthly_actuals(est, lookback=24):
    """Recent monthly actuals (cleaned target) with quality flags for styling."""
    d = est.tail(lookback).copy()
    d["date"] = d["month"].dt.to_timestamp()
    keep = ["date", "bunches_total", "bunches_total_raw", "is_underrecorded",
            "exclude_from_model"]
    return d[keep].rename(columns={"bunches_total": "actual"})


# ── Monthly forecast (Ensemble) ──────────────────────────────────────────────
def monthly_forecast(est, wm, horizon=3):
    # Use the backtest-selected feature set; keep the tree column named
    # "LightGBM" so the UI's fixed column references stay valid regardless of
    # which feature set won. wdmonth supplies lagged workdone effort for the
    # future months when the served model uses those features.
    wd = mm.build_workdone_monthly() if os.path.exists(mm.WORKDONE_FILE) else None
    model = mm.fit_final(est, PROD_FEATS)
    fwd = mm.forecast_next(est, model, PROD_FEATS, wm, horizon=horizon,
                           label="LightGBM", wdmonth=wd)
    fwd["date"] = pd.PeriodIndex(fwd["month"], freq="M").to_timestamp()
    return fwd  # cols: month, SARIMAX, Trailing3, LightGBM, Ensemble, date


# ── Daily / weekly forecast (TTM, with fallback) ─────────────────────────────
def _future_dates(last_date, horizon, step_days):
    return [pd.Timestamp(last_date) + pd.Timedelta(days=step_days * (i + 1))
            for i in range(horizon)]


def _fallback_daily(daily, horizon):
    """Day-of-week seasonal forecast if TTM is unavailable (point only)."""
    df = pd.DataFrame({"date": daily.index, "y": daily.values})
    df["dow"] = df["date"].dt.dayofweek
    recent = df.tail(90)
    dow_factor = recent.groupby("dow")["y"].mean()
    overall = recent["y"].mean()
    step = max(1, int(round(df["date"].diff().dt.days.median())))
    dates = _future_dates(daily.index[-1], horizon, step)
    pts = [float(dow_factor.get(pd.Timestamp(d).dayofweek, overall)) for d in dates]
    spread = recent["y"].std()
    return pd.DataFrame({
        "date": dates, "forecast": pts,
        "p10": np.clip(np.array(pts) - 1.0 * spread, 0, None),
        "p90": np.array(pts) + 1.0 * spread,
        "engine": "DOW-baseline",
    })


def daily_forecast(daily, horizon=7, use_ttm=True):
    step = max(1, int(round(pd.Series(daily.index).diff().dt.days.median())))
    if use_ttm:
        try:
            import ttm_model as ttm
            pt, p10, p90 = ttm.forecast_with_bands(daily.values, horizon=horizon)
            dates = _future_dates(daily.index[-1], horizon, step)
            return pd.DataFrame({"date": dates, "forecast": pt, "p10": p10,
                                 "p90": p90, "engine": "TTM"})
        except Exception as e:
            print("TTM forecast failed, using fallback:", e)
    return _fallback_daily(daily, horizon)


def summarize(mode, fc):
    """Headline numbers for the KPI row."""
    if mode == "monthly":
        nxt = fc.iloc[0]
        return {"label": f"Next month ({nxt['month']})",
                "value": f"{nxt['Ensemble']:,.0f} bunches"}
    if mode == "weekly":
        return {"label": "Next 7 harvest days (total)",
                "value": f"{fc['forecast'].sum():,.0f} bunches"}
    # daily
    return {"label": "Next harvest day",
            "value": f"{fc.iloc[0]['forecast']:,.0f} bunches"}


if __name__ == "__main__":
    est, wm, daily = load_all()
    print("Data: months", len(est), "| harvest days", len(daily),
          "| last day", daily.index[-1].date())
    mf = monthly_forecast(est, wm, 3)
    print("\nMonthly (Ensemble):")
    print(mf[["month", "Ensemble"]].to_string(index=False))
    df = daily_forecast(daily, 7)
    print(f"\nDaily/weekly (engine={df['engine'].iloc[0]}):")
    print(df[["date", "forecast", "p10", "p90"]].round(0).to_string(index=False))
    print("\nKPIs:",
          summarize("daily", df), "|", summarize("weekly", df), "|",
          summarize("monthly", mf))
