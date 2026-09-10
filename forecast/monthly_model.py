"""
Phase 1 — Monthly estate FFB forecast (cleaned target).

Supersedes phase5_models.py. Key changes vs phase5:
  * Target is the cleaned `bunches_total` from merge_block_monthly.py:
      - fully-recorded months: raw recorded total
      - under-recorded months (>=10 & <20 days): seasonal-imputed
      - sparse months (<10 days) + partial endpoints: excluded from train/score
        (still seasonally filled so AR lags stay contiguous)
  * Naive day-scaling of the target is GONE (it inflated 2-6 day months up to
    14x and caused ~41% of all backtest error).
  * Completeness columns (completeness_scale, n_harvest_days, is_underrecorded)
    are now MODEL FEATURES, not target transforms — ablated below.
  * Block-level LightGBM panel is retired: it lost the phase5 ablation and
    block-level seasonal imputation is infeasible. Estate-level only.

Models (all estate level, 1-step expanding-window walk-forward):
  1 SeasonalNaive   month-of-year mean              (MASE benchmark)
  2 Trailing3       mean of last 3 modelled months
  3 SARIMAX         (1,1,1) + month sin/cos exog    (on contiguous imputed series)
  4 LightGBM        base AR + weather features
  5 LightGBM+comp   base + completeness features    (answers: does it help?)

Outputs: predictions_monthly.csv, forecast_monthly_next3.csv, monthly_results.txt,
         monthly_model.joblib (final model + metadata for the live app).
Importable: load_estate(), backtest(), fit_final(), forecast_next().
"""

import os as _os
import warnings
import numpy as np
import pandas as pd
import statsmodels.api as sm
from lightgbm import LGBMRegressor
import joblib

warnings.simplefilter("ignore")

_DIR = _os.path.dirname(_os.path.abspath(__file__))
ESTATE_FILE = _os.path.join(_DIR, "features_estate_monthly.csv")
WEATHER_FILE = _os.path.join(_DIR, "weather_nasa_power_history.csv")
WORKDONE_FILE = _os.path.join(_DIR, _os.pardir, "workdone_daily.csv")
TARGET = "bunches_total"
MIN_TRAIN = 6
WORKDONE_COLS = ["prune_md_lag1m", "prune_qty_lag5m", "fert_qty_lag3m"]

# Shallow, strongly-regularised, FIXED rounds (no early stopping). With only
# ~30 usable months a tail-validation early-stop quit at 1 tree -> the model
# collapsed to a near-constant (pred std ~3.8k vs actual ~15k) and ignored every
# feature. Depth-2 stumps on all training months actually learn the signal
# (standalone LightGBM sMAPE 13.5% -> ~11-12%) without overfitting 30 points.
LGB_PARAMS = dict(
    n_estimators=100, learning_rate=0.05, num_leaves=3, max_depth=2,
    min_child_samples=8, subsample=0.8, colsample_bytree=0.8,
    reg_lambda=2.0, random_state=42, verbose=-1,
)
BASE_FEATS = ["month_sin", "month_cos", "y_lag1", "y_lag2", "y_lag3", "y_lag12",
              "y_roll3_mean", "y_roll6_mean", "rain_lag1m", "wb_lag1m",
              "rain_flower_5_7m"]
COMP_FEATS = BASE_FEATS + ["completeness_scale", "n_harvest_days", "is_underrecorded"]
# Adds monthly-lagged temperature and humidity (strongest signals from correlation analysis)
WEATHER_FEATS = BASE_FEATS + ["temp_lag1m", "humid_lag1m"]
# Adds estate-management effort (lagged pruning + fertilizer from workdone_daily.csv)
WORKDONE_FEATS = WEATHER_FEATS + WORKDONE_COLS


# ── Load ─────────────────────────────────────────────────────────────────────
def build_workdone_monthly(path=WORKDONE_FILE, known_through=None, extend=0):
    """Estate-month management effort as strictly-lagged features.

    Mirrors merge_block_monthly.build_workdone_monthly so the live app can
    rebuild these features (e.g. for forward months) without the merge step.
    Gaps stay NaN (unrecorded != zero); lags only, so no contemporaneous leak.

    known_through (Period) truncates the raw records to months <= it, and
    extend adds that many future months to the index, so the walk-forward
    backtest sees exactly the information available at each origin (lags into
    the future stay NaN where the source month is unknown).
    """
    wd = pd.read_csv(path)
    wd["date"] = pd.to_datetime(wd["date"], format="%m/%d/%Y")
    wd["month"] = wd["date"].dt.to_period("M")
    if known_through is not None:
        wd = wd[wd["month"] <= known_through]
    wn = wd["work_name"].str.upper()
    prune = (wd[wn.str.contains("FROND PRUNING")]
             .groupby("month").agg(prune_qty=("quantity", "sum"),
                                   prune_md=("mandays", "sum")))
    fert = (wd[wn.str.contains("FERTILIZER")]
            .groupby("month").agg(fert_qty=("quantity", "sum")))
    idx = pd.period_range(wd["month"].min(), wd["month"].max() + extend, freq="M")
    m = pd.DataFrame(index=idx)
    m.index.name = "month"
    m = m.join(prune).join(fert)
    m["prune_md_lag1m"] = m["prune_md"].shift(1)
    m["prune_qty_lag5m"] = m["prune_qty"].shift(5)
    m["fert_qty_lag3m"] = m["fert_qty"].shift(3)
    return m[WORKDONE_COLS]


def load_estate(path=ESTATE_FILE):
    df = pd.read_csv(path)
    df["month"] = pd.PeriodIndex(df["month"], freq="M")
    df = df.sort_values("month").reset_index(drop=True)
    # Derive lagged temp/humidity from monthly columns (valid: uses only month t-1 info)
    if "temp_mo" in df.columns and "temp_lag1m" not in df.columns:
        df["temp_lag1m"] = df["temp_mo"].shift(1)
    if "humid_mo" in df.columns and "humid_lag1m" not in df.columns:
        df["humid_lag1m"] = df["humid_mo"].shift(1)
    # Attach workdone effort features if the CSV predates them.
    if not all(c in df.columns for c in WORKDONE_COLS) and _os.path.exists(WORKDONE_FILE):
        wd = build_workdone_monthly().reset_index()
        df = df.merge(wd, on="month", how="left")
    return df


def apply_weather(df, wmonth):
    """Refresh df's weather-derived columns from the authoritative daily weather CSV.

    Without this, backtest() consumed only the weather columns baked into
    features_estate_monthly.csv, so swapping weather_nasa_power_history.csv left
    every scoreboard figure bit-identical -- weather reached the served forward
    forecast and the conformal intervals, but never the metric used to validate
    them, and a corrupt weather file was undetectable by the scoreboard.

    Lag discipline is unchanged: build_weather_monthly() derives every feature by
    shifting past months forward, so no row sees its own or a later month.
    """
    if wmonth is None or len(wmonth) == 0:
        return df
    # reindex() inside build_weather_monthly drops the index name, so set it back.
    w = wmonth.rename_axis("month").reset_index()
    cols = [c for c in w.columns if c != "month"]
    df = df.drop(columns=[c for c in cols if c in df.columns])
    return df.merge(w, on="month", how="left")


def build_weather_monthly(path=WEATHER_FILE, known_through=None, extend=0):
    """Monthly weather aggregates + lagged features.

    known_through / extend mirror build_workdone_monthly: raw daily weather is
    truncated to months <= known_through and the index is extended `extend`
    months past the data so lag features (and the climatological temp/humid
    fallback) are available for forecast months without peeking at weather
    recorded after the origin.
    """
    w = pd.read_csv(path, parse_dates=["date"])
    w["month"] = w["date"].dt.to_period("M")
    if known_through is not None:
        w = w[w["month"] <= known_through]
    wm = w.groupby("month").agg(
        rain_mo=("rainfall_mm", "sum"), et0_mo=("et0_mm", "sum"),
        wb_mo=("water_balance_mm", "sum"), solar_mo=("solar_radiation", "mean"),
        temp_mo=("temp_mean_c", "mean"), humid_mo=("humidity_pct", "mean"),
    )
    wm = wm.reindex(pd.period_range(wm.index.min(), wm.index.max() + extend, freq="M"))
    for k in (1, 2, 3):
        wm[f"rain_lag{k}m"] = wm["rain_mo"].shift(k)
        wm[f"wb_lag{k}m"] = wm["wb_mo"].shift(k)
    wm["rain_flower_5_7m"] = wm["rain_mo"].rolling(3).sum().shift(5)
    wm["wb_flower_5_7m"] = wm["wb_mo"].rolling(3).sum().shift(5)
    wm["temp_lag1m"] = wm["temp_mo"].shift(1)
    wm["humid_lag1m"] = wm["humid_mo"].shift(1)
    # Climatological monthly means — used as fallback for future months beyond weather coverage
    cal_month = pd.Series(wm.index.month, index=wm.index)
    temp_clim  = wm.groupby(cal_month)["temp_mo"].mean()
    humid_clim = wm.groupby(cal_month)["humid_mo"].mean()
    wm["temp_lag1m"]  = wm["temp_lag1m"].fillna(cal_month.map(temp_clim))
    wm["humid_lag1m"] = wm["humid_lag1m"].fillna(cal_month.map(humid_clim))
    return wm


# ── Metrics ──────────────────────────────────────────────────────────────────
def smape(a, p):
    a, p = np.asarray(a, float), np.asarray(p, float)
    d = np.abs(a) + np.abs(p)
    return float(np.mean(np.where(d == 0, 0.0, 2 * np.abs(p - a) / d)) * 100)


def score(actual, pred, naive_mae):
    err = np.asarray(pred, float) - np.asarray(actual, float)
    mae = float(np.mean(np.abs(err)))
    return dict(MAE=mae, RMSE=float(np.sqrt(np.mean(err ** 2))),
                sMAPE=smape(actual, pred), MASE=mae / naive_mae,
                Bias=float(np.mean(err)))


# ── LightGBM helper (fixed rounds, trained on every supplied month) ──────────
def _fit_lgb(X_tr, y_tr):
    m = LGBMRegressor(**LGB_PARAMS)
    m.fit(X_tr, y_tr)
    return m


# ── Backtest ─────────────────────────────────────────────────────────────────
def backtest(df, min_train=MIN_TRAIN):
    """Expanding-window 1-step walk-forward. Returns long predictions DataFrame."""
    idx = df.set_index("month")
    y = idx[TARGET]
    model_months = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    # contiguous interior series for SARIMAX (drop partial endpoints only)
    interior = df.loc[df["is_partial"] == 0, "month"].tolist()

    rows = []
    for i in range(min_train, len(model_months)):
        test_m = model_months[i]
        train_m = model_months[:i]                 # tree/naive targets
        actual = float(y.loc[test_m])

        # 1 Seasonal naive
        same = [y.loc[m] for m in train_m if m.month == test_m.month]
        pred_naive = float(np.mean(same)) if same else float(np.mean([y.loc[m] for m in train_m]))
        # 2 Trailing-3
        pred_tr3 = float(np.mean([y.loc[m] for m in train_m[-3:]]))
        # 3 SARIMAX on contiguous imputed series strictly before test_m
        sarimax_train = [m for m in interior if m < test_m]
        try:
            ytr = y.loc[sarimax_train].astype(float).values
            ex_tr = idx.loc[sarimax_train, ["month_sin", "month_cos"]].values
            ex_te = idx.loc[[test_m], ["month_sin", "month_cos"]].values
            res = sm.tsa.statespace.SARIMAX(
                ytr, exog=ex_tr, order=(1, 1, 1),
                enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
            pred_sx = float(res.forecast(steps=1, exog=ex_te)[0])
        except Exception:
            pred_sx = pred_tr3
        # 4-7 LightGBM variants (fixed rounds, fit on all months strictly < test_m)
        tr = idx.loc[train_m]
        preds_lgb = {}
        for name, feats in [("LightGBM", BASE_FEATS), ("LightGBM_comp", COMP_FEATS),
                             ("LightGBM_weather", WEATHER_FEATS),
                             ("LightGBM_workdone", WORKDONE_FEATS)]:
            mdl = _fit_lgb(tr[feats], tr[TARGET])
            preds_lgb[name] = float(np.clip(mdl.predict(idx.loc[[test_m], feats]), 0, None)[0])

        pred_ens = float(np.mean([pred_sx, pred_tr3, preds_lgb["LightGBM"]]))
        pred_ens_wx = float(np.mean([pred_sx, pred_tr3, preds_lgb["LightGBM_weather"]]))
        pred_ens_wd = float(np.mean([pred_sx, pred_tr3, preds_lgb["LightGBM_workdone"]]))
        for nm, p in [("SeasonalNaive", pred_naive), ("Trailing3", pred_tr3),
                      ("SARIMAX", pred_sx), ("LightGBM", preds_lgb["LightGBM"]),
                      ("LightGBM_comp", preds_lgb["LightGBM_comp"]),
                      ("LightGBM_weather", preds_lgb["LightGBM_weather"]),
                      ("LightGBM_workdone", preds_lgb["LightGBM_workdone"]),
                      ("Ensemble", pred_ens),
                      ("Ensemble_weather", pred_ens_wx),
                      ("Ensemble_workdone", pred_ens_wd)]:
            rows.append(dict(month=str(test_m), model=nm, actual=actual, pred=p))

    preds = pd.DataFrame(rows)
    preds["error"] = preds["pred"] - preds["actual"]
    return preds


def scoreboard(preds):
    naive_mae = preds.loc[preds.model == "SeasonalNaive", "error"].abs().mean()
    board = [dict(model=nm, **score(g["actual"], g["pred"], naive_mae))
             for nm, g in preds.groupby("model")]
    return pd.DataFrame(board).sort_values("MASE").reset_index(drop=True), naive_mae


# ── Final fit + recursive forward forecast ───────────────────────────────────
def fit_final(df, feats):
    idx = df.set_index("month")
    train_m = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    tr = idx.loc[train_m]
    return _fit_lgb(tr[feats], tr[TARGET])


def forecast_next(df, model, feats, wmonth, horizon=3, label="LightGBM", wdmonth=None,
                  feature_rows=None):
    """Recursive monthly forecast for all sub-models + Ensemble.

    Future months are assumed fully recorded. Each model recurses on its own
    predicted history for its lags. wdmonth (optional) supplies lagged workdone
    effort features for the future months. Returns columns:
    month, SARIMAX, Trailing3, <label>, Ensemble.

    feature_rows (optional list) collects the exact LightGBM feature row used
    for each forecast month ({"month": ..., <feature>: ...}) so the serving
    layer can compute TreeSHAP attributions on the same inputs.
    """
    idx = df.set_index("month")
    interior = df.loc[df["is_partial"] == 0, "month"].tolist()
    last = df.loc[df["exclude_from_model"] == 0, "month"].max()
    future = pd.period_range(last + 1, periods=horizon, freq="M")

    def roll(hist, m, k):
        v = [hist[m - j] for j in range(1, k + 1) if (m - j) in hist]
        return float(np.mean(v)) if v else np.nan

    # SARIMAX: fit once on the contiguous imputed interior series, multi-step.
    try:
        ytr = idx.loc[interior, TARGET].astype(float).values
        ex_tr = idx.loc[interior, ["month_sin", "month_cos"]].values
        ex_fc = np.array([[np.sin(2 * np.pi * m.month / 12),
                           np.cos(2 * np.pi * m.month / 12)] for m in future])
        sx = sm.tsa.statespace.SARIMAX(ytr, exog=ex_tr, order=(1, 1, 1),
                enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
        sx_fc = [float(v) for v in sx.forecast(steps=horizon, exog=ex_fc)]
    except Exception:
        sx_fc = [float(idx.loc[interior[-3:], TARGET].mean())] * horizon

    lgb_hist = idx[TARGET].to_dict()       # recursive history for LightGBM lags
    tr3_hist = idx[TARGET].to_dict()       # recursive history for Trailing-3

    out = []
    for step, m in enumerate(future):
        wr = wmonth.loc[m] if m in wmonth.index else pd.Series(dtype=float)
        wd = wdmonth.loc[m] if (wdmonth is not None and m in wdmonth.index) else pd.Series(dtype=float)
        feat = {
            "month_sin": np.sin(2 * np.pi * m.month / 12),
            "month_cos": np.cos(2 * np.pi * m.month / 12),
            "y_lag1": lgb_hist.get(m - 1, np.nan), "y_lag2": lgb_hist.get(m - 2, np.nan),
            "y_lag3": lgb_hist.get(m - 3, np.nan), "y_lag12": lgb_hist.get(m - 12, np.nan),
            "y_roll3_mean": roll(lgb_hist, m, 3), "y_roll6_mean": roll(lgb_hist, m, 6),
            "rain_lag1m": wr.get("rain_lag1m", np.nan), "wb_lag1m": wr.get("wb_lag1m", np.nan),
            "rain_flower_5_7m": wr.get("rain_flower_5_7m", np.nan),
            "temp_lag1m": wr.get("temp_lag1m", np.nan),
            "humid_lag1m": wr.get("humid_lag1m", np.nan),
            "prune_md_lag1m": wd.get("prune_md_lag1m", np.nan),
            "prune_qty_lag5m": wd.get("prune_qty_lag5m", np.nan),
            "fert_qty_lag3m": wd.get("fert_qty_lag3m", np.nan),
            "completeness_scale": 1.0, "n_harvest_days": 28, "is_underrecorded": 0,
        }
        lgb_p = float(np.clip(model.predict(pd.DataFrame([feat])[feats])[0], 0, None))
        if feature_rows is not None:
            feature_rows.append({"month": str(m), **{f: feat[f] for f in feats}})
        lgb_hist[m] = lgb_p
        tr3_p = roll(tr3_hist, m, 3)
        tr3_hist[m] = tr3_p
        sx_p = max(0.0, sx_fc[step])
        ens = float(np.mean([sx_p, tr3_p, lgb_p]))
        out.append({"month": str(m), "SARIMAX": round(sx_p), "Trailing3": round(tr3_p),
                    label: round(lgb_p), "Ensemble": round(ens)})
    return pd.DataFrame(out)


# ── Multi-horizon walk-forward backtest (conformal calibration data) ─────────
def backtest_multih(df, feats, horizon=12, label="LightGBM", min_train=MIN_TRAIN):
    """Walk-forward multi-step backtest of the served Ensemble pipeline.

    For every origin month (expanding window), refits SARIMAX + LightGBM on
    data <= origin and rolls the SAME recursive forecast the live app serves
    (forecast_next), with weather and workdone features rebuilt under
    information-at-origin discipline (masked after the origin, climatological
    fallbacks only from the past). Residuals are scored only where the target
    calendar month is a modelled (non-excluded) month.

    Returns a long DataFrame: origin, step, month, actual, pred — the
    calibration set for conformal.calibrate().
    """
    model_months = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    scoreable = set(model_months)
    y = df.set_index("month")[TARGET]
    have_wd = _os.path.exists(WORKDONE_FILE)

    rows = []
    for i in range(min_train - 1, len(model_months) - 1):
        origin = model_months[i]
        hist = df[df["month"] <= origin]
        wm_o = build_weather_monthly(known_through=origin, extend=horizon)
        wd_o = (build_workdone_monthly(known_through=origin, extend=horizon)
                if have_wd else None)
        mdl = fit_final(hist, feats)
        fwd = forecast_next(hist, mdl, feats, wm_o, horizon=horizon,
                            label=label, wdmonth=wd_o)
        for step, (_, r) in enumerate(fwd.iterrows(), start=1):
            m = pd.Period(r["month"], freq="M")
            if m in scoreable:
                rows.append(dict(origin=str(origin), step=step, month=str(m),
                                 actual=float(y.loc[m]), pred=float(r["Ensemble"])))
    return pd.DataFrame(rows)


def _load_sibling(name):
    """Import a sibling module by path (works both as a script run from this
    directory and when monthly_model.py itself was loaded by file path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"epms_forecast_{name}", _os.path.join(_DIR, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Script entry ─────────────────────────────────────────────────────────────
def train(estate_dir=_DIR, min_train=MIN_TRAIN, workdone_file=WORKDONE_FILE, estate_label="k3"):
    """Full train + backtest + conformal-calibrate + serve pipeline for one estate.

    estate_dir holds (or receives) features_estate_monthly.csv,
    weather_nasa_power_history.csv and every training artifact — forecast/ for k3
    (unchanged legacy layout), forecast/<ESTATE>/ for anything added since. min_train
    lets a thin-history estate (e.g. EC, ~4 usable months) run the same walk-forward
    machinery with fewer required folds instead of crashing on an empty scoreboard.
    """
    estate_file = _os.path.join(estate_dir, "features_estate_monthly.csv")
    weather_file = _os.path.join(estate_dir, "weather_nasa_power_history.csv")

    df = load_estate(estate_file)
    wmonth = build_weather_monthly(weather_file)
    # Make the weather CSV authoritative for the backtest, not just the forecast.
    df = apply_weather(df, wmonth)
    wdmonth = (build_workdone_monthly(workdone_file)
               if workdone_file and _os.path.exists(workdone_file) else None)
    n_excl = int((df["exclude_from_model"] == 1).sum())
    print(f"[{estate_label}] Estate months: {len(df)} | usable (train/score): "
          f"{len(df) - n_excl} | excluded: {n_excl}")

    preds = backtest(df, min_train=min_train)
    preds.to_csv(_os.path.join(estate_dir, "predictions_monthly.csv"), index=False)
    board, naive_mae = scoreboard(preds)
    n_folds = preds["month"].nunique()

    # Choose the best ensemble (by sMAPE) as the served model
    b = board.set_index("model")
    _candidates = [
        ("Ensemble",          BASE_FEATS,     "LightGBM"),
        ("Ensemble_weather",  WEATHER_FEATS,  "LightGBM_weather"),
        ("Ensemble_workdone", WORKDONE_FEATS, "LightGBM_workdone"),
    ]
    _avail = [(ens, f, lab) for ens, f, lab in _candidates if ens in b.index]
    served_ens, prod_feats, served_label = min(
        _avail, key=lambda c: b.loc[c[0], "sMAPE"])
    base_smape = b.loc["Ensemble", "sMAPE"] if "Ensemble" in b.index else 999

    final_model = fit_final(df, prod_feats)
    fwd = forecast_next(df, final_model, prod_feats, wmonth, horizon=3,
                        label=served_label, wdmonth=wdmonth)
    fwd.to_csv(_os.path.join(estate_dir, "forecast_monthly_next3.csv"), index=False)

    # ── Stage-1 R&D: conformal calibration on multi-step walk-forward residuals
    conformal = _load_sibling("conformal")
    print(f"[{estate_label}] Running multi-horizon walk-forward backtest for conformal calibration…")
    resid = backtest_multih(df, prod_feats, horizon=12, label=served_label, min_train=min_train)
    resid.to_csv(_os.path.join(estate_dir, "predictions_multih.csv"), index=False)
    conf = conformal.calibrate(resid, max_step=12)

    joblib.dump({"lgb_model": final_model, "features": prod_feats, "target": TARGET,
                 "served_model": f"Ensemble(SARIMAX+Trailing3+{served_label})",
                 "served_ensemble": served_ens, "served_label": served_label,
                 "served_smape": float(b.loc[served_ens, "sMAPE"]),
                 "served_mase": float(b.loc[served_ens, "MASE"]),
                 "n_folds": int(n_folds),
                 "trained_through": str(df.loc[df.exclude_from_model == 0, "month"].max()),
                 "conformal": conf,
                 "scoreboard": board}, _os.path.join(estate_dir, "monthly_model.joblib"))

    # ── report ───────────────────────────────────────────────────────────────
    def fb(d):
        o = d.copy()
        for c in ["MAE", "RMSE", "Bias"]:
            o[c] = o[c].map(lambda v: f"{v:,.0f}")
        o["sMAPE"] = o["sMAPE"].map(lambda v: f"{v:.1f}%")
        o["MASE"] = o["MASE"].map(lambda v: f"{v:.3f}")
        return o.to_string(index=False)

    b = board.set_index("model")
    lines = ["=" * 64, f"[{estate_label}] PHASE 1 RESULTS - monthly estate forecast (cleaned target)",
             "=" * 64,
             f"Test folds: {n_folds} (expanding window, 1-step-ahead, min_train={min_train})",
             f"Excluded (<10 days or partial): "
             f"{df.loc[df.exclude_from_model==1,'month'].astype(str).tolist()}",
             f"MASE benchmark: Seasonal Naive (MAE = {naive_mae:,.0f} bunches)", "",
             "SCOREBOARD (sorted by MASE, lower = better):", fb(board), "",
             "FEATURE IMPACT (ensemble sMAPE vs base; lower = better):",
             f"  Ensemble (base)    sMAPE {b.loc['Ensemble','sMAPE']:.1f}%  "
             f"MASE {b.loc['Ensemble','MASE']:.3f}"]
    for _ens, _desc in [("Ensemble_weather", "+ weather    "),
                        ("Ensemble_workdone", "+ workdone   ")]:
        if _ens in b.index:
            lines.append(
                f"  Ensemble {_desc} sMAPE {b.loc[_ens,'sMAPE']:.1f}%  "
                f"MASE {b.loc[_ens,'MASE']:.3f}"
                + ("  <- BETTER" if b.loc[_ens, 'sMAPE'] < base_smape
                   else "  <- no improvement"))
    lines.append(f"  >> Served: {served_ens} "
                 f"(sMAPE {b.loc[served_ens,'sMAPE']:.1f}%, MASE {b.loc[served_ens,'MASE']:.3f})")
    lines += ["", "OBSERVATIONS:"]
    lines.append(f"  1. Best model: {board.iloc[0]['model']} (MASE {board.iloc[0]['MASE']:.2f}, "
                 f"sMAPE {board.iloc[0]['sMAPE']:.1f}%).")
    if "LightGBM" in b.index and "LightGBM_comp" in b.index:
        d = b.loc["LightGBM", "MASE"] - b.loc["LightGBM_comp", "MASE"]
        verdict = "helps" if d > 0.01 else ("hurts" if d < -0.01 else "no material effect")
        lines.append(f"  2. completeness features: {verdict} "
                     f"(base MASE {b.loc['LightGBM','MASE']:.3f} vs "
                     f"+comp {b.loc['LightGBM_comp','MASE']:.3f}).")
    biases = board.set_index("model")["Bias"]
    lines.append(f"  3. Bias range across models: {biases.min():+,.0f} .. {biases.max():+,.0f} "
                 f"(was strongly negative before de-scaling).")
    lines.append(f"  4. Served model for the app: Ensemble({served_label}) "
                 f"(MASE {b.loc[served_ens,'MASE']:.3f}, sMAPE {b.loc[served_ens,'sMAPE']:.1f}%).")
    lines += ["", "FORWARD FORECAST - next 3 months (recursive, full-month assumption):",
              fwd.to_string(index=False)]

    # ── conformal calibration report ─────────────────────────────────────────
    nominal = int(round(conf["nominal"] * 100))
    lines += ["", "CONFORMAL PREDICTION INTERVALS "
              f"({nominal}% nominal, prequential/honest evaluation):",
              f"  Served method: {conf['served_method']} "
              f"(alpha={conf['alpha']}, ACI gamma={conf['gamma']})",
              "  step  n_resid  n_scored  split_cov  aci_cov  half-width(served)"]
    for h in sorted(conf["served_widths"]):
        st = conf["prequential"].get(h) or {}
        fmt_cov = lambda v: f"{v:.0%}" if v is not None else "  -  "
        lines.append(
            f"  {h:>4}  {conf['n_residuals'].get(h, 0):>7}  {st.get('n_scored', 0):>8}  "
            f"{fmt_cov(st.get('split_coverage')):>9}  {fmt_cov(st.get('aci_coverage')):>7}  "
            f"{conf['served_widths'][h]:>12,.0f}")
    cov13 = conf.get("coverage_h1_3")
    lines.append(f"  Steps 1-3 empirical coverage ({conf['served_method']}): "
                 f"{cov13:.0%}" if cov13 is not None else
                 "  Steps 1-3 empirical coverage: n/a")
    if not conf.get("coverage_ok", True):
        lines.append(f"  !! WARNING: coverage < 80% at {nominal}% nominal — "
                     "investigate calibration before trusting the bands.")

    with open(_os.path.join(estate_dir, "monthly_results.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))

    # ── MLflow tracking (best-effort: training still succeeds without it) ────
    try:
        import mlflow
        root = _os.path.dirname(_DIR)  # shared repo-wide store regardless of estate_dir
        mlflow.set_tracking_uri(
            "sqlite:///" + _os.path.join(root, "mlflow.db").replace(_os.sep, "/"))
        _art = "file:///" + _os.path.join(root, "mlartifacts").replace(_os.sep, "/")
        _exp = mlflow.get_experiment_by_name("ffb-monthly-forecast")
        exp_id = (_exp.experiment_id if _exp else
                  mlflow.create_experiment("ffb-monthly-forecast", artifact_location=_art))
        trained_through = str(df.loc[df.exclude_from_model == 0, "month"].max())
        with mlflow.start_run(experiment_id=exp_id,
                              run_name=f"{estate_label}_train_through_{trained_through}"):
            mlflow.log_params({**LGB_PARAMS,
                               "estate": estate_label,
                               "min_train": min_train,
                               "served_ensemble": served_ens,
                               "served_label": served_label,
                               "features": ",".join(prod_feats),
                               "n_features": len(prod_feats),
                               "conformal_alpha": conf["alpha"],
                               "conformal_gamma": conf["gamma"],
                               "conformal_served": conf["served_method"],
                               "trained_through": trained_through})
            for _, r in board.iterrows():
                tag = r["model"]
                mlflow.log_metrics({f"smape_{tag}": r["sMAPE"],
                                    f"mase_{tag}": r["MASE"],
                                    f"mae_{tag}": r["MAE"]})
            mlflow.log_metric("n_folds", n_folds)
            for h, st in conf["prequential"].items():
                if st.get("split_coverage") is not None:
                    mlflow.log_metric(f"conf_split_cov_h{h}", st["split_coverage"])
                if st.get("aci_coverage") is not None:
                    mlflow.log_metric(f"conf_aci_cov_h{h}", st["aci_coverage"])
            for h, w in conf["served_widths"].items():
                mlflow.log_metric(f"conf_width_h{h}", w)
            if cov13 is not None:
                mlflow.log_metric("conf_coverage_h1_3", cov13)
            for art in ("monthly_results.txt", "predictions_monthly.csv",
                        "predictions_multih.csv", "forecast_monthly_next3.csv"):
                p = _os.path.join(estate_dir, art)
                if _os.path.exists(p):
                    mlflow.log_artifact(p)
        print(f"\n[{estate_label}] MLflow: run logged (experiment ffb-monthly-forecast, "
              f"store sqlite:///{root}/mlflow.db)")
    except Exception as exc:  # mlflow not installed / store locked — not fatal
        print(f"\n[{estate_label}] MLflow logging skipped: {exc}")

    print(f"\n[{estate_label}] Wrote: predictions_monthly.csv, predictions_multih.csv, "
          f"forecast_monthly_next3.csv, monthly_results.txt, monthly_model.joblib -> {estate_dir}")


def main():
    train(_DIR, MIN_TRAIN, WORKDONE_FILE, "k3")


if __name__ == "__main__":
    main()
