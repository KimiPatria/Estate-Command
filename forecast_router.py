"""
EPMS Production Forecast — FastAPI router
Mounted at /forecast on dashboard_server.py (port 8001).

Serves the estate FFB (plantation) production forecast for the next 3 months,
computed from the CSV-based model that lives in ./forecast/ (monthly_model.py +
monthly_model.joblib). No database access — the forecasting codebase reads its
own CSVs.

Endpoint:
  GET /forecast/data            — historical monthly production + 3-month forecast
                                  (Ensemble) with a measured per-step sMAPE
                                  uncertainty band (see _measured_smape_band).
  GET /forecast/data?refresh=1  — bypass the in-process cache and recompute.

The model fit (SARIMAX + LightGBM on ~40 months) takes ~1-2s, so the result is
cached in-process after the first request.
"""

import json
import importlib.util
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

log = logging.getLogger("epms-forecast")

router = APIRouter(prefix="/forecast", tags=["forecast"])

_FORECAST_DIR = Path(__file__).parent / "forecast"
_MODEL_PY     = _FORECAST_DIR / "monthly_model.py"
_MODEL_JOBLIB = _FORECAST_DIR / "monthly_model.joblib"

HORIZON_DEFAULT = 3
HORIZON_CHOICES = (3, 6, 12)

# ── multi-estate scope ──────────────────────────────────────────────────────
# Each modeled estate has its own directory of training artifacts
# (features_estate_monthly.csv, weather_nasa_power_history.csv,
# monthly_model.joblib — written by forecast.monthly_model.train()). k3 keeps
# the legacy top-level layout (forecast/*); anything added since (e.g. ec)
# lives in forecast/<ESTATE>/. MODELED_ESTATES names which config.ESTATES /
# SPATIAL_ONLY_ESTATES ids actually have a trained model behind them, so the
# Head Office view can state its real coverage instead of implying every
# estate is forecast.
#
# This is the single source of truth for every "N/A · no model yet" state in
# the UI: add an id here (and to ESTATE_DIRS below) the moment that estate
# gets a model, and the estate tiles / scope selector light up on their own.
MODELED_ESTATES = ("k3", "ec")

ESTATE_DIRS = {
    "k3": _FORECAST_DIR,
    "ec": _FORECAST_DIR / "EC",
}


def _estate_dir(estate: str) -> Path:
    return ESTATE_DIRS.get(estate, _FORECAST_DIR)

# Forecast levels the UI exposes. Only the ones in AVAILABLE_LEVELS have a
# model behind them; division/block are rendered as disabled containers until a
# per-block model exists (no per-block series is produced anywhere today).
SCOPE_LEVELS = ("group", "estate", "division", "block")
AVAILABLE_LEVELS = ("group", "estate")

# Estates with block-level spatial geometry (forecast/<ID>/<ID>_overlay.csv)
# but no live database or production model — a research overlay, not part of
# the Head Office's real estate roster. Kept out of config.ESTATES so the
# chat/dashboard/report multi-estate fan-out (which assumes a working DB
# connection per estate) is completely untouched; this page is the only
# consumer. Add an id here the moment another estate gets a spatial-only
# export; move it into config.ESTATES the moment it gets a real database.
SPATIAL_ONLY_ESTATES = {
    "ec": {"label": "EC"},
}


def _scope_payload(estate: str = "k3") -> dict:
    """Head-Office scope + coverage metadata for the forecast page.

    Purely declarative — it reports which estates/levels have a model or
    block geometry, never invents a series (or a map) for the ones that
    don't. `estate_count`/`modeled_count` stay scoped to config.ESTATES (the
    real multi-estate roster) so "N of M estates modelled" keeps meaning "N
    of the Head Office's actual estates" — spatial-only research estates are
    appended to the list for the scope selector and Block Map, never folded
    into that fraction.

    `estate` is the id the CALLER actually served numbers for (defaults "k3",
    the group/"all" view's estate — see forecast.html's switchScope). Two
    estates can be modeled at once now, so series_estate/series_label report
    the one actually behind this payload rather than guessing from the count.
    """
    from config import ESTATES, ESTATE_ORDER
    from forecast.blocks import has_block_geometry

    estates = [
        {
            "id": eid,
            "label": ESTATES[eid]["label"],
            "modeled": eid in MODELED_ESTATES,
            "has_blocks": has_block_geometry(eid),
        }
        for eid in ESTATE_ORDER
    ]
    all_labels = {e["id"]: e["label"] for e in estates}
    for eid, meta in SPATIAL_ONLY_ESTATES.items():
        if eid in ESTATES:
            continue  # already covered above once/if it joins the real roster
        estates.append({
            "id": eid,
            "label": meta["label"],
            "modeled": eid in MODELED_ESTATES,
            "has_blocks": has_block_geometry(eid),
            "spatial_only": True,
        })
        all_labels[eid] = meta["label"]
    modeled = [e for e in estates if e["modeled"]]
    return {
        "levels":           list(SCOPE_LEVELS),
        "available_levels": list(AVAILABLE_LEVELS),
        "estates":          estates,
        "modeled_count":    len(modeled),
        "estate_count":     len(ESTATES),
        # The estate the served numbers on THIS payload actually belong to.
        "series_estate":    estate if estate in MODELED_ESTATES else None,
        "series_label":     all_labels.get(estate) if estate in MODELED_ESTATES else None,
        "complete":         len(modeled) == len(ESTATES),
    }

def _multih_accuracy(est_dir: Path, max_step: int = HORIZON_DEFAULT) -> dict:
    """Pooled and per-horizon sMAPE over the MULTI-HORIZON walk-forward
    backtest — the accuracy of the product the client actually receives.

    The headline number the UI used to lead with was `served_smape`: the
    1-step-ahead figure from the 1-step expanding-window backtest
    (monthly_results.txt SCOREBOARD, ~10.1%). But the served product is a
    3-month forecast, and steps 2 and 3 are materially worse than step 1
    because the ensemble is recursive — it feeds its own predictions back in
    as AR lags. Leading with the 1-step number understates what the client
    receives and is a direct source of expectation mismatch.

    This re-scores predictions_multih.csv (written by
    monthly_model.backtest_multih, already on disk — nothing is refit here)
    over steps 1..max_step:
      * pooled  — one sMAPE over every (origin, step<=max_step) row, the
                  headline "what you get over the next 3 months" number
      * per_horizon — the same split by step, so the degradation is visible
                  rather than averaged away

    Returns {} if the file is missing, so callers fall back to the 1-step
    figure rather than showing nothing.
    """
    path = Path(est_dir) / "predictions_multih.csv"
    try:
        import csv as _csv
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                step = int(float(r["step"]))
                if step <= max_step:
                    rows.append((step, float(r["actual"]), float(r["pred"]),
                                 str(r["origin"]), str(r["month"])))
    except Exception as exc:
        log.warning("[forecast] multi-horizon accuracy unavailable (%s): %s",
                    path, exc)
        return {}
    if not rows:
        return {}

    def _smape(pairs):
        # identical formula to monthly_model.smape
        tot = 0.0
        for a, p in pairs:
            d = abs(a) + abs(p)
            tot += 0.0 if d == 0 else 2 * abs(p - a) / d
        return round(tot / len(pairs) * 100, 1)

    # Seasonal-naive benchmark on the SAME (origin, step) rows, so MASE
    # describes the 3-month product rather than being borrowed from the
    # 1-step scoreboard. Same rule as monthly_model.backtest: mean of the
    # target's calendar month over the non-excluded history at the origin.
    naive = _seasonal_naive_preds(est_dir, [(o, m) for _, _, _, o, m in rows])

    def _mase(sel):
        """MAE of the model over `sel` divided by the seasonal-naive MAE over
        the same rows. Returns None if the benchmark could not be built."""
        num, den = [], []
        for _st, a, p, o, m in sel:
            nv = naive.get((o, m))
            if nv is None:
                return None
            num.append(abs(p - a))
            den.append(abs(nv - a))
        d = sum(den) / len(den) if den else 0.0
        return round((sum(num) / len(num)) / d, 2) if d else None

    per_h = []
    for s in range(1, max_step + 1):
        sel = [r for r in rows if r[0] == s]
        if sel:
            per_h.append({"step": s, "smape": _smape([(a, p) for _, a, p, _, _ in sel]),
                          "mase": _mase(sel), "n": len(sel)})
    return {
        "pooled_smape": _smape([(a, p) for _, a, p, _, _ in rows]),
        "pooled_mase":  _mase(rows),
        "per_horizon":  per_h,
        "n_rows":       len(rows),
        "n_origins":    len({o for _, _, _, o, _ in rows}),
        "max_step":     max_step,
        "source":       "predictions_multih.csv (multi-horizon walk-forward)",
        "benchmark":    "SeasonalNaive on the same walk-forward rows (MASE denominator)",
    }


def _seasonal_naive_preds(est_dir: Path, pairs: list) -> dict:
    """Seasonal-naive prediction for each (origin, target month) pair.

    Mirrors monthly_model.backtest's benchmark exactly: the mean of the
    target's calendar month across the non-excluded history available at the
    origin, falling back to the mean of that history when the calendar month
    has not been seen yet. Returns {} if the feature file is unreadable, in
    which case MASE is reported as unavailable rather than approximated.
    """
    try:
        import csv as _csv
        y, excl = {}, set()
        with open(Path(est_dir) / "features_estate_monthly.csv",
                  newline="", encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                y[r["month"]] = float(r["bunches_total"])
                if str(r.get("exclude_from_model", "0")).strip() in ("1", "1.0"):
                    excl.add(r["month"])
    except Exception as exc:
        log.warning("[forecast] seasonal-naive benchmark unavailable: %s", exc)
        return {}
    months = sorted(y)
    out = {}
    for origin, m in set(pairs):
        hist = [x for x in months if x <= origin and x not in excl]
        same = [y[x] for x in hist if x[-2:] == m[-2:]]
        pool = same or [y[x] for x in hist]
        if pool:
            out[(origin, m)] = sum(pool) / len(pool)
    return out


def _served_band_coverage(est_dir: Path, smape_widths: dict, fallback_smape: float,
                          max_step: int = HORIZON_DEFAULT) -> dict:
    """Empirical coverage of the band ACTUALLY DRAWN on the forecast chart.

    The chart band is +/- the measured per-step sMAPE (see
    _measured_smape_band), which is a much NARROWER interval than the
    split/ACI conformal band calibrated in conformal.py. Reporting the
    conformal band's ~95% coverage next to the sMAPE band's width would
    misattribute one interval's calibration to a different, narrower
    interval — so this measures the sMAPE band on its own terms: over the
    multi-horizon walk-forward folds, how often did the actual fall inside
    pred +/- band_pct% of pred?

    Returns {step: {"coverage": float, "n": int}}.
    """
    path = Path(est_dir) / "predictions_multih.csv"
    try:
        import csv as _csv
        hits: dict = {}
        with open(path, newline="", encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                step = int(float(r["step"]))
                if step > max_step:
                    continue
                a, pr = float(r["actual"]), float(r["pred"])
                band = _measured_smape_band(smape_widths, fallback_smape, step)
                inside = abs(a - pr) <= abs(pr) * band / 100.0
                h, n = hits.get(step, (0, 0))
                hits[step] = (h + (1 if inside else 0), n + 1)
    except Exception as exc:
        log.warning("[forecast] served-band coverage unavailable (%s): %s", path, exc)
        return {}
    return {s: {"coverage": round(h / n, 3), "n": n}
            for s, (h, n) in sorted(hits.items()) if n}


def _measured_smape_band(smape_widths: dict, fallback_smape: float, step: int) -> float:
    """Served uncertainty band at a given forecast step: the sMAPE measured
    directly on the multi-horizon walk-forward backtest for that step
    (forecast/conformal.step_smape, persisted in the joblib as
    conformal.step_smape). No growth formula is assumed — this is what the
    model's error actually did at that step over ~20-30 backtest folds.

    Client-facing choice over the split/ACI conformal interval (still
    computed and persisted for the Investigator's band-breach trigger)
    because "here's the error we've actually measured at each horizon" is
    both simpler to explain and more defensible than either a fitted formula
    or an unexplained calibrated quantile.

    Falls back to the flat 1-step backtested sMAPE only if the joblib
    predates step_smape (no per-step data at all) — never a guessed curve.
    """
    if not smape_widths:
        return fallback_smape
    w = smape_widths.get(step)
    return float(w) if w is not None else float(max(smape_widths.values()))


# Operator-friendly names for the LightGBM features (TreeSHAP driver panel).
_FEAT_LABELS = {
    "y_lag1": "Production last month",
    "y_lag2": "Production 2 months ago",
    "y_lag3": "Production 3 months ago",
    "y_lag12": "Production same month last year",
    "y_roll3_mean": "3-month average production",
    "y_roll6_mean": "6-month average production",
    "rain_lag1m": "Rainfall last month (mm)",
    "wb_lag1m": "Water balance last month (mm)",
    "rain_flower_5_7m": "Rain at flowering (5–7 mo ago, mm)",
    "temp_lag1m": "Temperature last month (°C)",
    "humid_lag1m": "Humidity last month (%)",
    "prune_md_lag1m": "Pruning effort last month (man-days)",
    "prune_qty_lag5m": "Pruning 5 months ago (qty)",
    "fert_qty_lag3m": "Fertiliser 3 months ago (qty)",
    "completeness_scale": "Recording completeness",
    "n_harvest_days": "Harvest days in month",
    "is_underrecorded": "Under-recorded flag",
}


def _shap_drivers(model, feats, feature_rows, top_n=7):
    """Per-forecast-month TreeSHAP attributions for the LightGBM member.

    Uses LightGBM's built-in pred_contrib (Lundberg's TreeSHAP): one signed
    contribution per feature plus the expected value in the last column.
    month_sin/month_cos are merged into a single 'Seasonality' driver. Any
    failure returns [] so the forecast payload never breaks on explainability.
    """
    import pandas as pd

    if not feature_rows:
        return []
    try:
        X = pd.DataFrame(feature_rows)
        contrib = model.predict(X[feats], pred_contrib=True)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[forecast] TreeSHAP attribution failed: %s", exc)
        return []

    months_out = []
    for i, row in enumerate(feature_rows):
        base = float(contrib[i][-1])
        per_feat = dict(zip(feats, (float(v) for v in contrib[i][:-1])))
        season = per_feat.pop("month_sin", 0.0) + per_feat.pop("month_cos", 0.0)
        items = [{"feature": "seasonality",
                  "label": "Seasonality (calendar month)",
                  "value": row["month"],
                  "contribution": round(season)}]
        for f, c in per_feat.items():
            v = row.get(f)
            items.append({
                "feature": f,
                "label": _FEAT_LABELS.get(f, f),
                "value": (None if v is None or v != v else round(float(v), 1)),
                "contribution": round(c),
            })
        items.sort(key=lambda d: abs(d["contribution"]), reverse=True)
        months_out.append({
            "month": row["month"],
            "base": round(base),
            "prediction": round(base + sum(d["contribution"] for d in items)),
            "contributions": items[:top_n],
        })
    return months_out

# In-process cache keyed by horizon (model fit is deterministic).
_CACHE: dict = {}
_LOCK = Lock()


# ── model loading ───────────────────────────────────────────────────────────

def _load_monthly_model():
    """Import forecast/monthly_model.py as a standalone module.

    It is self-contained (only third-party imports + its own CSV paths derived
    from __file__), so loading it by path avoids polluting sys.path or colliding
    with the project's top-level modules.
    """
    spec = importlib.util.spec_from_file_location("epms_forecast_monthly_model", _MODEL_PY)
    mm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mm)
    return mm


def _compute(estate: str = "k3", horizon: int = HORIZON_DEFAULT) -> dict:
    """Fit the served ensemble and build the history + forecast payload for one
    estate's model artifacts (forecast/ for k3, forecast/<ESTATE>/ otherwise —
    see ESTATE_DIRS)."""
    import joblib  # local import so the server still boots if deps are missing

    mm = _load_monthly_model()
    est_dir = _estate_dir(estate)
    model_joblib = est_dir / "monthly_model.joblib"

    # Served model metadata (feature set + headline backtest metrics).
    try:
        meta = joblib.load(model_joblib)
        feats        = meta.get("features", mm.BASE_FEATS)
        smape        = round(float(meta.get("served_smape", 10.7)), 1)
        mase         = round(float(meta.get("served_mase", 0.61)), 2)
        n_folds      = int(meta.get("n_folds", 30))
        served_model = meta.get("served_model", "Ensemble(SARIMAX+Trailing3+LightGBM)")
        trained_through = str(meta.get("trained_through", ""))
        conf         = meta.get("conformal") or {}
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[forecast] joblib metadata load failed: %s", exc)
        feats, smape, mase, n_folds = mm.BASE_FEATS, 10.7, 0.61, 30
        served_model, trained_through = "Ensemble(SARIMAX+Trailing3+LightGBM)", ""
        conf = {}
    # conf_widths is the calibrated split/ACI conformal band; kept for the
    # Investigator's band-breach trigger (forecast_investigator._conformal_meta)
    # but no longer served on the chart below.
    conf_widths = {int(k): float(v) for k, v in (conf.get("served_widths") or {}).items()}
    # smape_widths is the measured per-step sMAPE band actually served on the
    # chart — see _measured_smape_band.
    smape_widths = {int(k): float(v) for k, v in (conf.get("step_smape") or {}).items()}
    smape_widths_n = {int(k): int(v) for k, v in (conf.get("step_smape_n") or {}).items()}

    # Data + recursive 3-month forecast (mirrors forecast/serving.monthly_forecast,
    # which we cannot import directly because it pulls in a missing daily_model).
    est = mm.load_estate(est_dir / "features_estate_monthly.csv")
    wm  = mm.build_weather_monthly(est_dir / "weather_nasa_power_history.csv", extend=horizon)
    # Workdone (pruning/fertilizer) effort is only recorded for k3; other estates'
    # features already carry those columns as NaN (baked in at build time — see
    # forecast/build_estate_features.py), so extending them here would wrongly
    # pull k3's own workdone_daily.csv into a different estate's forecast.
    wd  = (mm.build_workdone_monthly(extend=horizon)
           if estate == "k3" and os.path.exists(mm.WORKDONE_FILE) else None)
    model = mm.fit_final(est, feats)
    feat_rows: list = []
    fwd = mm.forecast_next(est, model, feats, wm, horizon=horizon, label="LightGBM",
                           wdmonth=wd, feature_rows=feat_rows)

    # The forecast is anchored on the last fully-recorded month; the first
    # forecast month is one step after it.
    last_modeled = est.loc[est["exclude_from_model"] == 0, "month"].max()
    first_fc     = last_modeled + 1

    # History = cleaned monthly target for every month strictly before the
    # forecast window (drops the partial trailing month so it isn't shown twice).
    hist_df = est[est["month"] < first_fc]
    history = [
        {
            "month": str(row["month"]),
            "value": round(float(row["bunches_total"])),
            "underrecorded": bool(row.get("is_underrecorded", 0)) or bool(row.get("exclude_from_model", 0)),
            "n_harvest_days": int(row["n_harvest_days"]) if row.get("n_harvest_days") == row.get("n_harvest_days") else None,
        }
        for _, row in hist_df.iterrows()
    ]

    # Anchor: the last historical point the forecast line should connect to.
    anchor_row = hist_df.iloc[-1]
    anchor = {"month": str(anchor_row["month"]), "value": round(float(anchor_row["bunches_total"]))}

    # Interval per step: sMAPE band measured directly on the multi-horizon
    # walk-forward backtest for that step — no growth formula assumed. See
    # _measured_smape_band. band_pct stays as the width relative to the point
    # forecast so existing UI fields keep working.
    interval_method = "measured_smape" if smape_widths else "flat_smape"
    forecast = []
    for i, (_, r) in enumerate(fwd.iterrows()):
        step = i + 1
        ens = float(r["Ensemble"])
        step_band = _measured_smape_band(smape_widths, smape, step)
        lower, upper = ens * (1 - step_band / 100), ens * (1 + step_band / 100)
        forecast.append({
            "month":     str(r["month"]),
            "step":      step,
            "ensemble":  round(ens),
            "lower":     round(lower),
            "upper":     round(upper),
            "band_pct":  step_band,
            "sarimax":   round(float(r["SARIMAX"])),
            "trailing3": round(float(r["Trailing3"])),
            "lightgbm":  round(float(r["LightGBM"])),
        })

    drivers = _shap_drivers(model, feats, feat_rows)

    total_forecast = sum(f["ensemble"] for f in forecast)
    total_3m = sum(f["ensemble"] for f in forecast[:3])
    avg_band_pct = round(sum(f["band_pct"] for f in forecast) / len(forecast), 1) if forecast else smape
    last_band_pct = forecast[-1]["band_pct"] if forecast else smape
    avg_effective_mase = round(mase * (avg_band_pct / smape), 2) if smape else mase
    last_effective_mase = round(mase * (last_band_pct / smape), 2) if smape else mase

    # Interval metadata for the UI/agents: how the served band was built and
    # how many backtest folds back each step's number (the defensibility
    # trail). The conformal calibration's own coverage stats live in conf/preq
    # and are surfaced separately by the Investigator, not here.
    # Band width next to whether that band actually held. The coverage shown
    # is the SERVED (sMAPE) band's own empirical coverage — not the wider
    # conformal band's, which would flatter it. The conformal band's coverage
    # is reported separately on /model-health, beside its own width.
    band_cov = _served_band_coverage(est_dir, smape_widths, smape,
                                     max_step=horizon)
    interval = {
        "method":  interval_method,
        "conformal_nominal_pct": round(float(conf.get("nominal", 0.90)) * 100),
        "conformal_coverage_h1_3": conf.get("coverage_h1_3"),
        "per_step": {
            str(s): {"n_scored": smape_widths_n.get(s),
                     "band_pct": _measured_smape_band(smape_widths, smape, s),
                     "coverage": (band_cov.get(s) or {}).get("coverage"),
                     "coverage_n": (band_cov.get(s) or {}).get("n")}
            for s in range(1, horizon + 1) if s in smape_widths_n
        },
    }

    # Headline accuracy for the product actually served (a 3-month forecast),
    # not the 1-step figure. `smape` below stays the 1-step number so existing
    # consumers keep working, but it is labelled "1-month-ahead" everywhere it
    # is shown — see _multih_accuracy for why leading with it misleads.
    acc_mh = _multih_accuracy(est_dir, max_step=min(horizon, HORIZON_DEFAULT))
    headline_smape = acc_mh.get("pooled_smape", smape)

    return {
        "history":        history,
        "anchor":         anchor,
        "forecast":       forecast,
        "interval":       interval,
        "scope":          _scope_payload(estate),
        "drivers": {
            "note": "TreeSHAP attribution of the LightGBM member (one of three "
                    "equal-weight ensemble members); it explains that member's "
                    "prediction, not the whole ensemble.",
            "months": drivers,
        },
        # 1-step-ahead sMAPE from the 1-step backtest. Kept for backward
        # compatibility and still shown, but ALWAYS labelled "1-month-ahead" —
        # it is not the accuracy of the 3-month product.
        "smape":          smape,
        "smape_1step":    smape,
        # The headline: pooled sMAPE over steps 1..3 of the multi-horizon
        # walk-forward, with the per-horizon breakdown beside it.
        "headline_smape": headline_smape,
        # MASE of the 3-month product, measured against a seasonal-naive
        # benchmark on the same walk-forward rows (not the 1-step scoreboard's).
        "headline_mase":  acc_mh.get("pooled_mase"),
        "accuracy_multih": acc_mh,
        "mase":           mase,
        "n_folds":        n_folds,
        "model_name":     served_model,
        "model_label":    "Ensemble · SARIMAX + Trailing-3 + LightGBM",
        "trained_through": trained_through,
        "horizon":         horizon,
        "total_forecast":  total_forecast,
        "total_3m":        total_3m,
        "avg_band_pct":    avg_band_pct,
        "last_band_pct":   last_band_pct,
        "avg_effective_mase":  avg_effective_mase,
        "last_effective_mase": last_effective_mase,
        "band_pct":        smape,
        "generated_at":    datetime.now().isoformat(timespec="seconds"),
    }


def get_forecast(estate: str = "k3", refresh: bool = False, horizon: int = HORIZON_DEFAULT) -> dict:
    global _CACHE
    key = (estate, horizon)
    with _LOCK:
        if key not in _CACHE or refresh:
            _CACHE[key] = _compute(estate=estate, horizon=horizon)
        return _CACHE[key]


# ── model health (MLOps view) ───────────────────────────────────────────────

def _model_health() -> dict:
    """Served-model health: accuracy, interval calibration, data quality and
    lifecycle — for the /model-health page, deliberately kept off the
    operator-facing forecast page.

    Everything here is read back from the training artifact (monthly_model.joblib:
    scoreboard / conformal / trained_through) and from the already-cached
    forecast payload. Anything with no measurement behind it — drift, retrain
    schedule, registry version — is returned as an explicit unavailable block
    rather than a placeholder number.
    """
    import joblib

    fc = get_forecast()  # cached; no refit

    meta: dict = {}
    try:
        meta = joblib.load(_MODEL_JOBLIB)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[forecast] model-health joblib load failed: %s", exc)

    # ── accuracy: the served row of the walk-forward scoreboard ──────────────
    board_raw = meta.get("scoreboard")
    scoreboard: list[dict] = []
    if board_raw is not None:
        try:
            rows = (board_raw.to_dict(orient="records")
                    if hasattr(board_raw, "to_dict") else list(board_raw))
            scoreboard = [
                {
                    "model": str(r.get("model")),
                    "mae":   round(float(r["MAE"])),
                    "rmse":  round(float(r["RMSE"])),
                    "bias":  round(float(r["Bias"])),
                    "smape": round(float(r["sMAPE"]), 1),
                    "mase":  round(float(r["MASE"]), 3),
                }
                for r in rows
            ]
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[forecast] scoreboard parse failed: %s", exc)

    served_ens = str(meta.get("served_ensemble") or "")
    served_row = next((r for r in scoreboard if r["model"] == served_ens), None)

    history = fc.get("history") or []
    hist_vals = [h["value"] for h in history if h.get("value") is not None]
    mean_actual = (sum(hist_vals) / len(hist_vals)) if hist_vals else None

    # The scoreboard row is the 1-STEP backtest. The served product is a
    # 3-month forecast, so the headline is the pooled multi-horizon sMAPE and
    # the 1-step figure is kept beside it, explicitly labelled.
    acc_mh = fc.get("accuracy_multih") or {}
    accuracy = {
        # 1-month-ahead MASE (1-step backtest). The 3-month product's own MASE,
        # measured on the same walk-forward rows as smape_h1_3, is mase_h1_3.
        "mase":     served_row["mase"]  if served_row else fc.get("mase"),
        "mase_h1_3": acc_mh.get("pooled_mase"),
        # 1-month-ahead only — never present this as the overall figure.
        "smape":    served_row["smape"] if served_row else fc.get("smape"),
        "smape_1step": served_row["smape"] if served_row else fc.get("smape"),
        # headline: pooled over steps 1-3 of the multi-horizon walk-forward
        "smape_h1_3":   acc_mh.get("pooled_smape"),
        "per_horizon":  acc_mh.get("per_horizon") or [],
        "multih_n":     acc_mh.get("n_rows"),
        "multih_origins": acc_mh.get("n_origins"),
        "mae":      served_row["mae"]   if served_row else None,
        "rmse":     served_row["rmse"]  if served_row else None,
        "bias":     served_row["bias"]  if served_row else None,
        "bias_pct": (round(served_row["bias"] / mean_actual * 100, 1)
                     if served_row and mean_actual else None),
        "n_folds":  fc.get("n_folds"),
        "benchmark": "SeasonalNaive (MASE denominator)",
    }

    # ── interval calibration: prequential coverage vs nominal, per step ───────
    conf = meta.get("conformal") or {}
    preq = conf.get("prequential") or {}
    widths = conf.get("served_widths") or {}
    step_sm = conf.get("step_smape") or {}
    step_n = conf.get("step_smape_n") or {}
    steps = sorted({int(k) for k in list(widths) + list(step_sm) + list(preq)})
    # Empirical coverage of the band actually drawn on the forecast chart
    # (+/- measured sMAPE). Reported beside the conformal band's coverage so
    # the two are never confused: they are different intervals.
    _chart_cov = _served_band_coverage(
        _estate_dir("k3"),
        {int(k): float(v) for k, v in step_sm.items()},
        float(fc.get("smape") or 0.0),
        max_step=max(steps) if steps else HORIZON_DEFAULT)
    per_step = []
    for s in steps:
        st = preq.get(s) or preq.get(str(s)) or {}
        sw = (float(widths[s]) if s in widths else None)
        # Band width in the units the client complains in: a percentage of a
        # typical month, not a raw bunch count. served_width is a HALF-width,
        # so the full interval is 2x it.
        per_step.append({
            "step":           s,
            "smape_pct":      (round(float(step_sm[s]), 1)
                               if s in step_sm else step_sm.get(str(s))),
            "n_scored":       step_n.get(s, step_n.get(str(s))),
            "split_coverage": st.get("split_coverage"),
            "aci_coverage":   st.get("aci_coverage"),
            "served_width":   (round(sw) if sw is not None else None),
            "served_width_pct": (round(2 * sw / mean_actual * 100, 1)
                                 if sw is not None and mean_actual else None),
            "chart_band_pct": (round(float(step_sm[s]), 1)
                               if s in step_sm else None),
            "chart_width_pct": (round(2 * float(step_sm[s]), 1)
                                if s in step_sm else None),
            "chart_coverage": (_chart_cov.get(s) or {}).get("coverage"),
            "chart_coverage_n": (_chart_cov.get(s) or {}).get("n"),
        })

    interval = {
        "served_method":  conf.get("served_method"),
        "nominal_pct":    round(float(conf.get("nominal", 0.90)) * 100),
        "coverage_h1_3":  conf.get("coverage_h1_3"),
        "coverage_ok":    conf.get("coverage_ok"),
        "chart_method":   (fc.get("interval") or {}).get("method"),
        "per_step":       per_step,
    }

    # ── data quality: measured off the same cleaned monthly series ───────────
    under = [h for h in history if h.get("underrecorded")]
    trained_through = str(meta.get("trained_through") or fc.get("trained_through") or "")
    freshness_months = None
    if trained_through and "-" in trained_through:
        try:
            ty, tm = (int(x) for x in trained_through.split("-")[:2])
            now = datetime.now()
            freshness_months = (now.year - ty) * 12 + (now.month - tm)
        except Exception:
            freshness_months = None

    def _src(name: str, path) -> dict:
        p = Path(path)
        return {"name": name, "present": p.exists(),
                "modified": (datetime.fromtimestamp(p.stat().st_mtime)
                             .isoformat(timespec="seconds") if p.exists() else None)}

    # ── weather provenance + physics audit ───────────────────────────────────
    # The walk-forward scoreboard is a weak guard on weather: until recently it
    # read only the columns baked into features_estate_monthly.csv, so a corrupt
    # weather file moved the served forward forecast and the conformal intervals
    # without moving a single metric. K3 trained on such a file for months (its
    # stored ET0 reproduced from its own inputs at 1.3%). This audit is the
    # standing check for that class of failure.
    # NOTE: pinned to the K3 directory, matching the "sources" list below —
    # making model-health estate-aware is tracked separately.
    weather_audit = None
    try:
        _wq_spec = importlib.util.spec_from_file_location(
            "epms_weather_quality", str(_FORECAST_DIR / "weather_quality.py"))
        _wq = importlib.util.module_from_spec(_wq_spec)
        _wq_spec.loader.exec_module(_wq)
        weather_audit = _wq.validate_weather_csv(
            str(_FORECAST_DIR / "weather_nasa_power_history.csv"))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[forecast] weather quality audit failed: %s", exc)

    data_quality = {
        "months_history":       len(history),
        "first_month":          history[0]["month"] if history else None,
        "last_complete_month":  trained_through or None,
        "underrecorded_months": len(under),
        "underrecorded_pct":    (round(len(under) / len(history) * 100, 1)
                                 if history else None),
        "freshness_months":     freshness_months,
        "weather":              weather_audit,
        "sources": [
            _src("Estate monthly features", _FORECAST_DIR / "features_estate_monthly.csv"),
            _src("Weather (NASA POWER)",    _FORECAST_DIR / "weather_nasa_power_history.csv"),
            _src("Work-done daily",         _FORECAST_DIR.parent / "workdone_daily.csv"),
            _src("Model artifact",          _MODEL_JOBLIB),
        ],
    }

    # ── drift: nothing measures it yet ───────────────────────────────────────
    drift = {
        "available": False,
        "reason": "No reference-vs-live distribution monitor is wired up; drift "
                  "needs a scheduled job comparing the training window against "
                  "incoming months.",
        "features": ["Production (target)", "Rainfall", "Water balance",
                     "Temperature", "Harvest days"],
    }

    artifact = _MODEL_JOBLIB
    lifecycle = {
        "served_model":    meta.get("served_model") or fc.get("model_name"),
        # Exact scoreboard row name — the UI flags the served row on equality,
        # not substring (member names like "SARIMAX" appear inside the ensemble
        # name and would tag every row).
        "served_ensemble": served_ens or None,
        "served_label":    fc.get("model_label"),
        "trained_through": trained_through or None,
        "artifact_built":  (datetime.fromtimestamp(artifact.stat().st_mtime)
                            .isoformat(timespec="seconds") if artifact.exists() else None),
        "version":         None,   # no model registry wired up
        "next_retrain":    None,   # no retraining schedule wired up
        "registry_note":   "Training runs log to MLflow, but no registry version "
                           "is stamped on the served artifact and no retraining "
                           "schedule exists yet.",
    }

    return {
        "scope":        _scope_payload(),
        "lifecycle":    lifecycle,
        "accuracy":     accuracy,
        "scoreboard":   scoreboard,
        "interval":     interval,
        "data_quality": data_quality,
        "drift":        drift,
        "research":     _research_arms(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _research_arms() -> dict:
    """Offline benchmark arms (TimesFM, direct multi-horizon) vs the served model.

    Read straight from the artifact forecast/direct_experiment.py writes. Nothing
    is recomputed here: these come from a multi-minute walk-forward and must not
    run inside a request. Absent file -> an explicit unavailable block, matching
    how drift and registry version are handled.

    These arms are NOT served and nothing on this page promotes them. Two of the
    reasons are structural rather than statistical: the TimesFM 3.0 weights are
    non-commercial, and no TimesFM arm can produce the SHAP attribution the
    forecast page shows.
    """
    path = _FORECAST_DIR / "experiments" / "direct" / "k3_research_arms.json"
    if not path.exists():
        return {
            "available": False,
            "reason": "No benchmark artifact. Run `python forecast/direct_experiment.py "
                      "--estate k3` (and forecast/timesfm_experiment.py for the "
                      "TimesFM arms) to generate it.",
        }
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("[forecast] research arms parse failed: %s", exc)
        return {"available": False, "reason": f"Benchmark artifact unreadable: {exc}"}

    payload["available"] = True
    payload["stale"] = _artifact_older_than(path)
    return payload


def _artifact_older_than(path: Path) -> bool:
    """True when the benchmark predates the served model artifact.

    A benchmark scored against an older incumbent is misleading on this page,
    so the UI can mark it rather than showing a comparison that no longer holds.
    """
    try:
        return path.stat().st_mtime < _MODEL_JOBLIB.stat().st_mtime
    except Exception:
        return False


# ── routes ──────────────────────────────────────────────────────────────────

@router.get("/model-health")
def forecast_model_health():
    """Served-model health for the /model-health page (accuracy, interval
    calibration, data quality, lifecycle). Reads the cached forecast + the
    training artifact — never refits."""
    try:
        return _model_health()
    except Exception as exc:
        log.exception("[forecast] model-health failed")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Model health computation failed: {exc}"},
        )


@router.get("/blocks")
def forecast_blocks(estate: str = Query(...)):
    """Block-polygon GeoJSON for the Block Map. Spatial lookup only — no
    forecast, no model. Returns 404 (not an empty 200) for an estate with no
    overlay file, so the UI can tell "no geometry yet" apart from "empty
    estate" and render the same honest N/A state used everywhere else."""
    from forecast.blocks import load_block_geometry

    try:
        geo = load_block_geometry(estate)
    except Exception as exc:
        log.exception("[forecast] block geometry load failed for estate=%s", estate)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Block geometry load failed: {exc}"},
        )
    if geo is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No block geometry for estate '{estate}'."},
        )
    log.info("[forecast] served %d block(s) for estate=%s", geo["block_count"], estate)
    return geo


@router.get("/block-forecast")
def forecast_block_forecast(estate: str = Query(...)):
    """Per-block trailing-mean forecast (forecast/build_block_forecast.py output)
    for the Block Map's "Forecast" overlay and the Block drill-down table.

    Not the estate-level LightGBM+SARIMAX ensemble run per block — every block
    has only a handful of monthly points (same thin history the estate itself
    has), nowhere near enough for that pipeline to mean anything. Returns 404
    (not an empty 200) for an estate with no block_forecast.csv, matching
    /blocks' honest-N/A convention.
    """
    import pandas as pd

    path = _estate_dir(estate) / "block_forecast.csv"
    if not path.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"No block-level forecast for estate '{estate}'."},
        )
    try:
        df = pd.read_csv(path)
        blocks = [
            {
                "division_code": str(r["division_code"]),
                "block_code": str(r["block_code"]),
                "planted_area_ha": (float(r["planted_area_ha"])
                                    if r["planted_area_ha"] == r["planted_area_ha"] else None),
                "forecast_bunches": int(r["forecast_bunches"]),
                "n_months_used": int(r["n_months_used"]),
                "confidence": str(r["confidence"]),
            }
            for _, r in df.iterrows()
        ]
    except Exception as exc:
        log.exception("[forecast] block-forecast load failed for estate=%s", estate)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Block-forecast load failed: {exc}"},
        )
    log.info("[forecast] served %d block forecast row(s) for estate=%s", len(blocks), estate)
    return {
        "estate": estate,
        "method": "trailing_mean",
        "caveat": "Each block has only a handful of recorded months (the estate's own "
                  "full history) — this is a trailing mean of each block's own recent "
                  "months, not a validated per-block model. Treat as illustrative.",
        "blocks": blocks,
    }


@router.get("/data")
def forecast_data(refresh: bool = False, horizon: int = Query(default=HORIZON_DEFAULT),
                   estate: str = Query(default="k3")):
    horizon = horizon if horizon in HORIZON_CHOICES else HORIZON_DEFAULT
    if estate not in MODELED_ESTATES:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No forecast model for estate '{estate}'."},
        )
    try:
        data = get_forecast(estate=estate, refresh=refresh, horizon=horizon)
        log.info("[forecast] served %d history + %d forecast months for estate=%s "
                 "(sMAPE %.1f%% avg-band %.1f%%)",
                 len(data["history"]), len(data["forecast"]), estate, data["smape"], data["avg_band_pct"])
        return data
    except Exception as exc:
        log.exception("[forecast] computation failed for estate=%s", estate)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Forecast computation failed: {exc}"},
        )


@router.get("/investigate")
async def forecast_investigate(refresh: bool = False, force: bool = False,
                               estate: str = Query(default="k3")):
    """Forecast Investigator agent (LangGraph ReAct over live-signal + DB + SHAP
    tools). Runs only when a trigger fires — last actual outside the conformal
    band, or numeric forecast vs risk-level divergence — unless ?force=1.
    TTL-cached 6h; ?refresh=1 recomputes.
    """
    from forecast_intelligence import get_intelligence
    from forecast_investigator import get_investigation

    if estate not in MODELED_ESTATES:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No forecast model for estate '{estate}'."},
        )

    def _fc():
        return get_forecast(estate=estate)

    async def _intel():
        return await get_intelligence(_fc, refresh=False, estate=estate)

    try:
        data = await get_investigation(_fc, intelligence_loader=_intel,
                                       refresh=refresh, force=force, estate=estate)
        log.info("[forecast] investigator served (triggered=%s, tools=%d)",
                 data.get("triggered"), len(data.get("evidence_log") or []))
        return data
    except Exception as exc:
        log.exception("[forecast] investigator failed")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Investigation failed: {exc}"},
        )


@router.get("/intelligence")
async def forecast_intelligence(refresh: bool = False,
                                estate: str = Query(default="k3")):
    """LLM risk-synthesis layer over the ML forecast (read-only; never alters the
    numbers). Pulls live weather / ENSO / fire-hotspot signals and returns a risk
    level + narrative. TTL-cached 6h per estate; ?refresh=1 forces a recompute.

    Signals are fetched at the requested estate's own coordinates (read from its
    weather provenance sidecar), so selecting EC no longer reports Sabah weather
    and Sabah fires for a Papua estate.
    """
    from forecast_intelligence import get_intelligence
    if estate not in MODELED_ESTATES:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No forecast model for estate '{estate}'."},
        )
    try:
        data = await get_intelligence(lambda: get_forecast(estate=estate),
                                      refresh=refresh, estate=estate)
        log.info("[forecast] intelligence served (estate=%s risk=%s)",
                 estate, data.get("risk_level"))
        return data
    except Exception as exc:
        log.exception("[forecast] intelligence failed")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Intelligence analysis failed: {exc}"},
        )
