"""
TimesFM benchmark harness -- can a zero-shot foundation model beat the incumbent?

Offline R&D only. Writes nothing the app reads: no monthly_model.joblib, no
change to /forecast/data, no change to forecast.html. Artifacts land in
forecast/experiments/timesfm/ and MLflow experiment "ffb-timesfm-rd".

The bar (k3): Ensemble(SARIMAX+Trailing3+LightGBM_workdone), sMAPE 10.07 /
MASE 0.572 over 30 walk-forward folds.

Two tracks
----------
monthly  36 usable monthly points. The only apples-to-apples comparison, and
         structurally the hardest for TimesFM (see "the fold handicap" below).
daily    ~1,242 daily points, forecast daily and summed to monthly totals.
         Scored on the SAME monthly scale, so it lands on the same scoreboard.
         This is where TimesFM has real headroom -- 40x the observations, in
         the long-context regime it was designed for.

The fold handicap, and why two numbers are always reported
----------------------------------------------------------
backtest_multih starts origins at model_months[min_train-1] = index 5, so the
first ~20 of the 30 folds hand TimesFM a context of 6-25 points -- below its
own documented 32-point minimum -- while those folds are averaged into the
incumbent's 10.07%. So every result is reported twice:

  headline  all 30 folds, the honest apples-to-apples answer
  matched   folds with n_context >= MATCHED_MIN_CONTEXT

and the INCUMBENT IS RE-SCORED ON THE IDENTICAL SUBSET for the matched number.
Comparing TimesFM-on-late-folds against incumbent-on-all-folds would be the
easiest silent cheat available here: late folds have more history and are
easier for every model. The matched number never appears without the headline.

Leakage discipline
------------------
Contexts are always sliced `df[df.month <= origin]` before anything downstream
sees them, and covariates are built with known_through=origin, mirroring
monthly_model.backtest_multih. A covariate channel may be past_future ONLY if
it is non-NaN across the whole horizon; anything partially defined is demoted
to past_only automatically (see _split_covariates).

Usage:
    python forecast/timesfm_experiment.py --track monthly --backend 3.0
    python forecast/timesfm_experiment.py --track monthly --backend 3.0 --covariates
    python forecast/timesfm_experiment.py --track daily   --backend 3.0
    python forecast/timesfm_experiment.py --track both --backend both --covariates
"""

import argparse
import importlib.util
import os
import time

import numpy as np
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_DIR)
OUT_DIR = os.path.join(_DIR, "experiments", "timesfm")

ESTATE_DIRS = {"k3": _DIR, "ec": os.path.join(_DIR, "EC")}
HORIZON = 12
MIN_TRAIN = 6
MATCHED_MIN_CONTEXT = 24     # folds at/above this are "matched context"
MLFLOW_EXPERIMENT = "ffb-timesfm-rd"

# Pinned so the benchmark is reproducible -- a zero-shot result is only
# meaningful if the weights are fixed. From ~/.cache/huggingface snapshots.
REVISIONS = {"3.0": "43046b85ec22d584a13f8098c2ed39c889e129c2", "2.5": None}


def _load_by_path(name, path):
    """Import a module by file path -- the pattern forecast_router already uses."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mm = _load_by_path("epms_monthly_model", os.path.join(_DIR, "monthly_model.py"))
conformal = _load_by_path("epms_conformal", os.path.join(_DIR, "conformal.py"))
tfm = _load_by_path("epms_timesfm_model", os.path.join(_DIR, "timesfm_model.py"))
daily_mod = _load_by_path("epms_daily_series",
                          os.path.join(_DIR, "build_daily_series.py"))


# -- covariates ---------------------------------------------------------------
CANDIDATE_COVS = [
    "rain_mo", "et0_mo", "wb_mo", "solar_mo", "temp_mo", "humid_mo",
    "rain_lag1m", "wb_lag1m", "rain_lag2m", "wb_lag2m", "rain_lag3m", "wb_lag3m",
    "rain_flower_5_7m", "wb_flower_5_7m", "temp_lag1m", "humid_lag1m",
]


def _split_covariates(wm, hist_months, future_months, candidates=CANDIDATE_COVS):
    """Partition covariate channels by whether they are known across the horizon.

    A channel qualifies as past_future ONLY if its future segment is fully
    non-NaN under build_weather_monthly(known_through=origin, extend=H). This
    is a hard rule, not a convention: handing TimesFM a past_future channel
    with a trailing NaN block makes it interpolate values out of thin air,
    which looks like it works and quietly degrades the result.

    Under known_through=origin the raw aggregates (rain_mo, et0_mo, ...) are
    NaN for every future month, so they land in past_only. The lag columns are
    defined only for the first k steps. Only the calendar terms and the
    climatology-backfilled temp_lag1m/humid_lag1m survive as past_future.

    Returns (past_future (C1, L+H), past_only (C2, L), names_pf, names_po).
    """
    all_m = list(hist_months) + list(future_months)
    cal = pd.DataFrame(index=pd.PeriodIndex(all_m, freq="M"))
    cal["month_sin"] = np.sin(2 * np.pi * cal.index.month / 12)
    cal["month_cos"] = np.cos(2 * np.pi * cal.index.month / 12)

    pf_names, po_names = ["month_sin", "month_cos"], []
    pf_cols = [cal["month_sin"].to_numpy(), cal["month_cos"].to_numpy()]
    po_cols = []

    for c in candidates:
        if c not in wm.columns:
            continue
        fut = wm.reindex(pd.PeriodIndex(future_months, freq="M"))[c]
        hist = wm.reindex(pd.PeriodIndex(hist_months, freq="M"))[c]
        if hist.isna().all():
            continue
        if fut.notna().all():
            pf_names.append(c)
            pf_cols.append(np.concatenate([hist.to_numpy(), fut.to_numpy()]))
        else:
            po_names.append(c)
            po_cols.append(hist.to_numpy())

    def _fill(cols):
        """Backfill any residual NaN in the HISTORY segment (early lags)."""
        out = []
        for a in cols:
            a = np.asarray(a, dtype=float)
            if np.isnan(a).any():
                idx = np.arange(a.size)
                ok = ~np.isnan(a)
                if not ok.any():
                    continue
                a = np.interp(idx, idx[ok], a[ok])
            out.append(a)
        return out

    pf = np.vstack(_fill(pf_cols)) if pf_cols else None
    po = np.vstack(_fill(po_cols)) if po_cols else None
    # Scale each channel to unit variance so no single channel dominates purely
    # by magnitude (rain_mo is ~1e2, wb_mo ~1e2, month_sin ~1e0).
    for m in (pf, po):
        if m is not None:
            sd = m.std(axis=1, keepdims=True)
            sd[sd == 0] = 1.0
            m -= m.mean(axis=1, keepdims=True)
            m /= sd
    return pf, po, pf_names, po_names


# -- monthly track ------------------------------------------------------------
def run_monthly(estate="k3", backend="3.0", horizon=HORIZON, min_train=MIN_TRAIN,
                use_cov=False):
    """Walk-forward multi-horizon backtest, mirroring monthly_model.backtest_multih.

    All origins are prefixes of one series, so every origin goes into a SINGLE
    predict_batch call -- one model load, one batched forward pass. TimesFM
    decodes the whole horizon non-autoregressively, so horizon is nearly free
    compared to the incumbent's recursive rollout.
    """
    est_dir = ESTATE_DIRS[estate]
    df = mm.load_estate(os.path.join(est_dir, "features_estate_monthly.csv"))
    wm_full = mm.build_weather_monthly(
        os.path.join(est_dir, "weather_nasa_power_history.csv"))
    df = mm.apply_weather(df, wm_full)

    model_months = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    scoreable = set(model_months)
    y = df.set_index("month")[mm.TARGET]

    origins, contexts, futures, pfs, pos = [], [], [], [], []
    for i in range(min_train - 1, len(model_months) - 1):
        origin = model_months[i]
        hist = df[df["month"] <= origin]                 # slice ONCE, up front
        hist_months = hist["month"].tolist()
        future_months = list(pd.period_range(origin + 1, periods=horizon, freq="M"))

        origins.append(origin)
        contexts.append(hist[mm.TARGET].to_numpy(dtype=float))
        futures.append(future_months)

        if use_cov:
            # information-at-origin: truncate weather to <= origin, extend forward.
            # NOTE the explicit path -- backtest_multih omits it and silently
            # falls back to k3's weather file for every estate.
            wm_o = mm.build_weather_monthly(
                os.path.join(est_dir, "weather_nasa_power_history.csv"),
                known_through=origin, extend=horizon)
            pf, po, pf_n, po_n = _split_covariates(wm_o, hist_months, future_months)
            pfs.append(pf)
            pos.append(po)

    kw = {}
    if backend == "2.5":
        # 2.5 pads every context to max_context, so keep it tight for monthly.
        kw = dict(max_context=64, max_horizon=max(16, horizon))
    if REVISIONS.get(backend):
        kw["revision"] = REVISIONS[backend]

    t0 = time.perf_counter()
    res = tfm.forecast_batch(
        contexts, horizon, backend=backend,
        past_future=pfs if use_cov else None,
        past_only=pos if use_cov else None, **kw)
    elapsed = time.perf_counter() - t0

    rows = []
    for origin, fut, r in zip(origins, futures, res):
        for step, m in enumerate(fut, start=1):
            if m in scoreable:
                rows.append(dict(origin=str(origin), step=step, month=str(m),
                                 actual=float(y.loc[m]), pred=float(r["point"][step - 1]),
                                 n_context=int(r["n_context"])))
    meta = {"elapsed_s": elapsed, "n_origins": len(origins),
            "cov_past_future": pf_n if use_cov else [],
            "cov_past_only": po_n if use_cov else []}
    return pd.DataFrame(rows), meta


# -- daily track --------------------------------------------------------------
def run_daily(estate="k3", backend="3.0", horizon=HORIZON, min_train=MIN_TRAIN,
              gap_fill="zero", point="mean"):
    """Forecast the daily series, sum to monthly totals, score on the monthly scale.

    Origins are the last calendar day of each monthly origin, so folds line up
    1:1 with the monthly track and the two are directly comparable.

    Quantiles are deliberately NOT aggregated: the sum of per-day quantiles is
    not the quantile of the sum. Monthly intervals come from conformal
    calibration on the walk-forward residuals instead, which is exactly what
    the incumbent already does and needs no model contract at all.
    """
    est_dir = ESTATE_DIRS[estate]
    df = mm.load_estate(os.path.join(est_dir, "features_estate_monthly.csv"))
    d, fname = daily_mod.RAW_FILES[estate]
    daily = daily_mod.build(os.path.join(d, fname), gap_fill=gap_fill)

    model_months = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    scoreable = set(model_months)
    y = df.set_index("month")[mm.TARGET]

    origins, contexts, spans = [], [], []
    for i in range(min_train - 1, len(model_months) - 1):
        origin = model_months[i]
        cutoff = origin.to_timestamp(how="end").normalize()
        hist = daily[daily.index <= cutoff]
        if hist.empty:
            continue
        future_months = list(pd.period_range(origin + 1, periods=horizon, freq="M"))
        last_day = future_months[-1].to_timestamp(how="end").normalize()
        n_days = int((last_day - cutoff).days)
        if n_days < 1:
            continue
        origins.append(origin)
        contexts.append(hist.to_numpy(dtype=float))
        spans.append((cutoff, n_days, future_months))

    kw = {}
    if backend == "2.5":
        kw = dict(max_context=2048, max_horizon=max(400, max(s[1] for s in spans)))
    if REVISIONS.get(backend):
        kw["revision"] = REVISIONS[backend]

    max_days = max(s[1] for s in spans)
    t0 = time.perf_counter()
    # point="mean", NOT the default median. The backends' point head is the
    # median, which is correct for one step but wrong to SUM: daily harvest is
    # zero-inflated and right-skewed (CV ~180%), so the median sits well below
    # the mean and adding up ~30 of them undershoots the monthly total badly.
    # E[sum] = sum of E[X], so aggregation needs the mean.
    res = tfm.forecast_batch(contexts, max_days, backend=backend,
                             point=point, **kw)
    elapsed = time.perf_counter() - t0

    rows = []
    for origin, (cutoff, n_days, future_months), r in zip(origins, spans, res):
        idx = pd.date_range(cutoff + pd.Timedelta(days=1), periods=max_days, freq="D")
        fc = pd.Series(r["point"], index=idx)
        agg = fc.groupby(fc.index.to_period("M")).sum()
        for step, m in enumerate(future_months, start=1):
            if m in scoreable and m in agg.index:
                rows.append(dict(origin=str(origin), step=step, month=str(m),
                                 actual=float(y.loc[m]), pred=float(agg.loc[m]),
                                 n_context=int(r["n_context"])))
    meta = {"elapsed_s": elapsed, "n_origins": len(origins),
            "gap_fill": gap_fill, "max_days": max_days, "daily_point": point}
    return pd.DataFrame(rows), meta


# -- scoring ------------------------------------------------------------------
def score_against_incumbent(resid, estate, label, subset_months=None):
    """Put TimesFM on the incumbent's own scoreboard, with its own MASE denominator.

    mm.scoreboard() derives naive_mae from the SeasonalNaive rows IN THE FRAME
    IT IS GIVEN. So concatenating TimesFM's 1-step rows onto the existing
    predictions_monthly.csv and calling it once yields a single table in which
    every model -- incumbent and challenger -- shares the identical 20,387
    denominator. No re-derivation, no drift, no arithmetic of our own.
    """
    est_dir = ESTATE_DIRS[estate]
    existing = pd.read_csv(os.path.join(est_dir, "predictions_monthly.csv"))

    one = resid[resid["step"] == 1][["month", "actual", "pred"]].copy()
    one["model"] = label

    sn_months = set(existing.loc[existing.model == "SeasonalNaive", "month"])
    if set(one["month"]) != sn_months:
        missing, extra = sn_months - set(one["month"]), set(one["month"]) - sn_months
        raise AssertionError(
            f"fold misalignment: the comparison would be invalid. "
            f"missing={sorted(missing)} extra={sorted(extra)}")

    if subset_months is not None:
        keep = set(subset_months)
        existing = existing[existing["month"].isin(keep)]
        one = one[one["month"].isin(keep)]

    combined = pd.concat([existing, one], ignore_index=True)
    combined["error"] = combined["pred"] - combined["actual"]
    board, naive_mae = mm.scoreboard(combined)
    return board, naive_mae


def _fmt(board):
    o = board.copy()
    for c in ("MAE", "RMSE", "Bias"):
        o[c] = o[c].map(lambda v: f"{v:,.0f}")
    o["sMAPE"] = o["sMAPE"].map(lambda v: f"{v:.1f}%")
    o["MASE"] = o["MASE"].map(lambda v: f"{v:.3f}")
    return o.to_string(index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--estate", default="k3", choices=sorted(ESTATE_DIRS))
    ap.add_argument("--track", default="monthly",
                    choices=["monthly", "daily", "both"])
    ap.add_argument("--backend", default="3.0", choices=["3.0", "2.5", "both"])
    ap.add_argument("--covariates", action="store_true",
                    help="3.0 only: feed weather + calendar covariate channels")
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--min-train", type=int, default=MIN_TRAIN)
    ap.add_argument("--daily-point", default="mean",
                    choices=["mean", "median", "model"],
                    help="daily track aggregation statistic; mean is correct "
                         "for summing, median undershoots on skewed data")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    backends = ["3.0", "2.5"] if args.backend == "both" else [args.backend]
    tracks = ["monthly", "daily"] if args.track == "both" else [args.track]

    runs, lines = [], []
    for backend in backends:
        info = tfm.available(backend)
        if not info["available"]:
            print(f"  backend {backend} unavailable: {info['reason']}")
            continue
        for track in tracks:
            variants = [False]
            if track == "monthly" and args.covariates and backend == "3.0":
                variants = [False, True]
            for use_cov in variants:
                label = (f"TimesFM{backend.replace('.', '')}"
                         f"_{'daily_agg' if track == 'daily' else 'monthly'}"
                         f"{'_cov' if use_cov else ''}")
                print(f"\n=== {label} ({info['checkpoint']}) ===")
                if track == "monthly":
                    resid, meta = run_monthly(args.estate, backend, args.horizon,
                                              args.min_train, use_cov)
                else:
                    resid, meta = run_daily(args.estate, backend, args.horizon,
                                            args.min_train,
                                            point=args.daily_point)
                print(f"    {meta['n_origins']} origins, {len(resid)} scored rows, "
                      f"{meta['elapsed_s']:.1f}s inference")
                if use_cov:
                    print(f"    past_future: {meta['cov_past_future']}")
                    print(f"    past_only  : {meta['cov_past_only']}")

                board, naive = score_against_incumbent(resid, args.estate, label)
                row = board[board.model == label].iloc[0]

                one = resid[resid["step"] == 1]
                matched = sorted(one.loc[one["n_context"] >= MATCHED_MIN_CONTEXT,
                                         "month"])
                m_board = None
                if len(matched) >= 8:
                    m_board, _ = score_against_incumbent(
                        resid, args.estate, label, subset_months=matched)

                conf = conformal.calibrate(resid[["origin", "step", "month",
                                                  "actual", "pred"]],
                                           max_step=args.horizon)
                resid.to_csv(os.path.join(
                    OUT_DIR, f"{args.estate}_{label}_multih.csv"), index=False)
                board.to_csv(os.path.join(
                    OUT_DIR, f"{args.estate}_{label}_scoreboard.csv"), index=False)

                runs.append(dict(label=label, backend=backend, track=track,
                                 use_cov=use_cov, board=board, m_board=m_board,
                                 matched_n=len(matched), conf=conf, meta=meta,
                                 resid=resid,
                                 paired=_paired(resid, args.estate, label),
                                 smape=float(row.sMAPE), mase=float(row.MASE),
                                 checkpoint=info["checkpoint"],
                                 license=info["license"],
                                 shippable=info["shippable"]))

    if not runs:
        print("no runs completed")
        return 1

    # One scoreboard holding EVERY variant plus all incumbents, scored together
    # so they share the single SeasonalNaive MASE denominator.
    est_dir = ESTATE_DIRS[args.estate]
    frames = [pd.read_csv(os.path.join(est_dir, "predictions_monthly.csv"))]
    for r in runs:
        o = r["resid"][r["resid"]["step"] == 1][["month", "actual", "pred"]].copy()
        o["model"] = r["label"]
        frames.append(o)
    combined = pd.concat(frames, ignore_index=True)
    combined["error"] = combined["pred"] - combined["actual"]
    board_all, _ = mm.scoreboard(combined)
    board_all.to_csv(os.path.join(
        OUT_DIR, f"{args.estate}_scoreboard_all.csv"), index=False)
    for r in runs:
        r["board_all"] = board_all

    _report(args, runs)
    _log_mlflow(args, runs)
    return 0


def _paired(resid, estate, label):
    """Fold-by-fold paired comparison against the incumbent at step 1.

    A 0.014 MASE gap over 30 folds is well inside noise, so a single aggregate
    number is not enough to claim a win. This reports how often the challenger
    actually wins a fold and a paired test on absolute errors, which is what
    decides whether the headline delta means anything.
    """
    est_dir = ESTATE_DIRS[estate]
    ex = pd.read_csv(os.path.join(est_dir, "predictions_monthly.csv"))
    inc = ex[ex.model == "Ensemble_workdone"].set_index("month")
    one = resid[resid["step"] == 1].set_index("month")
    common = sorted(set(inc.index) & set(one.index))
    a = (one.loc[common, "pred"] - one.loc[common, "actual"]).abs().to_numpy()
    b = (inc.loc[common, "pred"] - inc.loc[common, "actual"]).abs().to_numpy()
    d = b - a                                    # >0 where TimesFM is closer
    n = len(d)
    wins = int((d > 0).sum())
    sd = d.std(ddof=1)
    t = float(d.mean() / (sd / np.sqrt(n))) if n > 1 and sd > 0 else 0.0
    return {"n": n, "wins": wins, "mean_gain": float(d.mean()), "t": t,
            "significant": abs(t) > 2.045}       # two-sided 5%, df~29


def _report(args, runs):
    inc = runs[0]["board"].set_index("model")
    inc_smape = float(inc.loc["Ensemble_workdone", "sMAPE"])
    inc_mase = float(inc.loc["Ensemble_workdone", "MASE"])
    best = min(runs, key=lambda r: r["mase"])
    ship = [r for r in runs if r["shippable"]]
    best_ship = min(ship, key=lambda r: r["mase"]) if ship else None

    out = []
    out.append("=" * 78)
    out.append(f"TimesFM benchmark -- estate {args.estate}")
    out.append("=" * 78)

    # Verdict is stated on the SHIPPABLE arm, because that is the only result
    # that could ever reach production. A non-commercial win is reported
    # separately and explicitly, never as "the" verdict.
    if best_ship is not None:
        won = best_ship["mase"] < inc_mase
        out.append(
            f"VERDICT (shippable, Apache-2.0 weights only): "
            f"{'WIN' if won else 'LOSS'} -- {best_ship['label']} "
            f"sMAPE {best_ship['smape']:.2f}% / MASE {best_ship['mase']:.3f}")
        out.append(f"         vs incumbent Ensemble_workdone {inc_smape:.2f}% / "
                   f"{inc_mase:.3f}  "
                   f"(delta {best_ship['smape'] - inc_smape:+.2f} pp sMAPE, "
                   f"{best_ship['mase'] - inc_mase:+.3f} MASE)")
    else:
        out.append("VERDICT (shippable): NOT MEASURED -- no Apache-2.0 (2.5) "
                   "variant was run.")
        out.append("         Run with --backend 2.5 to get a shippable number.")
    if not best["shippable"]:
        out.append("")
        out.append(f"  Best OVERALL is {best['label']} "
                   f"{best['smape']:.2f}% / {best['mase']:.3f}, but it uses "
                   f"TimesFM 3.0 weights under")
        out.append("  timesfm-non-commercial-license-v1.0 -- NON-COMMERCIAL, "
                   "NON-PRODUCTION. It CANNOT be")
        out.append("  served on this dashboard. Treat it as an upper bound, "
                   "not a deployable result.")
    out.append("LICENSE: 2.5 Apache-2.0 (shippable) | "
               "3.0 timesfm-non-commercial-license-v1.0 (benchmark only)")
    out.append("")

    out.append("-- headline scoreboard, all folds (sorted by MASE) --")
    out.append(_fmt(runs[-1]["board_all"]))
    out.append("")

    out.append("-- paired fold-by-fold vs Ensemble_workdone (step 1) --")
    for r in runs:
        p = r["paired"]
        out.append(
            f"   {r['label']:26s} wins {p['wins']}/{p['n']} folds | "
            f"mean MAE gain {p['mean_gain']:+,.0f} bunches | t={p['t']:+.2f} | "
            f"{'SIGNIFICANT at 5%' if p['significant'] else 'NOT significant'}")
    out.append("   A win on the aggregate with a non-significant paired test "
               "means the")
    out.append("   models are indistinguishable on this much data -- not that "
               "one is better.")
    out.append("")

    for r in runs:
        out.append(f"-- {r['label']} --")
        out.append(f"   checkpoint {r['checkpoint']}  ({r['license']})")
        out.append(f"   {r['meta']['n_origins']} origins, "
                   f"{r['meta']['elapsed_s']:.1f}s inference")
        if r["use_cov"]:
            out.append(f"   past_future {r['meta']['cov_past_future']}")
            out.append(f"   past_only   {r['meta']['cov_past_only']}")
        if r["m_board"] is not None:
            mb = r["m_board"].set_index("model")
            out.append(f"   matched-context subset (n_context >= "
                       f"{MATCHED_MIN_CONTEXT}, {r['matched_n']} folds) -- "
                       f"INCUMBENT RE-SCORED ON THE SAME FOLDS:")
            out.append(f"     {r['label']:28s} "
                       f"{float(mb.loc[r['label'], 'sMAPE']):.2f}% / "
                       f"{float(mb.loc[r['label'], 'MASE']):.3f}")
            out.append(f"     {'Ensemble_workdone':28s} "
                       f"{float(mb.loc['Ensemble_workdone', 'sMAPE']):.2f}% / "
                       f"{float(mb.loc['Ensemble_workdone', 'MASE']):.3f}")
        c = r["conf"]
        out.append(f"   conformal: method {c.get('served_method')}, "
                   f"coverage h1-3 {c.get('coverage_h1_3')}, "
                   f"n_residuals {c.get('n_residuals')}")
        ss = c.get("step_smape", {})
        out.append("   per-step sMAPE: " + "  ".join(
            f"h{k}={v:.1f}%" for k, v in sorted(ss.items())[:6]))
        out.append("")

    out.append("-- why this could be wrong --")
    nb = sum(1 for r in runs
             if r["matched_n"] and r["matched_n"] < r["meta"]["n_origins"])
    out.append(
        f"  * Context below TimesFM's own 32-point minimum on most folds: the\n"
        f"    walk-forward starts at 6 months of history. {nb} variant(s) had\n"
        f"    fewer matched-context folds than total folds.")
    out.append(
        "  * The shippable backend (2.5) is univariate-only on Windows -- its\n"
        "    XReg covariate path needs jax[cuda], which has no Windows wheels.")
    out.append(
        "  * Zero-shot: the model has no palm-agronomy signal. The incumbent\n"
        "    uses pruning man-days at lag 1, pruning qty at lag 5, fertiliser\n"
        "    at lag 3 and rainfall at flowering 5-7 months prior.")
    out.append(
        "  * Single estate, ~30 folds. A 90% coverage estimate has ~7pp\n"
        "    standard error at this fold count; treat coverage claims loosely.")
    out.append("=" * 78)

    text = "\n".join(out)
    print("\n" + text)
    path = os.path.join(OUT_DIR, f"{args.estate}_timesfm_results.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(f"\nwrote {path}")


def _log_mlflow(args, runs):
    """Best-effort, exactly like monthly_model.train -- never fatal."""
    try:
        import mlflow
        mlflow.set_tracking_uri(f"sqlite:///{os.path.join(_REPO, 'mlflow.db')}")
        mlflow.set_experiment(MLFLOW_EXPERIMENT)
        inc = runs[0]["board"].set_index("model")
        for r in runs:
            with mlflow.start_run(run_name=f"{args.estate}_{r['label']}"):
                mlflow.log_params({
                    "estate": args.estate, "backend": r["backend"],
                    "track": r["track"], "checkpoint": r["checkpoint"],
                    "revision": REVISIONS.get(r["backend"]),
                    "license": r["license"], "shippable": r["shippable"],
                    "covariates": r["use_cov"], "horizon": args.horizon,
                    "min_train": args.min_train,
                    "n_origins": r["meta"]["n_origins"],
                    "cov_past_future": r["meta"].get("cov_past_future"),
                    "cov_past_only": r["meta"].get("cov_past_only"),
                    "gap_fill": r["meta"].get("gap_fill"),
                })
                row = r["board"].set_index("model").loc[r["label"]]
                mlflow.log_metrics({
                    "smape": float(row.sMAPE), "mase": float(row.MASE),
                    "mae": float(row.MAE), "bias": float(row.Bias),
                    # incumbent alongside, so the comparison is visible in the UI
                    "incumbent_smape": float(inc.loc["Ensemble_workdone", "sMAPE"]),
                    "incumbent_mase": float(inc.loc["Ensemble_workdone", "MASE"]),
                    "smape_delta": float(row.sMAPE)
                                   - float(inc.loc["Ensemble_workdone", "sMAPE"]),
                    "matched_folds": r["matched_n"],
                    "inference_s": r["meta"]["elapsed_s"],
                })
                mlflow.set_tags({
                    "verdict": "win" if float(row.MASE) < float(
                        inc.loc["Ensemble_workdone", "MASE"]) else "loss",
                    "shippable": str(r["shippable"]),
                })
        p = os.path.join(OUT_DIR, f"{args.estate}_timesfm_results.txt")
        if os.path.exists(p):
            with mlflow.start_run(run_name=f"{args.estate}_report"):
                mlflow.log_artifact(p)
        print(f"MLflow: logged {len(runs)} run(s) to '{MLFLOW_EXPERIMENT}'")
    except Exception as exc:
        print(f"MLflow logging skipped: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
