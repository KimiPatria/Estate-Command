"""
Chronos-2 benchmark harness -- can a zero-shot foundation model beat the
incumbent? (Phase 1 of the Chronos-2 evaluation; Phase 2 is LoRA fine-tuning
on SageMaker, a separate script.)

Mirrors timesfm_experiment.py's structure and leakage discipline exactly, and
reuses its generic scoring/reporting helpers directly (score_against_incumbent,
_paired, _fmt, _split_covariates, CANDIDATE_COVS are model-agnostic -- they
only read predictions_monthly.csv and a resid frame, never TimesFM itself).
What's new here is Chronos-2-specific: the AutoGluon-backed model call, and a
simpler report because Chronos-2 has ONE checkpoint and it is fully
Apache-2.0/shippable -- no non-commercial-vs-shippable split to report.

Run this from the `chronos-rd` conda env (see requirements-chronos.txt), not
the anaconda base env that serves the dashboard.

The bar (k3): Ensemble(SARIMAX+Trailing3+LightGBM_workdone), sMAPE 10.07 /
MASE 0.572 over 30 walk-forward folds -- same incumbent TimesFM was benchmarked
against, so all three land on one scoreboard.

Two tracks (same as TimesFM)
-----------------------------
monthly  36 usable monthly points -- the only apples-to-apples comparison.
daily    ~1,242 daily points, forecast daily and summed to monthly totals,
         scored on the SAME monthly scale.

Leakage discipline
-------------------
Contexts are always sliced `df[df.month <= origin]` before anything downstream
sees them, exactly mirroring monthly_model.backtest_multih and
timesfm_experiment.py. Covariates reuse timesfm_experiment._split_covariates
verbatim, so a channel is past_future only if non-NaN across the whole horizon.

Usage:
    conda run -n chronos-rd python forecast/chronos_experiment.py --track monthly
    conda run -n chronos-rd python forecast/chronos_experiment.py --track monthly --covariates
    conda run -n chronos-rd python forecast/chronos_experiment.py --track daily
    conda run -n chronos-rd python forecast/chronos_experiment.py --track both --covariates
"""

import argparse
import importlib.util
import os
import time

import numpy as np
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_DIR)
OUT_DIR = os.path.join(_DIR, "experiments", "chronos2")

ESTATE_DIRS = {"k3": _DIR, "ec": os.path.join(_DIR, "EC")}
HORIZON = 12
MIN_TRAIN = 6
MATCHED_MIN_CONTEXT = 24  # NOT a documented Chronos-2 minimum (unlike TimesFM's
                          # 32-point floor) -- kept only as a general "does more
                          # history change the picture" robustness check, for
                          # methodological continuity with the TimesFM report.
MLFLOW_EXPERIMENT = "ffb-chronos2-rd"


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mm = _load_by_path("epms_monthly_model", os.path.join(_DIR, "monthly_model.py"))
conformal = _load_by_path("epms_conformal", os.path.join(_DIR, "conformal.py"))
cm = _load_by_path("epms_chronos_model", os.path.join(_DIR, "chronos_model.py"))
daily_mod = _load_by_path("epms_daily_series",
                          os.path.join(_DIR, "build_daily_series.py"))
# timesfm_experiment's covariate splitting and scoring helpers are
# model-agnostic -- reused rather than duplicated. This does NOT require
# timesfm/torch to be importable; only its module-level code runs at load.
tfe = _load_by_path("epms_timesfm_experiment",
                    os.path.join(_DIR, "timesfm_experiment.py"))
_split_covariates = tfe._split_covariates
score_against_incumbent = tfe.score_against_incumbent
_paired = tfe._paired
_fmt = tfe._fmt


# -- monthly track ------------------------------------------------------------
def run_monthly(estate="k3", horizon=HORIZON, min_train=MIN_TRAIN, use_cov=False,
                batch_size=256, predictor=None, min_origin=None, cross_learning=False):
    """Walk-forward multi-horizon backtest, mirroring monthly_model.backtest_multih.

    All origins are prefixes of one series, so every origin goes into a SINGLE
    forecast_batch call -- one predictor fit (a no-op for zero-shot), one
    batched predict.

    predictor   forwarded to cm.forecast_batch -- None (default) is Phase 1's
                zero-shot path; an already-fit predictor is Phase 2's
                fine-tuned-eval path (see chronos_finetune_sagemaker.py).
    min_origin  skip origins <= this Period. Used by the fine-tuning script to
                evaluate ONLY origins whose forecast targets postdate the
                fine-tuning cutoff, so no gradient step ever saw a month that
                is later scored as an "actual" value.
    cross_learning  keep False. Every origin here is a prefix of one series and
                they share one batch, so True lets early origins see the months
                they are scored on (see cm.forecast_batch).
    """
    est_dir = ESTATE_DIRS[estate]
    df = mm.load_estate(os.path.join(est_dir, "features_estate_monthly.csv"))
    wm_full = mm.build_weather_monthly(
        os.path.join(est_dir, "weather_nasa_power_history.csv"))
    df = mm.apply_weather(df, wm_full)

    model_months = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    scoreable = set(model_months)
    y = df.set_index("month")[mm.TARGET]

    origins, contexts, futures, starts, pfs, pos = [], [], [], [], [], []
    for i in range(min_train - 1, len(model_months) - 1):
        origin = model_months[i]
        if min_origin is not None and origin <= min_origin:
            continue
        hist = df[df["month"] <= origin]                 # slice ONCE, up front
        hist_months = hist["month"].tolist()
        future_months = list(pd.period_range(origin + 1, periods=horizon, freq="M"))

        origins.append(origin)
        contexts.append(hist[mm.TARGET].to_numpy(dtype=float))
        futures.append(future_months)
        starts.append(hist_months[0].to_timestamp())     # real calendar date

        if use_cov:
            # information-at-origin: truncate weather to <= origin, extend forward.
            wm_o = mm.build_weather_monthly(
                os.path.join(est_dir, "weather_nasa_power_history.csv"),
                known_through=origin, extend=horizon)
            pf, po, pf_n, po_n = _split_covariates(wm_o, hist_months, future_months)
            pfs.append(pf)
            pos.append(po)

    t0 = time.perf_counter()
    res = cm.forecast_batch(
        contexts, horizon, freq="MS", starts=starts,
        past_future=pfs if use_cov else None,
        past_only=pos if use_cov else None,
        batch_size=batch_size, predictor=predictor, cross_learning=cross_learning)
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


# -- daily track ----------------------------------------------------------------
def run_daily(estate="k3", horizon=HORIZON, min_train=MIN_TRAIN, gap_fill="zero",
             point="mean", batch_size=64, cross_learning=False):
    """Forecast the daily series, sum to monthly totals, score on the monthly scale.

    Same rationale as timesfm_experiment.run_daily: point="mean" because daily
    harvest is zero-inflated and right-skewed, and E[sum] = sum of E[X] while
    the sum of medians undershoots badly.
    """
    est_dir = ESTATE_DIRS[estate]
    df = mm.load_estate(os.path.join(est_dir, "features_estate_monthly.csv"))
    d, fname = daily_mod.RAW_FILES[estate]
    daily = daily_mod.build(os.path.join(d, fname), gap_fill=gap_fill)

    model_months = df.loc[df["exclude_from_model"] == 0, "month"].tolist()
    scoreable = set(model_months)
    y = df.set_index("month")[mm.TARGET]

    origins, contexts, spans, starts = [], [], [], []
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
        starts.append(hist.index[0])
        spans.append((cutoff, n_days, future_months))

    max_days = max(s[1] for s in spans)
    t0 = time.perf_counter()
    res = cm.forecast_batch(contexts, max_days, freq="D", starts=starts,
                            point=point, batch_size=batch_size,
                            cross_learning=cross_learning)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--estate", default="k3", choices=sorted(ESTATE_DIRS))
    ap.add_argument("--track", default="monthly", choices=["monthly", "daily", "both"])
    ap.add_argument("--covariates", action="store_true",
                    help="monthly track only: feed weather + calendar covariate channels")
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--min-train", type=int, default=MIN_TRAIN)
    ap.add_argument("--daily-point", default="mean", choices=["mean", "median", "model"],
                    help="daily track aggregation statistic; mean is correct for "
                         "summing, median undershoots on skewed data")
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--cross-learning", action="store_true",
                    help="reproduce the pre-fix numbers: lets origins in one batch "
                         "attend to each other, which leaks later months into "
                         "earlier origins' forecasts. Never use for a verdict.")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    info = cm.available()
    if not info["available"]:
        print(f"Chronos-2 unavailable: {info['reason']}")
        return 1

    tracks = ["monthly", "daily"] if args.track == "both" else [args.track]
    runs = []
    for track in tracks:
        variants = [False, True] if (track == "monthly" and args.covariates) else [False]
        for use_cov in variants:
            label = f"Chronos2_{'daily_agg' if track == 'daily' else 'monthly'}{'_cov' if use_cov else ''}"
            print(f"\n=== {label} ({info['checkpoint']}) ===")
            if track == "monthly":
                resid, meta = run_monthly(args.estate, args.horizon, args.min_train,
                                          use_cov, batch_size=args.batch_size,
                                          cross_learning=args.cross_learning)
            else:
                resid, meta = run_daily(args.estate, args.horizon, args.min_train,
                                        point=args.daily_point, batch_size=args.batch_size,
                                        cross_learning=args.cross_learning)
            print(f"    {meta['n_origins']} origins, {len(resid)} scored rows, "
                  f"{meta['elapsed_s']:.1f}s inference")
            if use_cov:
                print(f"    past_future: {meta['cov_past_future']}")
                print(f"    past_only  : {meta['cov_past_only']}")

            board, naive = score_against_incumbent(resid, args.estate, label)
            row = board[board.model == label].iloc[0]

            one = resid[resid["step"] == 1]
            matched = sorted(one.loc[one["n_context"] >= MATCHED_MIN_CONTEXT, "month"])
            m_board = None
            if len(matched) >= 8:
                m_board, _ = score_against_incumbent(resid, args.estate, label,
                                                      subset_months=matched)

            conf = conformal.calibrate(
                resid[["origin", "step", "month", "actual", "pred"]],
                max_step=args.horizon)
            resid.to_csv(os.path.join(OUT_DIR, f"{args.estate}_{label}_multih.csv"),
                        index=False)
            board.to_csv(os.path.join(OUT_DIR, f"{args.estate}_{label}_scoreboard.csv"),
                        index=False)

            runs.append(dict(label=label, track=track, use_cov=use_cov, board=board,
                             m_board=m_board, matched_n=len(matched), conf=conf,
                             meta=meta, resid=resid,
                             paired=_paired(resid, args.estate, label),
                             smape=float(row.sMAPE), mase=float(row.MASE)))

    if not runs:
        print("no runs completed")
        return 1

    est_dir = ESTATE_DIRS[args.estate]
    frames = [pd.read_csv(os.path.join(est_dir, "predictions_monthly.csv"))]
    for r in runs:
        o = r["resid"][r["resid"]["step"] == 1][["month", "actual", "pred"]].copy()
        o["model"] = r["label"]
        frames.append(o)
    combined = pd.concat(frames, ignore_index=True)
    combined["error"] = combined["pred"] - combined["actual"]
    board_all, _ = mm.scoreboard(combined)
    board_all.to_csv(os.path.join(OUT_DIR, f"{args.estate}_scoreboard_all.csv"), index=False)
    for r in runs:
        r["board_all"] = board_all

    _report(args, runs, info)
    _log_mlflow(args, runs, info)
    return 0


def _report(args, runs, info):
    inc = runs[0]["board"].set_index("model")
    inc_smape = float(inc.loc["Ensemble_workdone", "sMAPE"])
    inc_mase = float(inc.loc["Ensemble_workdone", "MASE"])
    best = min(runs, key=lambda r: r["mase"])

    out = []
    out.append("=" * 78)
    out.append(f"Chronos-2 benchmark -- estate {args.estate}")
    out.append("=" * 78)

    # Unlike TimesFM, Chronos-2 (amazon/chronos-2) is Apache-2.0 in full --
    # there is no non-commercial checkpoint here, so the verdict is a single
    # clean number rather than a shippable-vs-non-commercial split.
    won = best["mase"] < inc_mase
    out.append(f"VERDICT (Apache-2.0, shippable): {'WIN' if won else 'LOSS'} -- "
              f"{best['label']} sMAPE {best['smape']:.2f}% / MASE {best['mase']:.3f}")
    out.append(f"         vs incumbent Ensemble_workdone {inc_smape:.2f}% / "
              f"{inc_mase:.3f}  (delta {best['smape'] - inc_smape:+.2f} pp sMAPE, "
              f"{best['mase'] - inc_mase:+.3f} MASE)")
    out.append(f"LICENSE: {info['license']} (checkpoint {info['checkpoint']})")
    out.append("")

    out.append("-- headline scoreboard, all folds (sorted by MASE) --")
    out.append(_fmt(runs[-1]["board_all"]))
    out.append("")

    out.append("-- paired fold-by-fold vs Ensemble_workdone (step 1) --")
    for r in runs:
        p = r["paired"]
        out.append(f"   {r['label']:26s} wins {p['wins']}/{p['n']} folds | "
                  f"mean MAE gain {p['mean_gain']:+,.0f} bunches | t={p['t']:+.2f} | "
                  f"{'SIGNIFICANT at 5%' if p['significant'] else 'NOT significant'}")
    out.append("   A win on the aggregate with a non-significant paired test means "
              "the")
    out.append("   models are indistinguishable on this much data -- not that one "
              "is better.")
    out.append("")

    for r in runs:
        out.append(f"-- {r['label']} --")
        out.append(f"   {r['meta']['n_origins']} origins, {r['meta']['elapsed_s']:.1f}s inference")
        if r["use_cov"]:
            out.append(f"   past_future {r['meta']['cov_past_future']}")
            out.append(f"   past_only   {r['meta']['cov_past_only']}")
        if r["m_board"] is not None:
            mb = r["m_board"].set_index("model")
            out.append(f"   context >= {MATCHED_MIN_CONTEXT} subset "
                      f"({r['matched_n']} folds, NOT a documented Chronos-2 "
                      f"minimum -- robustness check only), incumbent re-scored "
                      f"on the same folds:")
            out.append(f"     {r['label']:28s} {float(mb.loc[r['label'], 'sMAPE']):.2f}% "
                      f"/ {float(mb.loc[r['label'], 'MASE']):.3f}")
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
    out.append(
        "  * Zero-shot: the model has no palm-agronomy signal. The incumbent uses\n"
        "    pruning man-days at lag 1, pruning qty at lag 5, fertiliser at lag 3\n"
        "    and rainfall at flowering 5-7 months prior.")
    out.append(
        "  * Single estate, ~30 folds. A 90% coverage estimate has ~7pp standard\n"
        "    error at this fold count; treat coverage claims loosely.")
    out.append(
        "  * AutoGluon's zero-shot Chronos2 wrapper is called once per track/\n"
        "    variant here, not cached across calls -- fine for an R&D sweep run a\n"
        "    handful of times, but not the pattern for a live serving path.")
    out.append("=" * 78)

    text = "\n".join(out)
    print("\n" + text)
    path = os.path.join(OUT_DIR, f"{args.estate}_chronos2_results.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(f"\nwrote {path}")


def _log_mlflow(args, runs, info):
    """Best-effort, exactly like monthly_model.train -- never fatal."""
    try:
        import mlflow
        mlflow.set_tracking_uri(f"sqlite:///{os.path.join(_REPO, 'mlflow.db')}")
        mlflow.set_experiment(MLFLOW_EXPERIMENT)
        inc = runs[0]["board"].set_index("model")
        for r in runs:
            with mlflow.start_run(run_name=f"{args.estate}_{r['label']}"):
                mlflow.log_params({
                    "estate": args.estate, "track": r["track"],
                    "checkpoint": info["checkpoint"], "license": info["license"],
                    "shippable": info["shippable"], "covariates": r["use_cov"],
                    "horizon": args.horizon, "min_train": args.min_train,
                    "n_origins": r["meta"]["n_origins"],
                    "cov_past_future": r["meta"].get("cov_past_future"),
                    "cov_past_only": r["meta"].get("cov_past_only"),
                    "gap_fill": r["meta"].get("gap_fill"),
                    "cross_learning": args.cross_learning,
                })
                row = r["board"].set_index("model").loc[r["label"]]
                mlflow.log_metrics({
                    "smape": float(row.sMAPE), "mase": float(row.MASE),
                    "mae": float(row.MAE), "bias": float(row.Bias),
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
                    "shippable": str(info["shippable"]),
                })
        p = os.path.join(OUT_DIR, f"{args.estate}_chronos2_results.txt")
        if os.path.exists(p):
            with mlflow.start_run(run_name=f"{args.estate}_report"):
                mlflow.log_artifact(p)
        print(f"MLflow: logged {len(runs)} run(s) to '{MLFLOW_EXPERIMENT}'")
    except Exception as exc:
        print(f"MLflow logging skipped: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
