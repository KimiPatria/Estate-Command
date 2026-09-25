"""
Build the Forecast Lab's Chronos-2 artifacts (forecast/experiments/lab/).

The dashboard server's env has no Chronos (torch/autogluon live in the separate
chronos-rd-py313 env -- see requirements-chronos.txt), so the lab's Chronos
forecasts are produced here and read by experiment_router.py. Per model x
estate x grain it writes:

  <model>/<estate>_<grain>.json           the forward forecast, MAX_H steps
                                          (the page slices the horizon itself)
  backtest/<model>/<estate>_<grain>.csv   a walk-forward backtest: a forecast
                                          from every registry.backtest_origins
                                          position, scored against history

Forward and backtest forecasts go through the same input builders, called with
a history position: the forward forecast is simply the last position. A context
therefore never holds a value past its origin (test_forecast_lab.py checks it).

Every forecast is zero-shot amazon/chronos-2 with cross-learning OFF, points
are E[X] integrated from the quantiles (chronos_model._mean_from_quantiles) --
the same settings as the leak-free benchmark -- and the band is the model's own
10th-90th percentile, but only where that is a real interval for the number
drawn: a forecast of one series at the grain shown. Per-block (or per-week)
percentiles do not add up to a percentile of the sum -- summed they give a band
several times too wide -- so the block sum, and the estate-weekly model's
monthly view, carry no band.

Rebuild whenever the estate data changes (the page flags artifacts whose anchor
no longer matches the latest data; forecast/lab/shadow.py --rebuild runs this):
    C:\\Users\\DP\\anaconda3\\envs\\chronos-rd-py313\\python.exe forecast/lab/build_artifacts.py
    ... --models chronos2_blocks --estates k3 ec --grains week --no-backtest
"""

import argparse
import importlib.util
import json
import math
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

_LAB = os.path.dirname(os.path.abspath(__file__))
_FORECAST = os.path.dirname(_LAB)

CHECKPOINT = "amazon/chronos-2"
QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
MIN_BLOCK_WEEKS = 4
BAND_80 = "80% interval (model's 10th-90th percentile)"


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load_by_path("epms_lab_registry", os.path.join(_LAB, "registry.py"))
cm = _load_by_path("epms_chronos_model", os.path.join(_FORECAST, "chronos_model.py"))
bw = reg.bw


class Skip(Exception):
    """This model cannot run on this estate/grain; the message says why."""


_PIPE = None


def _pipeline():
    global _PIPE
    if _PIPE is None:
        from chronos import Chronos2Pipeline
        _PIPE = Chronos2Pipeline.from_pretrained(CHECKPOINT, device_map="cpu")
    return _PIPE


def _predict(inputs, horizon):
    """[(mean, q10, q90)] per input, each (horizon,), clipped at zero."""
    q, _ = _pipeline().predict_quantiles(
        inputs, prediction_length=horizon, quantile_levels=QUANTILE_LEVELS,
        batch_size=256, cross_learning=False)
    out = []
    for t in q:
        m = t[0].float().numpy()
        out.append(tuple(np.clip(a, 0.0, None) for a in
                         (cm._mean_from_quantiles(m), m[:, 0], m[:, -1])))
    return out


# -- inputs at a history position ------------------------------------------------------
_MM = _TFE = None


def _weather_mods():
    global _MM, _TFE
    if _MM is None:
        _MM = _load_by_path("epms_monthly_model", os.path.join(_FORECAST, "monthly_model.py"))
        _TFE = _load_by_path("epms_timesfm_experiment",
                             os.path.join(_FORECAST, "timesfm_experiment.py"))
    return _MM, _TFE


def monthly_input(estate, i, cov):
    """Chronos input for an origin at month-history position i (context <= i)."""
    ctx = reg.context_until(estate, "month", i).astype(np.float32)
    if not cov:
        return ctx, []
    weather = reg.ESTATES[estate]["weather"]
    if not weather or not os.path.exists(weather):
        raise Skip("no weather history for this estate")
    mm, tfe = _weather_mods()
    h = reg.month_history(estate).iloc[:i + 1]
    hist_months = list(h.loc[h["context_ok"], "period"])
    anchor = hist_months[-1]
    future_months = reg.future_periods(anchor, "month")
    # information-at-origin, exactly as chronos_experiment.run_monthly
    wm = mm.build_weather_monthly(weather, known_through=anchor, extend=reg.MAX_H)
    pf, po, pf_names, po_names = tfe._split_covariates(wm, hist_months, future_months)
    L = len(ctx)
    past = {n: pf[k][:L].astype(np.float32) for k, n in enumerate(pf_names)}
    past.update({n: po[k].astype(np.float32) for k, n in enumerate(po_names)})
    future = {n: pf[k][L:L + reg.MAX_H].astype(np.float32) for k, n in enumerate(pf_names)}
    notes = [f"known ahead: {', '.join(pf_names)}", f"past only: {', '.join(po_names)}"]
    return {"target": ctx, "past_covariates": past, "future_covariates": future}, notes


def weekly_inputs(estate, grain, i, per_block):
    """Weekly series cut at the origin at history position i, and what to forecast.

    Returns (series list, cutoff day, weeks to forecast). At the month grain the
    cutoff is the origin month's last day and enough weeks are forecast to cover
    the next MAX_H months; at the week grain it is the origin bin's last day.
    """
    daily = reg.daily_blocks(estate)
    anchor = reg.history(estate, grain)["period"].iloc[i]
    if grain == "week":
        cutoff, weeks = pd.Timestamp(anchor), reg.MAX_H
    else:
        cutoff = anchor.end_time.normalize()
        weeks = math.ceil(((anchor + reg.MAX_H).end_time.normalize() - cutoff).days / 7)
    wk = bw.weekly_bins(daily, cutoff)
    if per_block:
        series = list(bw.block_series(wk, min_weeks=MIN_BLOCK_WEEKS).values())
    else:
        total = wk.sum(axis=1).to_numpy(dtype=float)
        nz = np.flatnonzero(total > 0)
        series = [total[nz[0]:]] if nz.size else []
    return [s.astype(np.float32) for s in series], cutoff, weeks


def to_grain(values, grain, cutoff, anchor):
    """Weekly values -> the grain's MAX_H steps (months: spread weeks over days)."""
    if grain == "week":
        return np.asarray(values[:reg.MAX_H])
    return bw.daily_to_months(values, cutoff).reindex(
        reg.future_periods(anchor, "month")).to_numpy()


def _forecast_positions(model, estate, grain, positions):
    """Forecast from each history position: [(mean, lo, hi, context_n, notes)] in grain steps."""
    h = reg.history(estate, grain)
    if model in ("chronos2_monthly", "chronos2_monthly_cov"):
        built = [monthly_input(estate, i, model == "chronos2_monthly_cov") for i in positions]
        preds = _predict([b[0] for b in built], reg.MAX_H)
        out = []
        for (inp, notes), (mean, lo, hi) in zip(built, preds):
            n = len(inp["target"] if isinstance(inp, dict) else inp)
            out.append((mean, lo, hi, n, [f"{n} months of context"] + notes))
        return out

    per_block = model == "chronos2_blocks"
    built = [weekly_inputs(estate, grain, i, per_block) for i in positions]
    if not any(b[0] for b in built):
        raise Skip("no weekly series before the cutoff")
    flat = [s for b in built for s in b[0]]
    preds = _predict(flat, max(b[2] for b in built))
    out, k = [], 0
    for i, (series, cutoff, weeks) in zip(positions, built):
        if not series:          # nothing recorded yet at this origin: no forecast
            out.append(None)
            continue
        part = preds[k:k + len(series)]
        k += len(series)
        mean, lo, hi = (np.sum([p[j][:weeks] for p in part], axis=0) for j in range(3))
        anchor = h["period"].iloc[i]
        mean, lo, hi = (to_grain(a, grain, cutoff, anchor) for a in (mean, lo, hi))
        lens = [len(s) for s in series]
        notes = ([f"{len(series)} blocks, median {int(np.median(lens))} weeks of context each"]
                 if per_block else [f"{lens[0]} weeks of context"])
        if per_block and grain == "month" and reg.ESTATES[estate]["features"]:
            notes.append("history is the cleaned monthly target; block series are raw "
                         "recorded harvest")
        out.append((mean, lo, hi, len(series) if per_block else lens[0], notes))
    return out


def _has_band(model, grain):
    return model in ("chronos2_monthly", "chronos2_monthly_cov") or (
        model == "chronos2_estate_weekly" and grain == "week")


# -- writers ----------------------------------------------------------------------------
def forward(model, estate, grain):
    h = reg.history(estate, grain)
    i = len(h) - 1
    res = _forecast_positions(model, estate, grain, [i])[0]
    if res is None:
        raise Skip("no weekly series before the cutoff")
    mean, lo, hi, n, notes = res
    band = BAND_80 if _has_band(model, grain) else None
    if band is None:
        lo = hi = [None] * len(mean)
    rnd = lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v))
    anchor = h["period"].iloc[i]
    return {
        "model": model, "estate": estate, "grain": grain,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_through": reg.period_str(anchor, grain),
        "anchor": {"period": reg.period_str(anchor, grain), "value": round(float(h["value"].iloc[i]))},
        "context_n": int(n), "notes": notes,
        "band": {"label": band} if band else None,
        "steps": [{"period": reg.period_str(p, grain), "value": round(float(v)),
                   "lower": rnd(a), "upper": rnd(b)}
                  for p, v, a, b in zip(reg.future_periods(anchor, grain), mean, lo, hi)],
    }


def backtest(model, estate, grain):
    positions = reg.backtest_origins(estate, grain)
    if not positions:
        raise Skip(f"no origin has {reg.MIN_BACKTEST_CONTEXT[grain]} {grain}s of history")
    rows = []
    for i, res in zip(positions, _forecast_positions(model, estate, grain, positions)):
        if res is not None:
            rows += reg.backtest_rows(estate, grain, i, res[0])
    return pd.DataFrame(rows, columns=reg.BACKTEST_COLS)


ARTIFACT_MODELS = [m["id"] for m in reg.MODELS if m["source"] == "artifact"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=ARTIFACT_MODELS, choices=ARTIFACT_MODELS)
    ap.add_argument("--estates", nargs="+", default=list(reg.ESTATES), choices=list(reg.ESTATES))
    ap.add_argument("--grains", nargs="+", default=["month", "week"], choices=["month", "week"])
    ap.add_argument("--no-backtest", action="store_true",
                    help="forward forecasts only (backtests take a few minutes)")
    args = ap.parse_args()

    manifest = reg.read_manifest() or {"entries": {}}
    jobs = [("forward", forward)] + ([] if args.no_backtest else [("backtest", backtest)])
    t_all = time.perf_counter()
    for model in args.models:
        for estate in args.estates:
            for grain in args.grains:
                for kind, fn in jobs:
                    key = f"{'' if kind == 'forward' else 'backtest/'}{model}/{estate}_{grain}"
                    path = (reg.artifact_path(model, estate, grain) if kind == "forward"
                            else reg.backtest_path(model, estate, grain))
                    ok, reason = reg.static_availability(model, estate, grain)
                    if not ok:
                        if os.path.exists(path):
                            os.remove(path)
                        manifest["entries"][key] = {"status": "skipped", "reason": reason}
                        continue
                    t0 = time.perf_counter()
                    try:
                        out = fn(model, estate, grain)
                    except Skip as exc:
                        manifest["entries"][key] = {"status": "skipped", "reason": str(exc)}
                        print(f"  skip  {key}: {exc}")
                        continue
                    except Exception as exc:
                        manifest["entries"][key] = {"status": "error",
                                                    "reason": f"{type(exc).__name__}: {exc}"}
                        print(f"  ERROR {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
                        continue
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    if kind == "forward":
                        with open(path, "w", encoding="utf-8") as fh:
                            json.dump(out, fh, indent=1)
                        info = f"anchor {out['anchor']['period']}"
                    else:
                        out.to_csv(path, index=False)
                        info = f"{out['origin'].nunique()} origins, {len(out)} rows"
                    manifest["entries"][key] = {"status": "ok",
                                                "generated_at": datetime.now().isoformat(timespec="seconds")}
                    print(f"  ok    {key}: {info}, {time.perf_counter() - t0:.1f}s", flush=True)

    manifest.update({"built_at": datetime.now().isoformat(timespec="seconds"),
                     "checkpoint": CHECKPOINT, "cross_learning": False})
    os.makedirs(reg.ARTIFACT_DIR, exist_ok=True)
    with open(os.path.join(reg.ARTIFACT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    print(f"done in {time.perf_counter() - t_all:.0f}s -> {reg.ARTIFACT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
