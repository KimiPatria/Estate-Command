"""
Forecast Lab registry -- the estates, grains and models the /experiment/forecast
page can put side by side.

The lab is an experiment surface, deliberately separate from the client-facing
/forecast page: it reads the served model through forecast_router exactly as
that page does, and everything else from its own artifacts. Nothing here writes
to the served model or its caches' contents.

This module is imported by both sides of the lab, so it needs pandas/numpy only:
  * experiment_router.py (dashboard env) -- history + live models + artifacts
  * forecast/lab/build_artifacts.py (chronos-rd-py313 env) -- writes artifacts

Grains
------
month  calendar months. A month enters history once its recording window covers
       at least COMPLETE_SHARE of its days; a partial trailing month is left
       out (as on /forecast), a partial leading month is shown but flagged.
       k3 uses its cleaned monthly target (features_estate_monthly.csv), so
       its history is exactly the /forecast page's.
week   7-day bins ending on the estate's last recorded day (block_weekly.py),
       raw recorded harvest.

Adding a model
--------------
1. Append a spec to MODELS: a fixed id, a label, a fixed colour (colour follows
   the model, never its position in a selection), the grains it produces and
   its source: "live" (computed per request by experiment_router.py -- cheap
   things only) or "artifact" (written by build_artifacts.py, read here).
2. For "artifact", add a producer to build_artifacts.PRODUCERS writing the
   ARTIFACT schema below; for "live", add a branch to experiment_router._live.
Palette slots left for new models: see FREE_COLOURS (validated all-pairs with
the ones in use; beyond them give the new line a distinct dash as well).

Artifact schema (forecast/experiments/lab/<model>/<estate>_<grain>.json)
------------------------------------------------------------------------
{"model", "estate", "grain", "generated_at", "data_through",
 "anchor": {"period", "value"}, "context_n", "notes": [str],
 "band": {"label"} | null,
 "steps": [{"period", "value", "lower", "upper"}]}   # MAX_H steps
"""

import importlib.util
import json
import os
from functools import lru_cache

import numpy as np
import pandas as pd

_LAB = os.path.dirname(os.path.abspath(__file__))
_FORECAST = os.path.dirname(_LAB)
ARTIFACT_DIR = os.path.join(_FORECAST, "experiments", "lab")

MAX_H = 12
COMPLETE_SHARE = 0.9
MIN_CONTEXT = {"month": 3, "week": 6}
# A backtest origin needs this much history before it (k3's served backtest
# starts at its 6th usable month, monthly_model.MIN_TRAIN).
MIN_BACKTEST_CONTEXT = {"month": 6, "week": 8}
SEASON = {"month": 12, "week": 52}
GRAIN_LABEL = {"month": "Monthly", "week": "Weekly"}

# id -> where its data lives. `served` is forecast_router's estate id when the
# estate has a served model; `weather` is its NASA POWER history, if any.
ESTATES = {
    "k3": {"label": "K3", "blocks": "k3", "served": "k3",
           "features": os.path.join(_FORECAST, "features_estate_monthly.csv"),
           "weather": os.path.join(_FORECAST, "weather_nasa_power_history.csv")},
    "ec": {"label": "EC", "blocks": "EC", "served": "ec", "features": None,
           "weather": os.path.join(_FORECAST, "EC", "weather_nasa_power_history.csv")},
    "eb": {"label": "EB", "blocks": "EB", "served": None, "features": None, "weather": None},
    "ea": {"label": "EA", "blocks": "EA", "served": None, "features": None, "weather": None},
}

# Colours: the served ensemble keeps /forecast's gold; the Chronos arms take a
# set validated all-pairs on the light surface (dataviz validate_palette.js:
# all checks pass; gold and magenta sit under 3:1, so the page ships a legend
# and a table view). The naive baseline is a grey reference line with its own
# dash, like the teal actuals, rather than a categorical slot.
MODELS = [
    {"id": "served_ensemble", "label": "Ensemble (served)", "color": "#d4a84b",
     "dash": [6, 4], "grains": ["month"], "source": "live",
     "family": "Incumbent",
     "about": "The production model behind /forecast: SARIMAX + Trailing-3 + "
              "LightGBM, fit on the estate's served feature file. Band is its "
              "measured per-step backtest sMAPE, as on /forecast."},
    {"id": "chronos2_monthly", "label": "Chronos-2 · monthly", "color": "#2a78d6",
     "dash": [6, 4], "grains": ["month"], "source": "artifact",
     "family": "Chronos-2 zero-shot",
     "about": "amazon/chronos-2, no fine-tuning, on the estate's monthly totals. "
              "k3 leak-free backtest: step-1 MASE 0.572 (incumbent 0.572)."},
    {"id": "chronos2_monthly_cov", "label": "Chronos-2 · monthly + weather",
     "color": "#4a3aa7", "dash": [6, 4], "grains": ["month"], "source": "artifact",
     "family": "Chronos-2 zero-shot",
     "about": "As monthly, plus NASA POWER weather channels (calendar and "
              "climatology-backfilled temp/humidity known ahead, the rest past-only). "
              "k3 leak-free backtest: step-1 MASE 0.507."},
    {"id": "chronos2_blocks", "label": "Chronos-2 · block sum", "color": "#e87ba4",
     "dash": [6, 4], "grains": ["month", "week"], "source": "artifact",
     "family": "Chronos-2 zero-shot",
     "about": "One forecast per block on weekly series, summed to the estate "
              "(months: weeks spread evenly over their days). k3 leak-free "
              "backtest: step-1 MASE 0.553; h2/h3 MAE ~12k vs the incumbent's 16-18k."},
    {"id": "chronos2_estate_weekly", "label": "Chronos-2 · estate weekly",
     "color": "#008300", "dash": [6, 4], "grains": ["month", "week"],
     "source": "artifact", "family": "Chronos-2 zero-shot",
     "about": "One forecast on the estate's weekly total (months: weeks spread "
              "over their days). k3 leak-free backtest: step-1 MASE 0.730."},
    {"id": "naive_trailing3", "label": "Naive · last-3 mean", "color": "#8a938f",
     "dash": [2, 3], "grains": ["month", "week"], "source": "live",
     "family": "Baseline",
     "about": "Flat mean of the last three complete periods. A floor every "
              "model should clear."},
    {"id": "seasonal_naive", "label": "Seasonal naive · same period last year",
     "color": "#5f6b67", "dash": [9, 3, 2, 3], "grains": ["month", "week"],
     "source": "live", "family": "Baseline",
     "about": "Each period's value from one year earlier (12 months / 52 weeks). "
              "The MASE yardstick: past ~6 months ahead no k3 model beats it "
              "clearly, so a long-range forecast that loses to it adds nothing."},
]
MODEL_IDS = [m["id"] for m in MODELS]
FREE_COLOURS = []  # extend only with hues re-validated against the set above


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bw = _load_by_path("epms_block_weekly", os.path.join(_FORECAST, "block_weekly.py"))


# -- history -----------------------------------------------------------------------
@lru_cache(maxsize=None)
def daily_blocks(estate):
    """Daily bunches per block for a lab estate (cached for the process)."""
    return bw.load_daily_blocks(ESTATES[estate]["blocks"])


def clear_cache():
    daily_blocks.cache_clear()
    month_history.cache_clear()
    week_history.cache_clear()


@lru_cache(maxsize=None)
def month_history(estate):
    """Monthly history the chart draws and the monthly models read.

    Returns a DataFrame: period (Period[M]), value, underrecorded, context_ok,
    scoreable. Ends at the last complete month (the forecast anchor).
    `scoreable` marks the months a backtest may score against: for k3 exactly
    the incumbent scoreboard's (exclude_from_model == 0), elsewhere every
    complete month.
    """
    spec = ESTATES[estate]
    if spec["features"]:
        mm = _load_by_path("epms_monthly_model", os.path.join(_FORECAST, "monthly_model.py"))
        est = mm.load_estate(spec["features"])
        last = est.loc[est["exclude_from_model"] == 0, "month"].max()
        h = est[est["month"] <= last]
        return pd.DataFrame({
            "period": h["month"].to_numpy(),
            "value": h[mm.TARGET].astype(float).to_numpy(),
            "underrecorded": (h["is_underrecorded"].astype(bool)
                              | h["exclude_from_model"].astype(bool)).to_numpy(),
            # the cleaned target is imputed on those months, so it is usable
            "context_ok": np.ones(len(h), dtype=bool),
            "scoreable": (h["exclude_from_model"] == 0).to_numpy(),
        })

    daily = daily_blocks(estate)
    total = daily.sum(axis=1)
    first, last = total.index.min(), total.index.max()
    rows = []
    for p in pd.period_range(first.to_period("M"), last.to_period("M"), freq="M"):
        lo, hi = max(p.start_time, first), min(p.end_time.normalize(), last)
        covered = (hi - lo).days + 1
        rows.append({"period": p, "value": float(total.loc[lo:hi].sum()),
                     "share": covered / p.days_in_month})
    df = pd.DataFrame(rows)
    while len(df) and df["share"].iloc[-1] < COMPLETE_SHARE:
        df = df.iloc[:-1]
    partial = df["share"] < COMPLETE_SHARE
    return pd.DataFrame({"period": df["period"].to_numpy(), "value": df["value"].to_numpy(),
                         "underrecorded": partial.to_numpy(),
                         "context_ok": (~partial).to_numpy(),
                         "scoreable": (~partial).to_numpy()})


@lru_cache(maxsize=None)
def week_history(estate):
    """Weekly estate totals, bins ending on the last recorded day.

    Returns a DataFrame: period (Timestamp of each bin's last day), value.
    """
    daily = daily_blocks(estate)
    wk = bw.weekly_bins(daily, daily.index.max()).sum(axis=1)
    nz = np.flatnonzero(wk.to_numpy() > 0)
    wk = wk.iloc[nz[0]:] if nz.size else wk
    return pd.DataFrame({"period": wk.index, "value": wk.to_numpy(dtype=float),
                         "underrecorded": np.zeros(len(wk), dtype=bool),
                         "context_ok": np.ones(len(wk), dtype=bool),
                         "scoreable": np.ones(len(wk), dtype=bool)})


def history(estate, grain):
    return month_history(estate) if grain == "month" else week_history(estate)


def period_str(p, grain):
    return str(p) if grain == "month" else pd.Timestamp(p).strftime("%Y-%m-%d")


def future_periods(anchor, grain, n=MAX_H):
    if grain == "month":
        return [anchor + k for k in range(1, n + 1)]
    return [pd.Timestamp(anchor) + pd.Timedelta(weeks=k) for k in range(1, n + 1)]


def context(estate, grain):
    """Usable context values and the anchor period (last history point)."""
    h = history(estate, grain)
    ok = h[h["context_ok"]]
    return ok["value"].to_numpy(dtype=float), h["period"].iloc[-1]


def estate_grain_status(estate, grain):
    """(available, reason, n_points) for an estate at a grain."""
    try:
        h = history(estate, grain)
    except FileNotFoundError:
        return False, "no harvest snapshot for this estate", 0
    n = int(h["context_ok"].sum())
    need = MIN_CONTEXT[grain]
    unit = "month" if grain == "month" else "week"
    if n < need:
        return False, f"only {n} complete {unit}{'s' if n != 1 else ''} of data (needs {need})", n
    return True, "", n


def model_spec(model_id):
    for m in MODELS:
        if m["id"] == model_id:
            return m
    raise KeyError(model_id)


def static_availability(model_id, estate, grain):
    """Why a model cannot run on an estate/grain, before looking for artifacts."""
    m = model_spec(model_id)
    if grain not in m["grains"]:
        return False, f"produces {' and '.join(m['grains'])} only"
    ok, reason, _ = estate_grain_status(estate, grain)
    if not ok:
        return False, reason
    if model_id == "served_ensemble" and not ESTATES[estate]["served"]:
        return False, "no served model for this estate"
    if model_id == "chronos2_monthly_cov" and not ESTATES[estate]["weather"]:
        return False, "no weather history for this estate (forecast/fetch_nasa_weather.py)"
    if model_id == "seasonal_naive" and len(history(estate, grain)) < SEASON[grain]:
        return False, f"needs a full year of history ({SEASON[grain]} {grain}s)"
    return True, ""


# -- backtests -----------------------------------------------------------------------
# Rows: origin, step, period, actual, pred, scoreable -- one per (origin, step)
# whose target period is inside history. Every model is scored on the SAME
# origins (backtest_origins), always from context <= origin only.
BACKTEST_COLS = ["origin", "step", "period", "actual", "pred", "scoreable"]


def backtest_origins(estate, grain):
    """History positions usable as walk-forward origins (not the last one)."""
    h = history(estate, grain)
    n_ctx = h["context_ok"].cumsum().to_numpy()
    return [i for i in range(len(h) - 1) if n_ctx[i] >= MIN_BACKTEST_CONTEXT[grain]]


def context_until(estate, grain, i):
    """Context values for an origin at history position i -- nothing after it."""
    h = history(estate, grain).iloc[:i + 1]
    return h.loc[h["context_ok"], "value"].to_numpy(dtype=float)


def backtest_rows(estate, grain, i, preds):
    """Rows for origin position i given its forecast (MAX_H values)."""
    h = history(estate, grain)
    rows = []
    for step in range(1, MAX_H + 1):
        j = i + step
        if j >= len(h):
            break
        rows.append({"origin": period_str(h["period"].iloc[i], grain), "step": step,
                     "period": period_str(h["period"].iloc[j], grain),
                     "actual": float(h["value"].iloc[j]), "pred": float(preds[step - 1]),
                     "scoreable": bool(h["scoreable"].iloc[j])})
    return rows


def backtest_path(model_id, estate, grain):
    return os.path.join(ARTIFACT_DIR, "backtest", model_id, f"{estate}_{grain}.csv")


def backtest(model_id, estate, grain):
    """(DataFrame, "") for a model's walk-forward backtest, or (None, reason)."""
    m = model_spec(model_id)
    if grain not in m["grains"]:
        return None, f"produces {' and '.join(m['grains'])} only"
    h = history(estate, grain)
    if model_id == "naive_trailing3":
        rows = []
        for i in backtest_origins(estate, grain):
            level = float(np.mean(context_until(estate, grain, i)[-3:]))
            rows += backtest_rows(estate, grain, i, [level] * MAX_H)
        return pd.DataFrame(rows, columns=BACKTEST_COLS), ""
    if model_id == "seasonal_naive":
        vals, ok = h["value"].to_numpy(dtype=float), h["context_ok"].to_numpy()
        k, rows = SEASON[grain], []
        for i in backtest_origins(estate, grain):
            preds = [vals[i + s - k] if 0 <= i + s - k and ok[i + s - k] else np.nan
                     for s in range(1, MAX_H + 1)]
            rows += backtest_rows(estate, grain, i, preds)
        df = pd.DataFrame(rows, columns=BACKTEST_COLS).dropna(subset=["pred"])
        return (df, "") if len(df) else (None, f"needs a full year of history before a target")
    if model_id == "served_ensemble":
        spec = ESTATES[estate]
        if not (spec["served"] and spec["features"] and grain == "month"):
            return None, ("no served-model backtest on this data" if spec["served"]
                          else "no served model for this estate")
        # the incumbent's own multi-step walk-forward (monthly_model.backtest_multih)
        d = pd.read_csv(os.path.join(os.path.dirname(spec["features"]), "predictions_multih.csv"))
        score = dict(zip(map(str, h["period"]), h["scoreable"]))
        d = d.rename(columns={"month": "period"})
        d["scoreable"] = d["period"].map(score).fillna(False).astype(bool)
        return d[BACKTEST_COLS], ""
    path = backtest_path(model_id, estate, grain)
    if not os.path.exists(path):
        ok, reason = static_availability(model_id, estate, grain)
        return None, reason if not ok else "backtest not built yet -- run forecast/lab/build_artifacts.py"
    return pd.read_csv(path, dtype={"origin": str, "period": str}), ""


# -- artifacts ----------------------------------------------------------------------
def artifact_path(model_id, estate, grain):
    return os.path.join(ARTIFACT_DIR, model_id, f"{estate}_{grain}.json")


def read_artifact(model_id, estate, grain):
    path = artifact_path(model_id, estate, grain)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_manifest():
    path = os.path.join(ARTIFACT_DIR, "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
