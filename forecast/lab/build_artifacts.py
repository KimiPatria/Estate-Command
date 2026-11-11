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
    ... --source staging                  # only the staging-database estates
    ... --source staging --remote         # the model step as one SageMaker GPU job
    ... --source staging --recover-job chronos2-lab-build-<timestamp>

--remote needs a live `aws login` session and this same Python 3.13 env (the job
image is matched to it; see chronos_finetune_sagemaker.remote_job). It uploads
the series being forecast (bunch counts per block and week) to the account's
SageMaker bucket. Only the model step moves: inputs are built and artifacts
written here either way, so a local and a remote build give the same files.
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
JOB_PREFIX = "chronos2-lab-build"


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


class _Deferred(Exception):
    """Raised by _predict while requests are being collected for one model run."""


# -- the model step ----------------------------------------------------------------------
# Everything around the model is pandas and runs here; only this function needs
# Chronos. A build collects every forecast request first, runs them all through
# ONE call of it, then writes the artifacts -- so the same code serves a local
# build (the call runs in this process) and a SageMaker build (--remote: the
# call runs as a GPU training job and its return value comes back).
def _chronos_job(requests, checkpoint, quantile_levels, batch_size=256):
    """Zero-shot Chronos-2 quantiles for a list of requests.

    requests  [(inputs, horizon)]: inputs is a list of series (arrays, or dicts
              with target/past_covariates/future_covariates), as
              Chronos2Pipeline.predict_quantiles takes them.
    Returns {"device", "seconds", "n_series", "quantiles"}, quantiles[k][j] an
    array (horizon, len(quantile_levels)) for request k's series j.

    Self-contained on purpose (imports inside, no module globals): with --remote
    it is shipped by value to the job, which has none of this repo's modules.
    """
    import time as _time

    import numpy as _np
    import torch
    from chronos import Chronos2Pipeline

    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipe = Chronos2Pipeline.from_pretrained(checkpoint, device_map=device)
    t0, out, n = _time.perf_counter(), [], 0
    for k, (inputs, horizon) in enumerate(requests):
        q, _ = pipe.predict_quantiles(
            inputs, prediction_length=horizon, quantile_levels=quantile_levels,
            batch_size=batch_size, cross_learning=False)
        out.append([t[0].float().cpu().numpy().astype(_np.float32) for t in q])
        n += len(inputs)
        print(f"  request {k + 1}/{len(requests)}: {len(inputs)} series x {horizon} steps, "
              f"{_time.perf_counter() - t0:.0f}s elapsed", flush=True)
    return {"device": device, "seconds": round(_time.perf_counter() - t0, 1),
            "n_series": n, "quantiles": out}


class _Batch:
    """The two passes around one model run.

    collect  _predict files the request under the current artifact key and
             raises _Deferred, so the producer stops right where the model
             would have run.
    replay   _predict hands back that key's quantiles and the producer carries
             on to the end. Inputs are rebuilt on the second pass (cheap) and
             checked against what was sent.
    """
    mode = None            # None = no batch: _predict runs the model directly
    key = None
    requests = {}
    results = {}


def _post(quantiles):
    """(mean, q10, q90) per series, each (horizon,), clipped at zero."""
    return [tuple(np.clip(a, 0.0, None) for a in
                  (cm._mean_from_quantiles(m), m[:, 0], m[:, -1])) for m in quantiles]


def _predict(inputs, horizon):
    """[(mean, q10, q90)] per input, each (horizon,), clipped at zero."""
    if _Batch.mode == "collect":
        _Batch.requests[_Batch.key] = (inputs, horizon)
        raise _Deferred()
    if _Batch.mode == "replay":
        got = _Batch.results[_Batch.key]
        if len(got) != len(inputs) or (got and got[0].shape[0] != horizon):
            raise RuntimeError(f"{_Batch.key}: the model run returned {len(got)} series, "
                               f"this pass built {len(inputs)} (horizon {horizon})")
        return _post(got)
    return _post(_chronos_job([(inputs, horizon)], CHECKPOINT, QUANTILE_LEVELS)["quantiles"][0])


def _sagemaker():
    """chronos_finetune_sagemaker.py: the one @remote job configuration proven to work."""
    return _load_by_path("epms_chronos_sagemaker",
                         os.path.join(_FORECAST, "chronos_finetune_sagemaker.py"))


def run_remote(requests, args):
    """Run _chronos_job as a SageMaker training job; blocks until it returns."""
    v1 = _sagemaker()
    s3_root = (args.s3_bucket or f"{v1.DEFAULT_S3_ROOT}/chronos2-lab").rstrip("/")
    fn = v1.remote_job(_chronos_job, args.role_arn or v1.DEFAULT_ROLE_ARN, s3_root,
                       instance_type=args.instance_type, region=args.region or v1.DEFAULT_REGION,
                       max_runtime_in_seconds=args.max_runtime_s,
                       job_name_prefix=JOB_PREFIX)
    print(f"Launching SageMaker job on {args.instance_type} (blocks until done). If this "
          f"process dies while it runs, collect it with --recover-job <job name>.", flush=True)
    return fn(requests, CHECKPOINT, QUANTILE_LEVELS)


def recover_remote(job_name, args):
    """Return value of a finished job whose local call died (expired `aws login`).

    The job keeps running on AWS regardless; @remote leaves its return value
    under jobs/<job>/results, checksum-verified on read.
    """
    import boto3
    from sagemaker.core.helper.session_helper import Session as SageMakerSession
    from sagemaker.core.remote_function.core.serialization import deserialize_obj_from_s3

    v1 = _sagemaker()
    region = args.region or v1.DEFAULT_REGION
    status = boto3.client("sagemaker", region_name=region).describe_training_job(
        TrainingJobName=job_name)["TrainingJobStatus"]
    if status != "Completed":
        raise SystemExit(f"{job_name} is {status}, not Completed")
    s3_root = (args.s3_bucket or f"{v1.DEFAULT_S3_ROOT}/chronos2-lab").rstrip("/")
    session = SageMakerSession(boto_session=boto3.Session(region_name=region))
    return deserialize_obj_from_s3(session, f"{s3_root}/jobs/{job_name}/results")


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
    ap.add_argument("--source", choices=list(reg.SOURCES), default=None,
                    help="only the estates cut from this source (default: every estate)")
    ap.add_argument("--remote", action="store_true",
                    help="run the model step as one SageMaker GPU job instead of on this machine")
    ap.add_argument("--recover-job", default=None, metavar="JOB_NAME",
                    help="collect a finished --remote job whose local call died; pass the "
                         "same --models/--estates/--grains/--source as the launch")
    ap.add_argument("--instance-type", default="ml.g5.xlarge")
    ap.add_argument("--role-arn", default=None, help="default: chronos_finetune_sagemaker's")
    ap.add_argument("--s3-bucket", default=None, help="default: <account bucket>/chronos2-lab")
    ap.add_argument("--region", default=None, help="default: chronos_finetune_sagemaker's")
    ap.add_argument("--max-runtime-s", type=int, default=3600)
    args = ap.parse_args()
    estates = [e for e in args.estates
               if args.source is None or reg.ESTATES[e]["source"] == args.source]

    manifest = reg.read_manifest() or {"entries": {}}
    jobs = [("forward", forward)] + ([] if args.no_backtest else [("backtest", backtest)])
    t_all = time.perf_counter()

    def note(key, status, reason):
        manifest["entries"][key] = {"status": status, "reason": reason}

    # Pass 1: build every producer's inputs, stopping where the model would run.
    todo = []
    _Batch.requests, _Batch.results = {}, {}
    _Batch.mode = "collect"
    for model in args.models:
        for estate in estates:
            for grain in args.grains:
                for kind, fn in jobs:
                    key = f"{'' if kind == 'forward' else 'backtest/'}{model}/{estate}_{grain}"
                    path = (reg.artifact_path(model, estate, grain) if kind == "forward"
                            else reg.backtest_path(model, estate, grain))
                    ok, reason = reg.static_availability(model, estate, grain)
                    if not ok:
                        if os.path.exists(path):
                            os.remove(path)
                        note(key, "skipped", reason)
                        continue
                    _Batch.key = key
                    try:
                        fn(model, estate, grain)
                    except _Deferred:
                        todo.append((key, kind, fn, model, estate, grain, path))
                    except Skip as exc:
                        note(key, "skipped", str(exc))
                        print(f"  skip  {key}: {exc}")
                    except Exception as exc:
                        note(key, "error", f"{type(exc).__name__}: {exc}")
                        print(f"  ERROR {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
    _Batch.mode = None

    # The model run: every request in one call, here or on SageMaker.
    run_info = {"where": "local"}
    if todo:
        requests = [_Batch.requests[t[0]] for t in todo]
        n_series = sum(len(r[0]) for r in requests)
        print(f"{len(requests)} artifacts to build, {n_series:,} series in all "
              f"(inputs ready in {time.perf_counter() - t_all:.0f}s)", flush=True)
        if args.recover_job:
            res = recover_remote(args.recover_job, args)
            run_info = {"where": "sagemaker", "job": args.recover_job, "recovered": True}
        elif args.remote:
            res = run_remote(requests, args)
            run_info = {"where": "sagemaker", "instance_type": args.instance_type}
        else:
            res = _chronos_job(requests, CHECKPOINT, QUANTILE_LEVELS)
        if len(res["quantiles"]) != len(requests):
            raise SystemExit(f"the model run returned {len(res['quantiles'])} results for "
                             f"{len(requests)} requests: not the build this command describes")
        run_info.update({k: res[k] for k in ("device", "seconds", "n_series")})
        print(f"model run: {res['n_series']:,} series on {res['device']} in {res['seconds']:.0f}s",
              flush=True)
        _Batch.results = {t[0]: q for t, q in zip(todo, res["quantiles"])}

    # Pass 2: carry each producer through with its results and write the artifact.
    _Batch.mode = "replay"
    for key, kind, fn, model, estate, grain, path in todo:
        _Batch.key = key
        try:
            out = fn(model, estate, grain)
        except Skip as exc:
            note(key, "skipped", str(exc))
            print(f"  skip  {key}: {exc}")
            continue
        except Exception as exc:
            note(key, "error", f"{type(exc).__name__}: {exc}")
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
        print(f"  ok    {key}: {info}", flush=True)
    _Batch.mode = None

    manifest.update({"built_at": datetime.now().isoformat(timespec="seconds"),
                     "checkpoint": CHECKPOINT, "cross_learning": False, "last_run": run_info})
    os.makedirs(reg.ARTIFACT_DIR, exist_ok=True)
    with open(os.path.join(reg.ARTIFACT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    print(f"done in {time.perf_counter() - t_all:.0f}s -> {reg.ARTIFACT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
