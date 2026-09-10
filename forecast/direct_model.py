"""
Direct (non-recursive) multi-horizon monthly FFB forecast.

WHY THIS EXISTS
---------------
The served pipeline (monthly_model.forecast_next) is *recursive*: to reach
step h it feeds its own step h-1 prediction back in as `y_lag1`. Error
therefore compounds across the 3-month horizon the dashboard actually shows.
The TimesFM benchmark (forecast/timesfm_experiment.py) made the cost visible --
the incumbent degrades 10.5 -> 13.7 -> 15.5 sMAPE across h1..h3 while a
direct multi-horizon model stays roughly flat. The gap at h3 was significant
(paired t=+2.67, p=0.013); at h1 the two were indistinguishable.

That is an *architectural* difference, not a knowledge one, so it can be
captured here without taking on a foundation-model dependency -- and unlike a
zero-shot model this keeps LightGBM's SHAP attributions intact.

APPROACH
--------
LightGBM maps information-at-origin -> y[origin+h] with no feedback loop. Two
disciplines make it honest:

  * Autoregressive features are ORIGIN-relative (y at the origin, y at the
    origin-1, ...) and are always real observations. The recursive model's
    `y_lag1` at h=3 is a prediction of a prediction; here there is no such
    thing.

  * Exogenous features are dropped per horizon by lag depth. A feature whose
    source window ends after the origin cannot be known at forecast time, so
    training must not see it either (`_feats_for_horizon`). rain_lag1m is
    available at h=1 and gone by h=2; rain_flower_5_7m survives to h=5. This
    is what keeps train/serve feature availability identical instead of
    training on observed weather and serving on NaN.

PER-HORIZON vs POOLED -- the result that decided the design
-----------------------------------------------------------
The textbook direct formulation fits one model per horizon (`fit_direct`). On
k3 that LOST to the recursive incumbent: 14.41 sMAPE vs 13.16 over steps 1-3.
With ~30 usable months, giving each horizon its own ~28 training rows costs
more in variance than the recursion was costing in bias.

Pooling every (origin, h) pair into a single fit with `h` as a feature
(`fit_pooled`) keeps the direct discipline and shares statistical strength
across horizons. That is what actually works -- 11.97 vs 13.16, and the error
profile flattens from 10.5/13.7/15.5 to 10.0/12.5/13.6 across h1/h2/h3. So
`fit_pooled` is the default ensemble member; `fit_direct` is retained as the
diagnostic arm that demonstrates why.

The gain is NOT statistically significant on 30 origins (clustered t=+1.60,
p=0.120). Read it as "the shape of the error curve is fixed and the point
estimate improved", not as a proven accuracy win.

Ensemble_direct = mean(SARIMAX multi-step, flat Trailing-3, pooled LightGBM).
Trailing-3 is held flat rather than recursed onto its own output -- recursing a
trailing mean just decays it toward a constant and is not a real forecast.
"""

import os as _os
import warnings
import numpy as np
import pandas as pd
import statsmodels.api as sm

warnings.simplefilter("ignore")

_DIR = _os.path.dirname(_os.path.abspath(__file__))


def _mm():
    """Import monthly_model by path (works as script or as an imported module)."""
    import importlib.util
    import sys
    if "epms_monthly_model" in sys.modules:
        return sys.modules["epms_monthly_model"]
    spec = importlib.util.spec_from_file_location(
        "epms_monthly_model", _os.path.join(_DIR, "monthly_model.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epms_monthly_model"] = mod
    spec.loader.exec_module(mod)
    return mod


MM = _mm()
TARGET = MM.TARGET

# Origin-relative autoregressive features -- always real observations at any h.
AR_FEATS = ["y_o0", "y_o1", "y_o2", "y_roll3_o", "y_roll6_o", "y_sm12"]

# Lag depth of every exogenous feature: the number of months back from the
# TARGET month that its source window ends. A feature is usable at horizon h
# only when depth >= h, otherwise its window runs past the origin.
FEAT_LAG = {
    "month_sin": 0, "month_cos": 0,          # deterministic calendar, always known
    "rain_lag1m": 1, "wb_lag1m": 1,
    "temp_lag1m": 1, "humid_lag1m": 1,
    "rain_flower_5_7m": 5,
    "prune_md_lag1m": 1, "prune_qty_lag5m": 5, "fert_qty_lag3m": 3,
}
EXO_FEATS = list(FEAT_LAG)


def _feats_for_horizon(h, exo=EXO_FEATS):
    """Features whose information is genuinely available h months ahead.

    month_sin/cos have depth 0 but are deterministic, so they are kept at every
    horizon; everything else needs depth >= h.
    """
    keep = [f for f in exo
            if f in ("month_sin", "month_cos") or FEAT_LAG.get(f, 0) >= h]
    return AR_FEATS + keep


def _row(y, origin, target, wrow, wdrow):
    """One direct-model feature row for (origin -> target)."""
    def at(p):
        v = y.get(p, np.nan)
        return float(v) if v == v else np.nan

    def roll(k):
        v = [y[origin - j] for j in range(0, k) if (origin - j) in y]
        return float(np.mean(v)) if v else np.nan

    return {
        "month_sin": np.sin(2 * np.pi * target.month / 12),
        "month_cos": np.cos(2 * np.pi * target.month / 12),
        "y_o0": at(origin), "y_o1": at(origin - 1), "y_o2": at(origin - 2),
        "y_roll3_o": roll(3), "y_roll6_o": roll(6),
        "y_sm12": at(target - 12),
        "rain_lag1m": wrow.get("rain_lag1m", np.nan),
        "wb_lag1m": wrow.get("wb_lag1m", np.nan),
        "temp_lag1m": wrow.get("temp_lag1m", np.nan),
        "humid_lag1m": wrow.get("humid_lag1m", np.nan),
        "rain_flower_5_7m": wrow.get("rain_flower_5_7m", np.nan),
        "prune_md_lag1m": wdrow.get("prune_md_lag1m", np.nan),
        "prune_qty_lag5m": wdrow.get("prune_qty_lag5m", np.nan),
        "fert_qty_lag3m": wdrow.get("fert_qty_lag3m", np.nan),
    }


def _wget(frame, m):
    if frame is None or m not in frame.index:
        return pd.Series(dtype=float)
    return frame.loc[m]


def build_direct_training(df, wmonth, wdmonth, h, max_target=None):
    """Training pairs (origin, origin+h) for the horizon-h model.

    Only pairs whose TARGET month is <= max_target are used, so a backtest at
    origin O never trains on an outcome it could not have observed by O.
    """
    y = df.set_index("month")[TARGET].to_dict()
    scoreable = set(df.loc[df["exclude_from_model"] == 0, "month"])
    months = sorted(y)
    rows, ys = [], []
    for origin in months:
        target = origin + h
        if target not in scoreable:
            continue
        if max_target is not None and target > max_target:
            continue
        if (origin - 2) not in y:                     # need a minimal AR window
            continue
        rows.append(_row(y, origin, target, _wget(wmonth, target), _wget(wdmonth, target)))
        ys.append(float(y[target]))
    return pd.DataFrame(rows), np.asarray(ys, float)


def fit_direct(df, wmonth, wdmonth, horizon=3, max_target=None, min_pairs=6, exo=EXO_FEATS):
    """Fit one LightGBM per horizon. Returns {h: (model, feats)}.

    A horizon with too few training pairs is left out; callers fall back to the
    ensemble's other members for that step rather than fitting a model on noise.
    """
    models = {}
    for h in range(1, horizon + 1):
        X, yv = build_direct_training(df, wmonth, wdmonth, h, max_target=max_target)
        if len(X) < min_pairs:
            continue
        feats = _feats_for_horizon(h, exo)
        models[h] = (MM._fit_lgb(X[feats], yv), feats)
    return models


def build_pooled_training(df, wmonth, wdmonth, horizon, max_target=None, exo=EXO_FEATS):
    """All (origin, h) pairs stacked into ONE frame with `h` as a feature.

    Per-horizon models are the textbook direct formulation, but with ~30 usable
    months each horizon gets its own ~28 rows and the split costs more in
    variance than the recursion costs in bias. Pooling shares one fit across
    every horizon while keeping the direct discipline: a feature unavailable at
    horizon h is written as NaN in the training row too, so what the model sees
    while training is exactly what it will see at serve time.
    """
    y = df.set_index("month")[TARGET].to_dict()
    scoreable = set(df.loc[df["exclude_from_model"] == 0, "month"])
    rows, ys = [], []
    for origin in sorted(y):
        if (origin - 2) not in y:
            continue
        for h in range(1, horizon + 1):
            target = origin + h
            if target not in scoreable:
                continue
            if max_target is not None and target > max_target:
                continue
            r = _row(y, origin, target, _wget(wmonth, target), _wget(wdmonth, target))
            for f in exo:                       # mask what this horizon cannot know
                if f not in ("month_sin", "month_cos") and FEAT_LAG.get(f, 0) < h:
                    r[f] = np.nan
            r["h"] = float(h)
            rows.append(r)
            ys.append(float(y[target]))
    return pd.DataFrame(rows), np.asarray(ys, float)


def fit_pooled(df, wmonth, wdmonth, horizon=3, max_target=None, min_pairs=10,
               exo=EXO_FEATS):
    """One LightGBM over all horizons. Returns (model, feats) or None."""
    X, yv = build_pooled_training(df, wmonth, wdmonth, horizon,
                                  max_target=max_target, exo=exo)
    if len(X) < min_pairs:
        return None
    feats = AR_FEATS + ["h"] + [f for f in exo]
    return MM._fit_lgb(X[feats], yv), feats


def forecast_direct(df, models, wmonth, wdmonth, horizon=3, feature_rows=None,
                    pooled=None, ensemble_source="pooled"):
    """Direct forward forecast from the last modelled month.

    Returns month, SARIMAX, Trailing3, LightGBM_direct, Ensemble_direct -- the
    same shape monthly_model.forecast_next returns, so it drops into the same
    conformal/serving plumbing.
    """
    idx = df.set_index("month")
    y = idx[TARGET].to_dict()
    interior = df.loc[df["is_partial"] == 0, "month"].tolist()
    origin = df.loc[df["exclude_from_model"] == 0, "month"].max()
    future = pd.period_range(origin + 1, periods=horizon, freq="M")

    # SARIMAX is already a true multi-step forecast -- reused unchanged.
    try:
        ytr = idx.loc[interior, TARGET].astype(float).values
        ex_tr = idx.loc[interior, ["month_sin", "month_cos"]].values
        ex_fc = np.array([[np.sin(2 * np.pi * m.month / 12),
                           np.cos(2 * np.pi * m.month / 12)] for m in future])
        sx = sm.tsa.statespace.SARIMAX(ytr, exog=ex_tr, order=(1, 1, 1),
                enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
        sx_fc = [float(v) for v in sx.forecast(steps=horizon, exog=ex_fc)]
    except Exception:
        sx_fc = [float(np.mean([y[m] for m in interior[-3:]]))] * horizon

    # Trailing-3 held FLAT at the origin -- not recursed onto its own output.
    tr3 = float(np.mean([y[origin - j] for j in range(0, 3) if (origin - j) in y]))

    out = []
    for step, m in enumerate(future, start=1):
        sx_p = max(0.0, sx_fc[step - 1])
        members = [sx_p, tr3]
        lgb_p = None
        if step in models:
            mdl, feats = models[step]
            row = _row(y, origin, m, _wget(wmonth, m), _wget(wdmonth, m))
            lgb_p = float(np.clip(mdl.predict(pd.DataFrame([row])[feats])[0], 0, None))
            members.append(lgb_p)
            if feature_rows is not None:
                feature_rows.append({"month": str(m), "step": step,
                                     **{f: row[f] for f in feats}})
        pool_p = None
        if pooled is not None:
            pmdl, pfeats = pooled
            prow = _row(y, origin, m, _wget(wmonth, m), _wget(wdmonth, m))
            for f in EXO_FEATS:
                if f not in ("month_sin", "month_cos") and FEAT_LAG.get(f, 0) < step:
                    prow[f] = np.nan
            prow["h"] = float(step)
            pool_p = float(np.clip(pmdl.predict(pd.DataFrame([prow])[pfeats])[0], 0, None))
            if feature_rows is not None and lgb_p is None:
                feature_rows.append({"month": str(m), "step": step,
                                     **{f: prow[f] for f in pfeats}})

        # The served ensemble keeps the incumbent's recipe (SARIMAX + Trailing3
        # + LightGBM) and changes only the LightGBM member. `pooled` is the
        # default because per-horizon models lost to it on every fold set --
        # 30 months is too little history to spend on separate fits.
        if ensemble_source == "pooled" and pool_p is not None:
            ens_members = [sx_p, tr3, pool_p]
        else:
            ens_members = members
        out.append({"month": str(m), "SARIMAX": round(sx_p), "Trailing3": round(tr3),
                    "LightGBM_direct": round(lgb_p) if lgb_p is not None else None,
                    "LightGBM_pooled": round(pool_p) if pool_p is not None else None,
                    "Ensemble_direct": round(float(np.mean(ens_members)))})
    return pd.DataFrame(out)


def backtest_multih_direct(df, wmonth_file, wdmonth_file, horizon=12,
                           min_train=MM.MIN_TRAIN, exo=EXO_FEATS, verbose=False,
                           ensemble_source="pooled"):
    """Walk-forward multi-horizon backtest of the DIRECT pipeline.

    Mirrors monthly_model.backtest_multih fold-for-fold so the two are directly
    comparable: same origins, same information-at-origin weather/workdone
    rebuild, same scoreable-month filter. The one difference is that per-horizon
    models replace the recursive roll-forward.

    NOTE the weather path is passed explicitly. backtest_multih() omits it and
    silently falls back to the module-level k3 WEATHER_FILE, which is why EC's
    conformal calibration is calibrated against K3 weather.
    """
    model_months = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    scoreable = set(model_months)
    y = df.set_index("month")[TARGET]
    have_wd = wdmonth_file is not None and _os.path.exists(wdmonth_file)

    rows = []
    for i in range(min_train - 1, len(model_months) - 1):
        origin = model_months[i]
        hist = df[df["month"] <= origin]
        wm_o = MM.build_weather_monthly(wmonth_file, known_through=origin, extend=horizon)
        wd_o = (MM.build_workdone_monthly(wdmonth_file, known_through=origin, extend=horizon)
                if have_wd else None)
        # train only on outcomes observable by this origin
        models = fit_direct(hist, wm_o, wd_o, horizon=horizon,
                            max_target=origin, exo=exo)
        pooled = fit_pooled(hist, wm_o, wd_o, horizon=horizon,
                            max_target=origin, exo=exo)
        fwd = forecast_direct(hist, models, wm_o, wd_o, horizon=horizon,
                              pooled=pooled, ensemble_source=ensemble_source)
        if verbose:
            print(f"  origin {origin}: {len(models)} horizon models")
        for step, (_, r) in enumerate(fwd.iterrows(), start=1):
            m = pd.Period(r["month"], freq="M")
            if m in scoreable:
                rows.append(dict(origin=str(origin), step=step, month=str(m),
                                 actual=float(y.loc[m]),
                                 pred=float(r["Ensemble_direct"]),
                                 pred_lgb=(float(r["LightGBM_direct"])
                                           if r["LightGBM_direct"] is not None else np.nan),
                                 pred_pooled=(float(r["LightGBM_pooled"])
                                              if r["LightGBM_pooled"] is not None else np.nan),
                                 pred_sarimax=float(r["SARIMAX"]),
                                 pred_tr3=float(r["Trailing3"])))
    return pd.DataFrame(rows)
