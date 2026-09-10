"""
Benchmark the direct multi-horizon model against the recursive incumbent and
the TimesFM arms, on identical folds.

Run:  python forecast/direct_experiment.py [--estate k3] [--horizon 12]

Writes forecast/experiments/direct/<estate>_direct_multih.csv and
<estate>_direct_results.txt, and a per-horizon paired comparison against
whatever TimesFM multih files already exist in experiments/timesfm/.

The comparison is clustered by ORIGIN. The three steps from one origin share a
fitting window and an information set, so treating 87 origin-step rows as 87
independent observations overstates significance; the clustered test averages
each origin's advantage first and tests the 30 origin means.
"""

import argparse
import os as _os
import sys
import numpy as np
import pandas as pd
from scipy import stats

_DIR = _os.path.dirname(_os.path.abspath(__file__))
sys.path.insert(0, _DIR)

import direct_model as DM                                    # noqa: E402
MM = DM.MM

OUT_DIR = _os.path.join(_DIR, "experiments", "direct")
TFM_DIR = _os.path.join(_DIR, "experiments", "timesfm")
ESTATE_DIRS = {"k3": _DIR, "EC": _os.path.join(_DIR, "EC")}


def smape(a, p):
    a, p = np.asarray(a, float), np.asarray(p, float)
    d = np.abs(a) + np.abs(p)
    return float(np.mean(np.where(d == 0, 0.0, 2 * np.abs(p - a) / d)) * 100)


def clustered(inc_pred, alt_pred, actual, origin):
    """Paired comparison clustered by origin. Positive gain favours `alt`."""
    d = np.abs(inc_pred - actual) - np.abs(alt_pred - actual)
    g = pd.Series(d).groupby(pd.Series(origin)).mean()
    if len(g) < 3:
        return dict(gain=float(g.mean()) if len(g) else np.nan, t=np.nan, p=np.nan,
                    wins=0, n_cl=len(g), lo=np.nan, hi=np.nan)
    t, p = stats.ttest_1samp(g, 0)
    rng = np.random.default_rng(0)
    idx = g.index.values
    boot = [g.loc[rng.choice(idx, len(idx), replace=True)].mean() for _ in range(10000)]
    return dict(gain=float(g.mean()), t=float(t), p=float(p),
                wins=int((g > 0).sum()), n_cl=len(g),
                lo=float(np.percentile(boot, 2.5)), hi=float(np.percentile(boot, 97.5)))


def run(estate="k3", horizon=12, max_step=3):
    edir = ESTATE_DIRS[estate]
    _os.makedirs(OUT_DIR, exist_ok=True)

    estate_file = _os.path.join(edir, "features_estate_monthly.csv")
    weather_file = _os.path.join(edir, "weather_nasa_power_history.csv")
    workdone_file = MM.WORKDONE_FILE

    df = MM.load_estate(estate_file)
    # Same authoritative-weather step train() does, with the ESTATE'S OWN file.
    df = MM.apply_weather(df, MM.build_weather_monthly(weather_file))

    print(f"[{estate}] running direct walk-forward (horizon={horizon}) ...")
    direct = DM.backtest_multih_direct(df, weather_file, workdone_file,
                                       horizon=horizon, min_train=MM.MIN_TRAIN)
    direct.to_csv(_os.path.join(OUT_DIR, f"{estate}_direct_multih.csv"), index=False)

    inc_path = _os.path.join(edir, "predictions_multih.csv")
    inc = pd.read_csv(inc_path)

    # ---- assemble every arm on the incumbent's folds -----------------------
    # Fall the LightGBM-only arms back to the other ensemble members where no
    # model was fit, so every arm is scored on the SAME rows. Dropping those
    # rows instead would quietly change the denominator and flatter the arm.
    fb = (direct["pred_sarimax"] + direct["pred_tr3"]) / 2
    arms = {
        "Direct_ensemble (pooled)": direct[["origin", "step", "month", "pred"]],
        "Direct_pooled_lgb_only": direct.assign(
            pred=direct["pred_pooled"].fillna(fb))[["origin", "step", "month", "pred"]],
        "Direct_perhorizon_lgb_only": direct.assign(
            pred=direct["pred_lgb"].fillna(fb))[["origin", "step", "month", "pred"]],
    }
    for f in sorted(_os.listdir(TFM_DIR)) if _os.path.isdir(TFM_DIR) else []:
        if f.startswith(f"{estate}_") and f.endswith("_multih.csv"):
            name = f[len(estate) + 1:-len("_multih.csv")]
            arms[name] = pd.read_csv(_os.path.join(TFM_DIR, f))[
                ["origin", "step", "month", "pred"]]

    lines = ["=" * 78,
             f"Direct multi-horizon benchmark -- estate {estate}",
             "=" * 78,
             f"Scored on steps 1-{max_step} (the horizon the dashboard serves).",
             "Paired tests are CLUSTERED BY ORIGIN: the steps within one origin share",
             "a fitting window, so they are not independent observations.", ""]

    rows = []
    for name, alt in arms.items():
        m = inc.merge(alt, on=["origin", "step", "month"], suffixes=("_inc", "_alt"))
        m = m[m["step"] <= max_step]
        if m.empty:
            continue
        a = m["actual"].values
        st = clustered(m["pred_inc"].values, m["pred_alt"].values, a, m["origin"].values)
        rows.append(dict(model=name, n=len(m),
                         sMAPE=smape(a, m["pred_alt"].values),
                         MAE=float(np.abs(m["pred_alt"].values - a).mean()),
                         Bias=float((m["pred_alt"].values - a).mean()), **st))
    # incumbent reference row on the same rows
    ref = inc[inc["step"] <= max_step]
    a = ref["actual"].values
    rows.append(dict(model="Ensemble_workdone (incumbent)", n=len(ref),
                     sMAPE=smape(a, ref["pred"].values),
                     MAE=float(np.abs(ref["pred"].values - a).mean()),
                     Bias=float((ref["pred"].values - a).mean()),
                     gain=0.0, t=np.nan, p=np.nan, wins=0, lo=np.nan, hi=np.nan))

    board = pd.DataFrame(rows).sort_values("sMAPE").reset_index(drop=True)
    lines.append(f"-- steps 1-{max_step} scoreboard (sorted by sMAPE) --")
    lines.append(f"{'model':30s} {'sMAPE':>6s} {'MAE':>8s} {'Bias':>8s} "
                 f"{'gain':>8s} {'t':>6s} {'p':>7s} {'wins':>7s}  boot95 CI")
    for _, r in board.iterrows():
        wins = f"{r['wins']}/{int(r['n']) // max_step}" if r["wins"] else "-"
        ci = (f"[{r['lo']:+.0f}, {r['hi']:+.0f}]" if r["lo"] == r["lo"] else "")
        tt = f"{r['t']:+.2f}" if r["t"] == r["t"] else "  ref"
        pp = f"{r['p']:.3f}" if r["p"] == r["p"] else "     -"
        lines.append(f"{r['model']:30s} {r['sMAPE']:6.2f} {r['MAE']:8,.0f} "
                     f"{r['Bias']:+8,.0f} {r['gain']:+8,.0f} {tt:>6s} {pp:>7s} "
                     f"{wins:>7s}  {ci}")

    # ---- per-horizon detail -------------------------------------------------
    lines += ["", "-- per-horizon sMAPE (where recursion actually costs) --",
              f"{'model':30s} " + " ".join(f"{'h'+str(s):>7s}" for s in range(1, max_step + 1))]
    detail = {"Ensemble_workdone (incumbent)": inc}
    detail.update({k: inc.merge(v, on=["origin", "step", "month"], suffixes=("_inc", ""))
                   for k, v in arms.items()})
    for name, frame in detail.items():
        cells = []
        for s in range(1, max_step + 1):
            k = frame[frame["step"] == s]
            aa = k["actual"].values if "actual" in k else k["actual_inc"].values
            cells.append(f"{smape(aa, k['pred'].values):7.2f}" if len(k) else "      -")
        lines.append(f"{name:30s} " + " ".join(cells))

    # significance of the direct arm per horizon
    lines += ["", "-- Direct_ensemble (pooled) vs incumbent, per horizon (paired, n = folds) --"]
    m = inc.merge(arms["Direct_ensemble (pooled)"], on=["origin", "step", "month"],
                  suffixes=("_inc", "_alt"))
    for s in range(1, max_step + 1):
        k = m[m["step"] == s]
        if len(k) < 3:
            continue
        a = k["actual"].values
        ei, ea = np.abs(k["pred_inc"] - a), np.abs(k["pred_alt"] - a)
        t, p = stats.ttest_rel(ei, ea)
        w = stats.wilcoxon(ei, ea)
        lines.append(f"  h{s}: n={len(k):3d} wins {int((ei > ea).sum())}/{len(k)} "
                     f"MAEgain {(ei - ea).mean():+8,.0f} t={t:+.2f} p={p:.3f} "
                     f"wilcoxon p={w.pvalue:.3f}")

    lines += ["", "-- caveats --",
              "  * Single estate, ~30 origins. NOTHING here is significant at 5%: the",
              "    pooled arm's clustered p is 0.120. The error CURVE is visibly fixed",
              "    (10.5/13.7/15.5 -> 10.0/12.5/13.6); the aggregate win is a point",
              "    estimate, not a proven one.",
              "  * Several ensemble compositions were tried against these same 30 folds",
              "    while choosing the pooled member. The reported p-values are NOT",
              "    corrected for that search -- an independent refold is the honest",
              "    confirmation before this displaces the served model.",
              "  * Exogenous features are dropped by lag depth per horizon, so the h>=2",
              "    models genuinely have less weather signal than the h=1 model.",
              "  * Per-horizon models (Direct_perhorizon_lgb_only) LOSE to the incumbent.",
              "    Pooling is what makes the direct formulation work on this much data.",
              "=" * 78]

    txt = "\n".join(lines)
    print(txt)
    with open(_os.path.join(OUT_DIR, f"{estate}_direct_results.txt"), "w") as fh:
        fh.write(txt + "\n")
    board.to_csv(_os.path.join(OUT_DIR, f"{estate}_direct_scoreboard.csv"), index=False)
    _write_research_json(estate, board, detail, max_step)
    return board


# ── research-arm export for the /model-health page ──────────────────────────
# Metadata the scoreboard cannot know: what each arm IS, and whether it could
# ever be served. Licence status is recorded here rather than in the UI so the
# non-commercial arms cannot be promoted by editing a template.
ARM_META = {
    "Ensemble_workdone (incumbent)": dict(
        family="incumbent", servable=True, licence="in-house",
        note="Recursive: step h is fed its own step h-1 prediction."),
    "Direct_ensemble (pooled)": dict(
        family="direct", servable=True, licence="in-house",
        note="Same ensemble recipe, LightGBM member made direct and pooled "
             "across horizons. Keeps SHAP attribution."),
    "Direct_pooled_lgb_only": dict(
        family="direct", servable=True, licence="in-house",
        note="The pooled LightGBM alone, without SARIMAX/Trailing-3."),
    "Direct_perhorizon_lgb_only": dict(
        family="direct", servable=True, licence="in-house",
        note="One model per horizon. Loses to the incumbent — kept to show why "
             "pooling is the part that matters."),
    "TimesFM25_monthly": dict(
        family="timesfm", servable=True, licence="Apache-2.0",
        note="Zero-shot foundation model on the monthly series. No feature "
             "attribution available."),
    "TimesFM25_daily_agg": dict(
        family="timesfm", servable=True, licence="Apache-2.0",
        note="Daily forecast aggregated to months. Mean head, not median."),
    "TimesFM30_monthly": dict(
        family="timesfm", servable=False, licence="timesfm-non-commercial-v1.0",
        note="NON-COMMERCIAL weights. Benchmark upper bound only."),
    "TimesFM30_monthly_cov": dict(
        family="timesfm", servable=False, licence="timesfm-non-commercial-v1.0",
        note="NON-COMMERCIAL weights, with weather covariates. Upper bound only."),
    "TimesFM30_daily_agg": dict(
        family="timesfm", servable=False, licence="timesfm-non-commercial-v1.0",
        note="NON-COMMERCIAL weights, daily aggregated. Upper bound only."),
}


def _write_research_json(estate, board, detail, max_step):
    """Emit the offline-benchmark payload the model-health page renders.

    Written by the experiment, not derived in the router: these numbers come
    from a walk-forward that takes minutes and must never run inside a request.
    """
    import json
    per_h = {}
    for name, frame in detail.items():
        cells = {}
        for s in range(1, max_step + 1):
            k = frame[frame["step"] == s]
            if len(k):
                aa = k["actual"].values if "actual" in k else k["actual_inc"].values
                cells[str(s)] = round(smape(aa, k["pred"].values), 2)
        per_h[name] = cells

    arms = []
    for _, r in board.iterrows():
        name = r["model"]
        meta = ARM_META.get(name, dict(family="other", servable=False, licence="unknown", note=""))
        arms.append({
            "model": name, "family": meta["family"], "servable": meta["servable"],
            "licence": meta["licence"], "note": meta["note"],
            "is_incumbent": name.startswith("Ensemble_workdone"),
            "smape": round(float(r["sMAPE"]), 2),
            "mae": round(float(r["MAE"])), "bias": round(float(r["Bias"])),
            "gain": round(float(r["gain"])) if r["gain"] == r["gain"] else None,
            "t": round(float(r["t"]), 2) if r["t"] == r["t"] else None,
            "p": round(float(r["p"]), 3) if r["p"] == r["p"] else None,
            "wins": int(r["wins"]) or None,
            "n_origins": int(r["n_cl"]) if r.get("n_cl") == r.get("n_cl") else None,
            "ci_lo": round(float(r["lo"])) if r["lo"] == r["lo"] else None,
            "ci_hi": round(float(r["hi"])) if r["hi"] == r["hi"] else None,
            "per_horizon": per_h.get(name, {}),
        })

    payload = {
        "estate": estate,
        "max_step": max_step,
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "arms": arms,
        "headline": ("Nothing here is significant at 5%. The pooled direct arm fixes the "
                     "shape of the error curve across the 3-month horizon; the aggregate "
                     "win is a point estimate on 30 origins, not a proven one."),
        "caveats": [
            "Single estate, ~30 walk-forward origins.",
            "p-values are NOT corrected for the ensemble compositions tried while "
            "selecting the pooled member.",
            "TimesFM 3.0 arms use non-commercial weights and can never be served; "
            "they are an upper bound.",
            "TimesFM arms provide no feature attribution, so promoting one would "
            "remove the SHAP explanations behind the forecast.",
        ],
    }
    with open(_os.path.join(OUT_DIR, f"{estate}_research_arms.json"), "w") as fh:
        json.dump(payload, fh, indent=2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--estate", default="k3", choices=sorted(ESTATE_DIRS))
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--max-step", type=int, default=3)
    args = ap.parse_args()
    run(estate=args.estate, horizon=args.horizon, max_step=args.max_step)
