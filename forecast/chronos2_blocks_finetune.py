"""
Chronos-2 fine-tuning v2 -- LoRA on block-level WEEKLY series from other
estates, scored on an estate the fine-tune never saw.

Why v2
------
v1 (chronos_finetune_sagemaker.py) pooled k3's first 60% of months with EC's
4 months: ~25 monthly points in two series. It scored MASE 0.564 against
zero-shot's 0.549 on the 14 folds left after its cutoff -- no gain, and it cost
the benchmark half its folds, because k3 had to be split between training and
testing.

v2 trains on the blocks of EC (292 x ~80 weeks) and EB (356 x ~34 weeks) -- see
block_weekly.py for why that grain -- and keeps k3 entirely out of training, so
all 30 k3 walk-forward folds are available as an unseen-estate test.

Roles of the data
-----------------
train       every EC/EB block series minus its last HORIZON_WEEKS weeks.
validation  the same series in full; Chronos2Pipeline.fit's VALIDATION mode
            scores the last HORIZON_WEEKS weeks from the context before them.
            Used ONLY to pick the checkpoint (eval every --eval-steps, early
            stop after --patience evals without improvement, best reloaded).
test        k3 (default --test-estate), walk-forward from each month-end origin,
            per-block weekly forecasts summed to the estate and spread into
            calendar months. Never used to choose anything.

Nothing is tuned on k3. The configurations run are fixed on the command line
up front, and every seed is reported, not the best one.

Calendar overlap
----------------
EC's data starts 2025-01-01. k3 folds whose target month ends before the first
training day share no calendar time with the fine-tuning corpus at all
("clean"); later folds could in principle carry a region-wide shock (a dry
spell hitting every estate at once) from training into test ("overlap"). Both
subsets are reported separately.

Cross-learning is off everywhere
--------------------------------
Every inference call here passes cross_learning=False. With it on, series in
one batch attend to each other; in a walk-forward batch that lets an early
origin see later data. It inflated Phase 1 (see cm.forecast_batch).

Scoring target
--------------
k3 is scored against features_estate_monthly.csv's bunches_total -- the same
actuals, months and SeasonalNaive MASE denominator as the incumbent's
scoreboard. That target is completeness-scaled on a few under-recorded months
while the block series are raw recorded harvest; the report lists those months.

Horizon
-------
HORIZON_WEEKS = 14 (98 days) covers the three calendar months after any
month-end origin (at most 92 days) -- the served forecast_monthly_next3 range.
Steps 1-3 are scored; step 1 is the headline, as on the existing scoreboard.

Usage (chronos-rd-py313 env; SageMaker needs `aws login` credentials):
    # baseline only, no training: leak-free zero-shot on the block track
    python forecast/chronos2_blocks_finetune.py --zero-shot-only

    # local CPU smoke of the whole train -> save -> load -> evaluate path
    python forecast/chronos2_blocks_finetune.py --local --max-steps 20 --eval-steps 10 --seeds 0

    # the real run: 3 seeds on one SageMaker job, then evaluate on k3
    python forecast/chronos2_blocks_finetune.py

    # the local process died mid-job (e.g. `aws login` expired): collect and evaluate
    python forecast/chronos2_blocks_finetune.py --run-dir forecast/experiments/chronos2_blocks/<run> \\
        --recover-job chronos2-blocks-lora-<timestamp>

    # re-evaluate an existing run, or on another estate (leave-one-estate-out)
    python forecast/chronos2_blocks_finetune.py --run-dir forecast/experiments/chronos2_blocks/<run>
    python forecast/chronos2_blocks_finetune.py --train-estates EC --test-estate EB
"""

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time

import numpy as np
import pandas as pd

# Same Windows cp1252 guard as chronos_finetune_sagemaker.py: SageMaker log
# streaming prints characters the legacy codepage cannot encode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_DIR)
OUT_ROOT = os.path.join(_DIR, "experiments", "chronos2_blocks")

CHECKPOINT = "amazon/chronos-2"
MLFLOW_EXPERIMENT = "ffb-chronos2-rd"
QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
HORIZON_WEEKS = 14
SCORED_STEPS = (1, 2, 3)
MIN_PAST = 4           # EB blocks have 32-34 weeks: 14 validation + 14 target + 4
MIN_TEST_CONTEXT = 4   # weeks a block needs before an origin to be forecast
LORA_TARGETS = ["self_attention.q", "self_attention.v", "self_attention.k",
                "self_attention.o", "output_patch_embedding.output_layer"]
INCUMBENT = "Ensemble_workdone"
T_CRIT = 2.045         # two-sided 5%, df ~29


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bw = _load_by_path("epms_block_weekly", os.path.join(_DIR, "block_weekly.py"))
cm = _load_by_path("epms_chronos_model", os.path.join(_DIR, "chronos_model.py"))
v1 = _load_by_path("epms_chronos_ft_v1", os.path.join(_DIR, "chronos_finetune_sagemaker.py"))
mm, ce = v1.mm, v1.ce


# -- corpus (local, no AWS) -----------------------------------------------------
def build_corpus(estates, horizon=HORIZON_WEEKS, min_past=MIN_PAST):
    """Block series for fine-tuning, split by time.

    Returns (train_inputs, val_inputs, meta). train_inputs[i] is val_inputs[i]
    without its last `horizon` weeks, so the validation target is always
    later than anything a gradient step sees for that block.
    """
    train, val, meta = [], [], {}
    for est in estates:
        daily = bw.load_daily_blocks(est)
        wk = bw.weekly_bins(daily, daily.index.max())
        series = bw.block_series(wk, min_weeks=min_past + 2 * horizon)
        n_all = len(bw.block_series(wk))
        for s in series.values():
            train.append(s[:-horizon])
            val.append(s)
        lens = [len(s) for s in series.values()]
        meta[est] = {
            "first_day": str(daily.index.min().date()),
            "last_day": str(daily.index.max().date()),
            "val_from": str((wk.index[-horizon] - pd.Timedelta(days=6)).date()),
            "n_blocks": len(series), "n_blocks_too_short": n_all - len(series),
            "weeks_min": int(min(lens)), "weeks_max": int(max(lens)),
            "train_weeks_total": int(sum(lens) - horizon * len(lens)),
        }
    return train, val, meta


# -- the job body (runs on SageMaker, or locally with --local) --------------------
def _fit_lora_seeds(train_inputs, val_inputs, cfg, dest):
    """Fine-tune one LoRA adapter per seed and ship them to `dest`.

    Self-contained on purpose: it runs inside the SageMaker container, which
    has none of this repo's modules. `dest` is an s3:// URI for a tar.gz of
    all adapters, or a local directory (the --local smoke path).

    Returns {"dest", "device", "seeds": {seed: {best_step, best_eval_loss,
    last_step, seconds, history}}}.
    """
    import os
    import shutil
    import tarfile
    import tempfile
    import time

    import numpy as np
    import torch
    from chronos import Chronos2Pipeline
    from transformers import EarlyStoppingCallback, TrainerCallback, set_seed

    class _Log(TrainerCallback):
        def __init__(self):
            self.history = []

        def on_log(self, args, state, control, logs=None, **kwargs):
            if logs:
                self.history.append({"step": int(state.global_step),
                                     **{k: float(v) for k, v in logs.items()
                                        if isinstance(v, (int, float))}})

    device = "cuda" if torch.cuda.is_available() else "cpu"
    base = Chronos2Pipeline.from_pretrained(cfg["checkpoint"], device_map=device)
    train = [np.asarray(x, dtype=np.float32) for x in train_inputs]
    val = [np.asarray(x, dtype=np.float32) for x in val_inputs]

    work = tempfile.mkdtemp(prefix="chronos2_blocks_")
    out = {"device": device, "seeds": {}}
    for seed in cfg["seeds"]:
        set_seed(seed)
        log = _Log()
        run_dir = os.path.join(work, f"run_{seed}")
        t0 = time.time()
        base.fit(
            inputs=train, prediction_length=cfg["horizon"], validation_inputs=val,
            finetune_mode="lora",
            lora_config={"r": cfg["lora_r"], "lora_alpha": 2 * cfg["lora_r"],
                         "target_modules": cfg["lora_targets"]},
            context_length=cfg["context_length"], learning_rate=cfg["lr"],
            num_steps=cfg["max_steps"], batch_size=cfg["batch_size"],
            output_dir=run_dir, min_past=cfg["min_past"],
            finetuned_ckpt_name="adapter", remove_printer_callback=True,
            callbacks=[log, EarlyStoppingCallback(early_stopping_patience=cfg["patience"])],
            eval_steps=cfg["eval_steps"], save_steps=cfg["eval_steps"],
            logging_steps=cfg["eval_steps"], seed=seed, disable_tqdm=True,
        )
        evals = [h for h in log.history if "eval_loss" in h]
        best = min(evals, key=lambda h: h["eval_loss"]) if evals else {}
        out["seeds"][seed] = {
            "best_step": best.get("step"), "best_eval_loss": best.get("eval_loss"),
            "last_step": max((h["step"] for h in log.history), default=0),
            "seconds": round(time.time() - t0, 1), "history": log.history,
        }
        print(f"seed {seed}: best eval_loss {best.get('eval_loss')} at step "
              f"{best.get('step')}, stopped at {out['seeds'][seed]['last_step']}, "
              f"{time.time() - t0:.0f}s", flush=True)

    if dest.startswith("s3://"):
        import boto3
        tar_path = os.path.join(work, "adapters.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            for seed in cfg["seeds"]:
                tar.add(os.path.join(work, f"run_{seed}", "adapter"), arcname=f"seed_{seed}")
        bucket, key = dest[len("s3://"):].split("/", 1)
        boto3.client("s3").upload_file(tar_path, bucket, key)
    else:
        os.makedirs(dest, exist_ok=True)
        for seed in cfg["seeds"]:
            shutil.copytree(os.path.join(work, f"run_{seed}", "adapter"),
                            os.path.join(dest, f"seed_{seed}"), dirs_exist_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    out["dest"] = dest
    return out


def recover_job(args, run_dir, job_name):
    """Collect a finished job's adapters and return value when the local call died.

    `aws login` credentials can fail to refresh while @remote is still polling
    (seen on the first v2 run: CreateOAuth2Token rejected mid-job). The job
    keeps running on AWS regardless, so nothing is lost: the adapters are at
    the run's artifact URI and @remote leaves the function's return value
    (the training log) under jobs/<job>/results, SHA-256 checked on read.
    """
    import boto3
    from sagemaker.core.helper.session_helper import Session as SageMakerSession
    from sagemaker.core.remote_function.core.serialization import deserialize_obj_from_s3

    sm = boto3.client("sagemaker", region_name=args.region)
    status = sm.describe_training_job(TrainingJobName=job_name)["TrainingJobStatus"]
    if status != "Completed":
        raise RuntimeError(f"{job_name} is {status}, not Completed")
    s3_root = args.s3_bucket.rstrip("/")
    run_name = os.path.basename(os.path.normpath(run_dir))
    _download_adapters(f"{s3_root}/artifacts/{run_name}/adapters.tar.gz",
                       os.path.join(run_dir, "adapters"), args.region)
    session = SageMakerSession(boto_session=boto3.Session(region_name=args.region))
    result = deserialize_obj_from_s3(session, f"{s3_root}/jobs/{job_name}/results")
    _write_json(os.path.join(run_dir, "train_log.json"), result)
    print(f"recovered {job_name}: adapters + training log into {run_dir}")


def _download_adapters(s3_uri, adapters_dir, region):
    import boto3
    os.makedirs(adapters_dir, exist_ok=True)
    tar_path = os.path.join(adapters_dir, "adapters.tar.gz")
    bucket, key = s3_uri[len("s3://"):].split("/", 1)
    boto3.client("s3", region_name=region).download_file(bucket, key, tar_path)
    with tarfile.open(tar_path) as tar:
        tar.extractall(adapters_dir, filter="data")
    os.remove(tar_path)


# -- inference (local) -------------------------------------------------------------
def load_pipeline(adapter_dir=None):
    """Base Chronos-2, or base + a LoRA adapter directory written by the job."""
    from chronos import Chronos2Pipeline
    return Chronos2Pipeline.from_pretrained(adapter_dir or CHECKPOINT, device_map="cpu")


def forecast_means(pipe, contexts, horizon, batch_size=256):
    """E[X] per week for each context, cross-learning OFF.

    The pipeline's own "mean" is the median; summing weekly medians into a
    month undershoots on zero-heavy block series, so E[X] is integrated from
    the quantiles exactly as chronos_model does for the daily track.
    """
    q, _ = pipe.predict_quantiles(
        [np.asarray(c, dtype=np.float32) for c in contexts], prediction_length=horizon,
        quantile_levels=QUANTILE_LEVELS, batch_size=batch_size, cross_learning=False)
    return [np.clip(cm._mean_from_quantiles(t[0].float().numpy()), 0.0, None) for t in q]


# -- the test estate ------------------------------------------------------------------
def test_folds(estate, daily):
    """Origins, actuals and incumbent rows for walk-forward scoring.

    k3: the incumbent scoreboard's own 30 folds -- step-1 months are exactly
    predictions_monthly.csv's SeasonalNaive months, origins the month before,
    actuals the cleaned bunches_total; incumbent multi-step rows come from
    predictions_multih.csv (the served ensemble's walk-forward).
    Any other estate: every month fully inside its recording window with at
    least 8 weeks of history before the origin; actuals are recorded totals;
    no incumbent exists.
    """
    if estate == "k3":
        feats = mm.load_estate(os.path.join(_DIR, "features_estate_monthly.csv"))
        actual = feats.set_index("month")["bunches_total"].astype(float)
        scoreable = set(feats.loc[feats["exclude_from_model"] == 0, "month"])
        imputed = sorted(str(m) for m in feats.loc[
            (feats["exclude_from_model"] == 0) & (feats["completeness_scale"] != 1), "month"])
        pm = pd.read_csv(os.path.join(_DIR, "predictions_monthly.csv"))
        months = sorted(pd.Period(m, freq="M") for m in
                        pm.loc[pm["model"] == "SeasonalNaive", "month"])
        inc = pd.read_csv(os.path.join(_DIR, "predictions_multih.csv"))
        inc = inc.rename(columns={"pred": "pred_incumbent"})[
            ["origin", "step", "month", "pred_incumbent"]]
        return {"origins": [m - 1 for m in months], "actual": actual,
                "scoreable": scoreable, "incumbent_multih": inc,
                "incumbent_monthly": pm, "imputed_months": imputed}

    first, last = daily.index.min(), daily.index.max()
    tot = daily.sum(axis=1)
    months = pd.period_range(first.to_period("M"), last.to_period("M"), freq="M")
    full = [m for m in months if m.start_time >= first and m.end_time.normalize() <= last]
    actual = tot.groupby(tot.index.to_period("M")).sum().astype(float)
    origins = [m - 1 for m in full
               if (m - 1).end_time.normalize() - first >= pd.Timedelta(weeks=8)]
    return {"origins": origins, "actual": actual, "scoreable": set(full),
            "incumbent_multih": None, "incumbent_monthly": None, "imputed_months": []}


def walk_forward(pipes, daily, folds, horizon=HORIZON_WEEKS):
    """One row per (origin, step, arm-track): block-sum and estate-weekly preds.

    pipes   {arm_label: pipeline}. Contexts are built once and reused by every
            arm, so all arms see byte-identical inputs.
    """
    cutoffs, blk_ctx, blk_fold, est_ctx = [], [], [], []
    for i, origin in enumerate(folds["origins"]):
        cutoff = origin.to_timestamp(how="end").normalize()
        wk = bw.weekly_bins(daily, cutoff)
        for s in bw.block_series(wk, min_weeks=MIN_TEST_CONTEXT).values():
            blk_ctx.append(s)
            blk_fold.append(i)
        est = wk.sum(axis=1).to_numpy()
        est_ctx.append(est[np.flatnonzero(est > 0)[0]:])
        cutoffs.append(cutoff)
    blk_fold = np.asarray(blk_fold)

    rows = []
    for label, pipe in pipes.items():
        t0 = time.perf_counter()
        blk = np.stack(forecast_means(pipe, blk_ctx, horizon))
        est = forecast_means(pipe, est_ctx, horizon)
        for i, origin in enumerate(folds["origins"]):
            by_month = {
                "blocks": bw.daily_to_months(blk[blk_fold == i].sum(axis=0), cutoffs[i]),
                "estate": bw.daily_to_months(est[i], cutoffs[i]),
            }
            for step in SCORED_STEPS:
                m = origin + step
                if m not in folds["scoreable"] or m not in folds["actual"].index:
                    continue
                for track, series in by_month.items():
                    rows.append(dict(arm=label, track=track, origin=str(origin), step=step,
                                     month=str(m), actual=float(folds["actual"].loc[m]),
                                     pred=float(series.loc[m])))
        print(f"  {label}: {len(blk_ctx)} block + {len(est_ctx)} estate contexts, "
              f"{time.perf_counter() - t0:.0f}s", flush=True)
    return pd.DataFrame(rows)


def in_domain_check(pipes, estates, horizon=HORIZON_WEEKS):
    """Forecast each training estate's validation tail from the weeks before it.

    These windows chose the checkpoint, so the fine-tuned arms are optimistic
    here by construction. The question it answers is narrower: did the
    fine-tune learn anything at all on data like its own?
    """
    _, val, meta = build_corpus(estates, horizon)
    idx = np.cumsum([0] + [meta[e]["n_blocks"] for e in estates])
    rows = []
    for label, pipe in pipes.items():
        preds = forecast_means(pipe, [s[:-horizon] for s in val], horizon)
        for k, est in enumerate(estates):
            p = np.stack(preds[idx[k]:idx[k + 1]])
            a = np.stack([s[-horizon:] for s in val[idx[k]:idx[k + 1]]])
            pw, aw = p.sum(axis=0), a.sum(axis=0)
            rows.append(dict(arm=label, estate=est,
                             block_wape=float(np.abs(p - a).sum() / a.sum() * 100),
                             estate_week_smape=float(np.mean(
                                 200 * np.abs(pw - aw) / (np.abs(pw) + np.abs(aw)))),
                             window_total_err_pct=float((pw.sum() - aw.sum()) / aw.sum() * 100)))
    return pd.DataFrame(rows)


# -- scoring -------------------------------------------------------------------------
def _paired(a_err, b_err):
    """Paired t on absolute errors; positive gain means `a` is closer."""
    d = np.abs(b_err) - np.abs(a_err)
    n = len(d)
    sd = d.std(ddof=1) if n > 1 else 0.0
    t = float(d.mean() / (sd / np.sqrt(n))) if n > 1 and sd > 0 else 0.0
    return {"n": n, "wins": int((d > 0).sum()), "gain": float(d.mean()), "t": t,
            "significant": abs(t) > T_CRIT}


def step1_board(res, folds, arms, months=None):
    """Step-1 scoreboard sharing the incumbent's SeasonalNaive MASE denominator."""
    one = res[res["step"] == 1].copy()
    one["model"] = one["arm"] + "|" + one["track"]
    frames = [one[["month", "model", "actual", "pred"]]]
    pm = folds["incumbent_monthly"]
    if pm is not None:
        sn = set(pm.loc[pm["model"] == "SeasonalNaive", "month"])
        for m in arms:
            got = set(one.loc[one["model"] == m, "month"])
            if got != sn:
                raise AssertionError(f"{m}: fold misalignment vs the incumbent scoreboard, "
                                     f"missing={sorted(sn - got)} extra={sorted(got - sn)}")
        frames.append(pm[pm["model"].isin([INCUMBENT, "SeasonalNaive", "SARIMAX", "Trailing3"])]
                      [["month", "model", "actual", "pred"]])
    else:
        # No incumbent file: a seasonal-naive stand-in isn't possible on <1
        # year of data, so MASE uses a last-value naive over the same folds.
        base = one[one["model"] == arms[0]].copy()
        prev = folds["actual"].reindex(
            [pd.Period(m, freq="M") - 1 for m in base["month"]]).to_numpy()
        base["model"], base["pred"] = "SeasonalNaive", prev
        frames.append(base[["month", "model", "actual", "pred"]].dropna())
    c = pd.concat(frames, ignore_index=True)
    if months is not None:
        c = c[c["month"].isin(set(months))]
    c["error"] = c["pred"] - c["actual"]
    board, _ = mm.scoreboard(c)
    return board, c


def per_step(res, folds):
    """MAE and sMAPE by step for every arm, plus the incumbent on the same rows."""
    r = res.copy()
    r["model"] = r["arm"] + "|" + r["track"]
    inc = folds["incumbent_multih"]
    frames = [r[["origin", "step", "month", "model", "actual", "pred"]]]
    if inc is not None:
        keys = r[["origin", "step", "month", "actual"]].drop_duplicates()
        i = keys.merge(inc, on=["origin", "step", "month"], how="inner")
        i = i.rename(columns={"pred_incumbent": "pred"}).assign(model=f"{INCUMBENT}(multih)")
        frames.append(i[["origin", "step", "month", "model", "actual", "pred"]])
    c = pd.concat(frames, ignore_index=True)
    c["ape"] = 200 * (c["pred"] - c["actual"]).abs() / (c["pred"].abs() + c["actual"].abs())
    c["ae"] = (c["pred"] - c["actual"]).abs()
    return (c.groupby(["model", "step"]).agg(MAE=("ae", "mean"), sMAPE=("ape", "mean"),
                                             n=("ae", "size"))
             .reset_index())


# -- orchestration ---------------------------------------------------------------------
def train(args, run_dir):
    train_in, val_in, meta = build_corpus(args.train_estates, args.horizon, args.min_past)
    cfg = {"checkpoint": CHECKPOINT, "horizon": args.horizon, "min_past": args.min_past,
           "context_length": args.context_length, "lr": args.lr, "lora_r": args.lora_r,
           "lora_targets": LORA_TARGETS, "batch_size": args.batch_size,
           "max_steps": args.max_steps, "eval_steps": args.eval_steps,
           "patience": args.patience, "seeds": args.seeds}
    print(f"corpus: {len(train_in)} block series from {args.train_estates}")
    for est, m in meta.items():
        print(f"  {est}: {m['n_blocks']} blocks ({m['n_blocks_too_short']} too short), "
              f"{m['first_day']} -> {m['last_day']}, validation from {m['val_from']}")
    _write_json(os.path.join(run_dir, "corpus.json"), {"meta": meta, "cfg": cfg})

    adapters_dir = os.path.join(run_dir, "adapters")
    if args.local:
        result = _fit_lora_seeds(train_in, val_in, cfg, adapters_dir)
    else:
        run_name = os.path.basename(run_dir)
        s3_root = args.s3_bucket.rstrip("/")
        dest = f"{s3_root}/artifacts/{run_name}/adapters.tar.gz"
        fn = v1.remote_job(_fit_lora_seeds, args.role_arn, s3_root,
                           instance_type=args.instance_type, region=args.region,
                           max_runtime_in_seconds=args.max_runtime_s,
                           job_name_prefix="chronos2-blocks-lora")
        print(f"Launching SageMaker job on {args.instance_type} (blocks until done)...")
        t0 = time.perf_counter()
        result = fn(train_in, val_in, cfg, dest)
        print(f"job finished in {time.perf_counter() - t0:.0f}s, adapters at {dest}")
        _download_adapters(dest, adapters_dir, args.region)
    _write_json(os.path.join(run_dir, "train_log.json"), result)
    return result


def evaluate(args, run_dir, zero_shot_only=False):
    corpus_path = os.path.join(run_dir, "corpus.json")
    corpus = _read_json(corpus_path) if os.path.exists(corpus_path) else None
    train_estates = list(corpus["meta"]) if corpus else args.train_estates
    train_start = min(pd.Timestamp(bw.load_daily_blocks(e).index.min()) for e in train_estates) \
        if not corpus else min(pd.Timestamp(m["first_day"]) for m in corpus["meta"].values())

    pipes = {"zero_shot": load_pipeline()}
    seeds = []
    if not zero_shot_only:
        adapters_dir = os.path.join(run_dir, "adapters")
        seeds = sorted(int(d.split("_")[1]) for d in os.listdir(adapters_dir)
                       if d.startswith("seed_"))
        for s in seeds:
            pipes[f"ft_s{s}"] = load_pipeline(os.path.join(adapters_dir, f"seed_{s}"))

    print(f"\nwalk-forward on {args.test_estate} ({', '.join(pipes)})")
    daily = bw.load_daily_blocks(args.test_estate)
    folds = test_folds(args.test_estate, daily)
    res = walk_forward(pipes, daily, folds, args.horizon)
    if seeds:  # seed ensemble: the mean of the seeds' predictions
        ens = (res[res["arm"].str.startswith("ft_s")]
               .groupby(["track", "origin", "step", "month", "actual"], as_index=False)["pred"]
               .mean().assign(arm="ft_mean"))
        res = pd.concat([res, ens], ignore_index=True)
    res["clean"] = res["month"].map(
        lambda m: pd.Period(m, freq="M").end_time.normalize() < train_start)
    res.to_csv(os.path.join(run_dir, f"eval_{args.test_estate}.csv"), index=False)

    arms = sorted(res["arm"].unique(), key=lambda a: (a != "zero_shot", a))
    models = [f"{a}|{t}" for a in arms for t in ("blocks", "estate")]
    one = res[res["step"] == 1]
    clean_m = sorted(one.loc[one["clean"], "month"].unique())
    over_m = sorted(one.loc[~one["clean"], "month"].unique())
    boards = {"all": step1_board(res, folds, models)[0]}
    if len(clean_m) >= 4:
        boards["clean"] = step1_board(res, folds, models, clean_m)[0]
    if len(over_m) >= 4:
        boards["overlap"] = step1_board(res, folds, models, over_m)[0]
    steps = per_step(res, folds)

    ref = None
    if args.test_estate == "k3" and not args.skip_monthly_ref:
        print("  reference: leak-free zero-shot on the monthly track with weather")
        r, _ = ce.run_monthly("k3", use_cov=True, cross_learning=False)
        ref = step1_board(res, folds, models)[1]
        extra = r[r["step"] == 1][["month", "actual", "pred"]].assign(model="Chronos2_monthly_cov")
        c = pd.concat([ref, extra], ignore_index=True)
        c["error"] = c["pred"] - c["actual"]
        boards["all"], _ = mm.scoreboard(c)

    indom = in_domain_check(pipes, train_estates, args.horizon) if seeds else None
    text = _report(args, run_dir, res, folds, boards, steps, indom, corpus, seeds,
                   train_start, clean_m, over_m)
    path = os.path.join(run_dir, f"report_{args.test_estate}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print("\n" + text + f"\n\nwrote {path}")
    _log_mlflow(args, run_dir, boards, seeds, corpus)


def _pair_line(res, a, b, track, months=None):
    one = res[(res["step"] == 1) & (res["track"] == track)]
    if months is not None:
        one = one[one["month"].isin(set(months))]
    x = one[one["arm"] == a].set_index("month").sort_index()
    y = one[one["arm"] == b].set_index("month").sort_index()
    common = x.index.intersection(y.index)
    p = _paired((x.loc[common, "pred"] - x.loc[common, "actual"]).to_numpy(),
                (y.loc[common, "pred"] - y.loc[common, "actual"]).to_numpy())
    return (f"{a} vs {b} [{track}]: wins {p['wins']}/{p['n']}, mean |err| gain "
            f"{p['gain']:+,.0f}, t={p['t']:+.2f} "
            f"{'SIGNIFICANT' if p['significant'] else 'not significant'}"), p


def _inc_pair_line(res, folds, arm, track):
    pm = folds["incumbent_monthly"]
    inc = pm[pm["model"] == INCUMBENT].set_index("month")
    one = res[(res["step"] == 1) & (res["track"] == track) & (res["arm"] == arm)].set_index("month")
    common = sorted(set(inc.index) & set(one.index))
    p = _paired((one.loc[common, "pred"] - one.loc[common, "actual"]).to_numpy(),
                (inc.loc[common, "pred"] - inc.loc[common, "actual"]).to_numpy())
    return (f"{arm} vs {INCUMBENT} [{track}]: wins {p['wins']}/{p['n']}, t={p['t']:+.2f} "
            f"{'SIGNIFICANT' if p['significant'] else 'not significant'}")


def _report(args, run_dir, res, folds, boards, steps, indom, corpus, seeds,
            train_start, clean_m, over_m):
    out = ["=" * 78,
           f"Chronos-2 fine-tune v2 (block-weekly LoRA) -- test estate {args.test_estate}",
           "=" * 78]
    if corpus:
        c = corpus["cfg"]
        out.append(f"trained on {', '.join(corpus['meta'])}: "
                   + "; ".join(f"{e} {m['n_blocks']} blocks {m['first_day']}..{m['last_day']}"
                               for e, m in corpus["meta"].items()))
        out.append(f"LoRA r={c['lora_r']} lr={c['lr']} batch={c['batch_size']} "
                   f"max_steps={c['max_steps']} eval every {c['eval_steps']} "
                   f"patience {c['patience']} horizon {c['horizon']}w seeds {c['seeds']}")
        log_path = os.path.join(run_dir, "train_log.json")
        if os.path.exists(log_path):
            for s, info in _read_json(log_path)["seeds"].items():
                out.append(f"  seed {s}: best validation loss {info['best_eval_loss']} at step "
                           f"{info['best_step']} (stopped at {info['last_step']})")
    else:
        out.append("zero-shot only (no adapters)")
    out.append(f"cross-learning OFF for every forecast; {len(folds['origins'])} origins; "
               f"step-1 folds: {len(clean_m)} clean (target month ends before "
               f"{train_start.date()}), {len(over_m)} calendar-overlapping")
    if folds["imputed_months"]:
        out.append(f"completeness-scaled actuals (block series are raw): "
                   f"{', '.join(folds['imputed_months'])}")
    out.append("")

    if seeds:
        out.append("-- DECISION: fine-tuned vs zero-shot, same folds, step 1 --")
        for track in ("blocks", "estate"):
            for subset, months in (("all", None), ("clean", clean_m), ("overlap", over_m)):
                if months is not None and len(months) < 4:
                    continue
                line, _ = _pair_line(res, "ft_mean", "zero_shot", track, months)
                out.append(f"   {subset:8s} {line}")
        per_seed = []
        for s in seeds:
            _, p = _pair_line(res, f"ft_s{s}", "zero_shot", "blocks")
            per_seed.append(p["gain"])
        agree = all(g > 0 for g in per_seed) or all(g < 0 for g in per_seed)
        out.append(f"   per-seed mean gain [blocks]: "
                   + ", ".join(f"s{s} {g:+,.0f}" for s, g in zip(seeds, per_seed))
                   + f"  -> seeds {'agree' if agree else 'DISAGREE'} on direction")
        out.append("   Rule: ship only if ft_mean beats zero_shot here AND the seeds agree.")
        out.append("")

    for name, board in boards.items():
        out.append(f"-- step-1 scoreboard, {name} folds (sorted by MASE) --")
        out.append(ce._fmt(board))
        out.append("")

    if folds["incumbent_monthly"] is not None:
        out.append(f"-- paired vs {INCUMBENT}, step 1, all folds --")
        for arm in sorted(res["arm"].unique()):
            out.append("   " + _inc_pair_line(res, folds, arm, "blocks"))
        out.append("")

    out.append("-- by step (MAE / sMAPE) --")
    piv = steps.pivot(index="model", columns="step", values=["MAE", "sMAPE"])
    for model, row in piv.iterrows():
        out.append(f"   {model:34s} " + "  ".join(
            f"h{s}: {row[('MAE', s)]:>9,.0f} / {row[('sMAPE', s)]:5.1f}%"
            for s in SCORED_STEPS if ("MAE", s) in row.index and pd.notna(row[("MAE", s)])))
    out.append("")

    if indom is not None:
        out.append("-- in-domain: training estates' validation tails (checkpoint chosen "
                   "here, so fine-tuned arms are optimistic) --")
        for _, r in indom.iterrows():
            out.append(f"   {r['estate']} {r['arm']:10s} block WAPE {r['block_wape']:5.1f}%  "
                       f"estate-week sMAPE {r['estate_week_smape']:5.1f}%  "
                       f"window total {r['window_total_err_pct']:+5.1f}%")
        out.append("")

    out.append("-- how to read this --")
    out.append("  * 'blocks' sums per-block forecasts (the grain the adapter trained on);\n"
               "    'estate' forecasts the estate's weekly total directly.")
    out.append("  * ~30 folds: a paired t below ~2 means the arms are indistinguishable,\n"
               "    whatever the aggregate MASE says.")
    out.append("  * EC/EB have a deep Sep-Dec trough that k3 does not; an adapter can learn\n"
               "    that shape and carry it to k3 where it does not belong. The clean vs\n"
               "    overlap split and the in-domain table separate 'learned nothing' from\n"
               "    'learned something that does not transfer'.")
    out.append("=" * 78)
    return "\n".join(out)


def _log_mlflow(args, run_dir, boards, seeds, corpus):
    try:
        import mlflow
        mlflow.set_tracking_uri(f"sqlite:///{os.path.join(_REPO, 'mlflow.db')}")
        mlflow.set_experiment(MLFLOW_EXPERIMENT)
        b = boards["all"].set_index("model")
        with mlflow.start_run(run_name=f"blocks_v2_{os.path.basename(run_dir)}_{args.test_estate}"):
            mlflow.log_params({"test_estate": args.test_estate, "seeds": str(seeds),
                               "train_estates": str(list(corpus["meta"]) if corpus else []),
                               "cross_learning": False, **(corpus["cfg"] if corpus else {})})
            for model in b.index:
                key = model.replace("|", "_").replace("(", "_").replace(")", "")
                mlflow.log_metrics({f"{key}_mase": float(b.loc[model, "MASE"]),
                                    f"{key}_smape": float(b.loc[model, "sMAPE"])})
            for f in os.listdir(run_dir):
                if f.endswith((".txt", ".json")):
                    mlflow.log_artifact(os.path.join(run_dir, f))
        print(f"MLflow: logged to '{MLFLOW_EXPERIMENT}'")
    except Exception as exc:
        print(f"MLflow logging skipped: {type(exc).__name__}: {exc}")


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train-estates", nargs="+", default=["EC", "EB"])
    ap.add_argument("--test-estate", default="k3")
    ap.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    ap.add_argument("--horizon", type=int, default=HORIZON_WEEKS)
    ap.add_argument("--min-past", type=int, default=MIN_PAST)
    ap.add_argument("--context-length", type=int, default=256,
                    help="weeks; longer than any series here, so nothing is truncated")
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--lora-r", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--max-steps", type=int, default=1000)
    ap.add_argument("--eval-steps", type=int, default=50)
    ap.add_argument("--patience", type=int, default=4,
                    help="evaluations without validation improvement before stopping")
    ap.add_argument("--local", action="store_true",
                    help="run the job body on this machine (CPU) instead of SageMaker")
    ap.add_argument("--zero-shot-only", action="store_true",
                    help="no training: evaluate the leak-free zero-shot block track only")
    ap.add_argument("--run-dir", default=None,
                    help="evaluate an existing run directory instead of training")
    ap.add_argument("--recover-job", default=None, metavar="JOB_NAME",
                    help="with --run-dir: fetch a Completed job's adapters and log from "
                         "S3 (for when the local process died mid-job), then evaluate")
    ap.add_argument("--skip-monthly-ref", action="store_true",
                    help="skip the leak-free Chronos2_monthly_cov reference arm on k3")
    ap.add_argument("--role-arn", default=v1.DEFAULT_ROLE_ARN)
    ap.add_argument("--s3-bucket", default=f"{v1.DEFAULT_S3_ROOT}/chronos2-lora-blocks")
    ap.add_argument("--region", default=v1.DEFAULT_REGION)
    ap.add_argument("--instance-type", default="ml.g5.xlarge")
    ap.add_argument("--max-runtime-s", type=int, default=3600)
    args = ap.parse_args()

    if args.recover_job and not args.run_dir:
        ap.error("--recover-job needs --run-dir (the run the job was launched from)")
    if args.run_dir:
        if args.recover_job:
            recover_job(args, args.run_dir, args.recover_job)
        evaluate(args, args.run_dir)
        return 0

    tag = "zeroshot" if args.zero_shot_only else ("local" if args.local else "sm")
    name = f"{'-'.join(args.train_estates)}_{tag}_{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir = os.path.join(OUT_ROOT, name)
    os.makedirs(run_dir, exist_ok=True)
    print(f"run dir: {run_dir}")
    if not args.zero_shot_only:
        train(args, run_dir)
    evaluate(args, run_dir, zero_shot_only=args.zero_shot_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
