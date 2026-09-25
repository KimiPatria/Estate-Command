"""
Chronos-2 LoRA fine-tuning on SageMaker (Phase 2 of the Chronos-2 evaluation;
Phase 1 is the zero-shot benchmark in chronos_experiment.py).

Uses the SageMaker Python SDK's `@remote` decorator: a plain local Python
function is decorated and, when called, runs synchronously as a real
SageMaker training job (packaging + uploading its arguments, provisioning the
instance, running, and returning the result) while looking like an ordinary
function call from VS Code. No Estimator boilerplate, no custom container --
see _fit_chronos2_lora() below for the whole job body.

Why the data is what it is
---------------------------
This repo has exactly two estates with monthly features: k3 (36 usable
months) and EC (4 usable months -- see forecast/EC/features_estate_monthly.csv).
EC alone is nowhere near enough to fine-tune a foundation model; it is pooled
in as a second item because a second independent series is strictly more
informative than none, not because it is sufficient by itself. The real
fine-tuning signal here comes from k3's own history.

Leakage discipline (read this before changing cutoff_frac)
------------------------------------------------------------
Fine-tuning on any k3 month whose value is later used as a walk-forward
"actual" would be training on the test set. So k3 is split at one cutoff
month: everything <= cutoff goes into the fine-tuning corpus (as gradient-
update targets), and evaluate_finetuned() below scores ONLY walk-forward
origins strictly after the cutoff (via chronos_experiment.run_monthly's
min_origin=). This is the same principle as chronos_experiment.py's
"matched-context" subset re-scoring: the incumbent AND the zero-shot Chronos2
arms are re-scored on the IDENTICAL restricted month set, never compared
against a wider baseline. The unavoidable cost is fewer evaluated folds
(typically ~14 of the original 30 at the default cutoff_frac=0.6) -- there is
no way around this with only one real target series.

Covariates: the fine-tuning corpus uses the exact same weather channels as
chronos_experiment.py's Chronos2_monthly_cov arm (month_sin, month_cos,
temp_lag1m, humid_lag1m as known/past-future; the remaining raw+lagged
weather aggregates as past-only) -- verified in that benchmark run to be the
channels timesfm_experiment._split_covariates selects for k3. Hardcoded here
rather than re-derived, since training data has no "is this known into the
future" question to answer (everything is already-realized history); the
question only matters at inference time, which chronos_experiment.run_monthly
already handles via the same _split_covariates call.

What you need before running this for real
----------------------------------------------
  1. An AWS account with SageMaker access and an IAM execution role that
     trusts sagemaker.amazonaws.com and can run training jobs, pull the
     amazon/chronos-2 checkpoint from the internet (or a VPC NAT/endpoint),
     and read/write the S3 bucket you pass. AmazonSageMakerFullAccess is the
     quick path; a scoped-down policy is the correct one for anything beyond
     a personal experiment.
  2. `conda run -n chronos-rd python -m pip install -r requirements-chronos.txt`
     (adds the `sagemaker` SDK to the local orchestration env; already done
     if you set up Phase 1).
  3. `aws configure` / valid default credentials in the environment this
     script runs in (the @remote decorator uses your local AWS credential
     chain to submit the job, same as the AWS CLI).

Usage:
    conda run -n chronos-rd python forecast/chronos_finetune_sagemaker.py \\
        --role-arn arn:aws:iam::<account>:role/<sagemaker-execution-role> \\
        --s3-bucket s3://<your-bucket>/chronos2-lora-k3 \\
        --instance-type ml.g5.xlarge
"""

import argparse
import importlib.util
import os
import shutil
import sys
import tarfile
import tempfile
import time

import numpy as np
import pandas as pd

# The SageMaker SDK streams the remote job's CloudWatch logs straight to
# stdout while @remote blocks. On Windows, stdout defaults to the legacy
# 'cp1252' codepage, which crashes (UnicodeEncodeError) on the first non-cp1252
# character a dependency's installer/progress bar happens to print -- killing
# the LOCAL script while the actual training job keeps running on AWS
# regardless (confirmed: a job survived exactly this crash). Force UTF-8 so
# log streaming can't take the whole script down over a character it can't print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_DIR)

MLFLOW_EXPERIMENT = "ffb-chronos2-rd"

# This Wilmar account's (430287291296) existing SageMaker Quick Setup domain --
# role, bucket and region were all auto-created together on 2026-09-22 and are
# mutually consistent (the role's AmazonSageMakerFullAccess policy is scoped to
# buckets with "sagemaker" in the name, which this bucket satisfies). Override
# them on the command line if you set up your own role/bucket/region instead.
DEFAULT_ROLE_ARN = ("arn:aws:iam::430287291296:role/service-role/"
                    "AmazonSageMaker-ExecutionRole-20260922T141629")
DEFAULT_S3_ROOT = "s3://sagemaker-ap-southeast-3-430287291296"
DEFAULT_REGION = "ap-southeast-3"

HORIZON = 12
MIN_TRAIN = 6

# Mirrors the split timesfm_experiment._split_covariates produced for k3 in
# the Chronos2_monthly_cov benchmark run -- see module docstring.
COV_PAST_FUTURE = ["month_sin", "month_cos", "temp_lag1m", "humid_lag1m"]
COV_PAST_ONLY = ["rain_mo", "et0_mo", "wb_mo", "solar_mo", "temp_mo", "humid_mo",
                "rain_lag1m", "wb_lag1m", "rain_lag2m", "wb_lag2m", "rain_lag3m",
                "wb_lag3m", "rain_flower_5_7m", "wb_flower_5_7m"]


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mm = _load_by_path("epms_monthly_model", os.path.join(_DIR, "monthly_model.py"))
cm = _load_by_path("epms_chronos_model", os.path.join(_DIR, "chronos_model.py"))
ce = _load_by_path("epms_chronos_experiment", os.path.join(_DIR, "chronos_experiment.py"))
ESTATE_DIRS = ce.ESTATE_DIRS


# -- data prep (local, no AWS) -------------------------------------------------
def _estate_monthly(estate):
    est_dir = ESTATE_DIRS[estate]
    df = mm.load_estate(os.path.join(est_dir, "features_estate_monthly.csv"))
    wm = mm.build_weather_monthly(os.path.join(est_dir, "weather_nasa_power_history.csv"))
    df = mm.apply_weather(df, wm)
    return df.loc[df["exclude_from_model"] == 0].sort_values("month").reset_index(drop=True)


def prepare_finetune_data(cutoff_frac=0.6, use_cov=True):
    """Pool k3's early history + all of EC's history into one training frame.

    Returns (train_df, known_names, cutoff_month):
      train_df     plain pandas DataFrame (item_id, timestamp, target,
                   [known_0.., past_0..]) -- small enough to travel to the
                   SageMaker job as a normal cloudpickled function argument,
                   no S3 data upload needed.
      known_names  column names to declare as known_covariates_names (empty
                   list if use_cov=False).
      cutoff_month the last k3 Period included in the fine-tuning corpus --
                   pass this straight to evaluate_finetuned()'s min_origin.
    """
    k3 = _estate_monthly("k3")
    ec = _estate_monthly("ec")

    k3_months = k3["month"].tolist()
    cutoff_idx = int(len(k3_months) * cutoff_frac)
    cutoff_idx = max(MIN_TRAIN, min(cutoff_idx, len(k3_months) - 2))
    cutoff_month = k3_months[cutoff_idx - 1]
    k3_ft = k3[k3["month"] <= cutoff_month]

    cols = ([mm.TARGET] + COV_PAST_FUTURE + COV_PAST_ONLY) if use_cov else [mm.TARGET]
    frames = []
    for label, sub in (("k3", k3_ft), ("ec", ec)):
        missing = sub[cols].isna()
        if missing.any().any():
            raise ValueError(
                f"{label}: NaN in {list(missing.columns[missing.any()])} -- "
                "fine-tuning needs complete covariate rows, unlike inference "
                "where past-only channels may have holes")
        d = pd.DataFrame({
            "item_id": label,
            "timestamp": sub["month"].apply(lambda m: m.to_timestamp()),
            "target": sub[mm.TARGET].astype(float).to_numpy(),
        })
        if use_cov:
            for k, col in enumerate(COV_PAST_FUTURE):
                d[f"known_{k}"] = sub[col].astype(float).to_numpy()
            for k, col in enumerate(COV_PAST_ONLY):
                d[f"past_{k}"] = sub[col].astype(float).to_numpy()
        frames.append(d)

    train_df = pd.concat(frames, ignore_index=True)
    known_names = [f"known_{k}" for k in range(len(COV_PAST_FUTURE))] if use_cov else []
    print(f"fine-tune corpus: k3 {len(k3_ft)} months (<= {cutoff_month}), "
          f"ec {len(ec)} months, {len(known_names)} known covariates")
    return train_df, known_names, cutoff_month


# -- the SageMaker job body -----------------------------------------------------
def _fit_chronos2_lora(train_df, known_names, prediction_length, s3_output_uri,
                       fine_tune_steps, fine_tune_lr, fine_tune_batch_size,
                       fine_tune_context_length):
    """Runs ON the SageMaker instance. Fits LoRA-fine-tuned Chronos-2, tars the
    saved predictor, uploads it to S3, and returns the URI.

    Kept self-contained (only autogluon.timeseries + boto3, both satisfied by
    requirements-chronos-remote.txt + the container's own SDK bootstrap) --
    it does not import mm.py/chronos_model.py, so it carries none of this
    repo's other local-module dependencies into the job.
    """
    import boto3
    from autogluon.timeseries import TimeSeriesDataFrame, TimeSeriesPredictor

    train_tsdf = TimeSeriesDataFrame.from_data_frame(
        train_df, id_column="item_id", timestamp_column="timestamp")

    work = tempfile.mkdtemp(prefix="chronos2_lora_")
    predictor_path = os.path.join(work, "predictor")
    predictor = TimeSeriesPredictor(
        prediction_length=prediction_length,
        target="target",
        known_covariates_names=known_names or None,
        quantile_levels=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
        freq="MS",
        path=predictor_path,
        verbosity=2,
    )
    predictor.fit(
        train_tsdf,
        hyperparameters={"Chronos2": {
            "model_path": "amazon/chronos-2",
            "fine_tune": True,
            "fine_tune_mode": "lora",
            "fine_tune_steps": fine_tune_steps,
            "fine_tune_lr": fine_tune_lr,
            "fine_tune_batch_size": fine_tune_batch_size,
            "fine_tune_context_length": fine_tune_context_length,
        }},
        skip_model_selection=True,
        enable_ensemble=False,
    )

    # fit() does NOT raise when Chronos2 fails internally -- AutoGluon logs
    # "Exception caused Chronos2 to fail during training... Skipping this
    # model" and returns normally having trained nothing (confirmed by a real
    # job: it silently produced an 8KB predictor directory with zero model
    # weights, which we then would have tarred and uploaded as if it were
    # usable). Fail loud here instead of shipping an empty predictor.
    trained = predictor.model_names()
    if not trained:
        raise RuntimeError(
            "AutoGluon trained zero models -- Chronos2 failed internally "
            "during fit(). Check the predictor log for the real traceback "
            f"(likely under {predictor_path}/logs/predictor_log.txt before "
            "this directory is discarded).")

    tar_path = os.path.join(work, "predictor.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(predictor_path, arcname=".")

    assert s3_output_uri.startswith("s3://")
    bucket, key = s3_output_uri[len("s3://"):].split("/", 1)
    boto3.client("s3").upload_file(tar_path, bucket, key)
    return s3_output_uri


def remote_job(fn, role_arn, s3_bucket_uri, instance_type="ml.g5.xlarge",
               region=None, max_runtime_in_seconds=3600,
               job_name_prefix="chronos2-lora-k3"):
    """Wrap `fn` so calling it runs as a SageMaker training job and blocks.

    Shared by this script and chronos2_blocks_finetune.py so both run on the
    one job configuration proven to work. `fn` must be self-contained (imports
    inside its body) and defined in the script being run as __main__, so
    cloudpickle ships it by value rather than by a module reference the job
    container could not import.

    region matters here because the role/bucket this script defaults to live
    in a SPECIFIC region (whatever SageMaker Quick Setup created them in) --
    if your default AWS CLI profile points elsewhere, the job would otherwise
    launch in the wrong region and fail to find either.

    image_uri is set explicitly, NOT left to @remote's own auto-detection --
    that only supports LOCAL Python 3.8/3.10 and raises ValueError otherwise.
    The job also enforces an EXACT python-version match against the local
    client at runtime (confirmed by trial). This must therefore be run from a
    LOCAL env that is Python 3.13, matching PyTorch 2.10's DLC build exactly
    (see requirements-chronos.txt) -- deliberately NOT the 2.9/py312 DLC:
    autogluon.timeseries pins torch>=2.10,<2.14, so on a 2.9 base image pip
    has to upgrade torch, which drags the whole torch ecosystem out of sync
    with whatever torchvision/torchaudio shipped in that image. Confirmed by
    two real failed jobs: transformers imports vision- and audio-loss
    utilities unconditionally at package-import time (nothing to do with
    Chronos-2's own text-only usage), and each one's compiled extension
    needs to match torch's ABI -- fixing torchvision only surfaced the same
    class of failure again via torchaudio next. 2.10's DLC ships torch,
    torchvision and torchaudio already mutually matched, so nothing needs
    upgrading and this whole failure class doesn't arise.
    """
    import boto3
    # Neither of these live at their pre-v3 top-level paths (we have SDK
    # 3.22.1): sagemaker.remote_function -> sagemaker.core.remote_function,
    # sagemaker.Session -> sagemaker.core.helper.session_helper.Session. See
    # https://github.com/aws/sagemaker-python-sdk/blob/master/migration.md
    from sagemaker.core.remote_function import remote
    from sagemaker.core.helper.session_helper import Session as SageMakerSession
    from sagemaker.core import image_uris

    sm_session = SageMakerSession(boto_session=boto3.Session(region_name=region)) \
        if region else None
    image_uri = image_uris.retrieve(
        framework="pytorch", region=region, version="2.10", py_version="py313",
        image_scope="training", instance_type=instance_type)

    return remote(
        role=role_arn,
        instance_type=instance_type,
        image_uri=image_uri,
        sagemaker_session=sm_session,
        dependencies=os.path.join(_REPO, "requirements-chronos-remote.txt"),
        s3_root_uri=f"{s3_bucket_uri.rstrip('/')}/jobs",
        volume_size=30,
        max_runtime_in_seconds=max_runtime_in_seconds,
        job_name_prefix=job_name_prefix,
    )(fn)


def launch_finetune_job(train_df, known_names, role_arn, s3_bucket_uri,
                        instance_type="ml.g5.xlarge", region=None,
                        fine_tune_steps=200, fine_tune_lr=1e-5,
                        fine_tune_batch_size=32, fine_tune_context_length=2048,
                        max_runtime_in_seconds=3600):
    """Decorates and calls _fit_chronos2_lora as a SageMaker job. Blocks until done.

    fine_tune_steps defaults to 200, not AutoGluon's own 1000 -- the pooled
    corpus here is ~2 items covering a few dozen unique months, so 1000 LoRA
    steps of resampling the same handful of windows is much more likely to
    overfit than to learn anything transferable. Worth sweeping once you have
    a live account; this default is a starting point, not a tuned value --
    nothing here has been validated against real SageMaker compute.
    """
    s3_bucket_uri = s3_bucket_uri.rstrip("/")
    s3_output_uri = f"{s3_bucket_uri}/artifacts/predictor.tar.gz"
    remote_fn = remote_job(_fit_chronos2_lora, role_arn, s3_bucket_uri,
                           instance_type=instance_type, region=region,
                           max_runtime_in_seconds=max_runtime_in_seconds)

    print(f"Launching SageMaker training job on {instance_type} "
          f"(this call blocks until the job finishes)...")
    t0 = time.perf_counter()
    uri = remote_fn(train_df, known_names, HORIZON, s3_output_uri,
                    fine_tune_steps, fine_tune_lr, fine_tune_batch_size,
                    fine_tune_context_length)
    print(f"job finished in {time.perf_counter() - t0:.0f}s, artifact at {uri}")
    return uri


def download_predictor(s3_uri, local_dir, region=None):
    """Downloads and extracts the tarred predictor produced by the job above."""
    import boto3

    os.makedirs(local_dir, exist_ok=True)
    tar_path = os.path.join(local_dir, "predictor.tar.gz")
    bucket, key = s3_uri[len("s3://"):].split("/", 1)
    boto3.client("s3", region_name=region).download_file(bucket, key, tar_path)
    extract_dir = os.path.join(local_dir, "predictor")
    with tarfile.open(tar_path) as tar:
        tar.extractall(extract_dir)
    return extract_dir


# -- evaluation (local, no AWS) -------------------------------------------------
def evaluate_finetuned(predictor_dir, cutoff_month, use_cov=True, estate="k3"):
    """Walk-forward-evaluates the fine-tuned predictor on k3 origins strictly
    after cutoff_month, alongside a zero-shot Chronos2 run restricted to the
    SAME origins and the incumbent re-scored on the SAME months -- exactly the
    "matched subset" discipline chronos_experiment.py already uses elsewhere,
    just keyed on the fine-tuning cutoff instead of a context-length floor.
    """
    predictor = cm.load_predictor(predictor_dir)

    ft_resid, ft_meta = ce.run_monthly(estate, HORIZON, MIN_TRAIN, use_cov=use_cov,
                                       predictor=predictor, min_origin=cutoff_month)
    zs_resid, zs_meta = ce.run_monthly(estate, HORIZON, MIN_TRAIN, use_cov=use_cov,
                                       min_origin=cutoff_month)

    eval_months = sorted(set(ft_resid.loc[ft_resid["step"] == 1, "month"]))
    if eval_months != sorted(set(zs_resid.loc[zs_resid["step"] == 1, "month"])):
        raise AssertionError("fine-tuned and zero-shot restricted runs scored "
                             "different months -- fold misalignment")
    total_model_months = len(_estate_monthly(estate))
    total_origins = max(0, total_model_months - 1 - (MIN_TRAIN - 1))
    print(f"restricted eval: {len(set(ft_resid['origin']))} origins after cutoff "
          f"{cutoff_month} (of {total_origins} total in the full Phase-1 benchmark)")

    ft_label = f"Chronos2_finetuned_monthly{'_cov' if use_cov else ''}"
    zs_label = f"Chronos2_monthly{'_cov' if use_cov else ''}"

    est_dir = ESTATE_DIRS[estate]
    existing = pd.read_csv(os.path.join(est_dir, "predictions_monthly.csv"))
    existing = existing[existing["month"].isin(eval_months)]
    frames = [existing]
    for label, resid in ((ft_label, ft_resid), (zs_label, zs_resid)):
        one = resid.loc[resid["step"] == 1, ["month", "actual", "pred"]].copy()
        one["model"] = label
        frames.append(one)
    combined = pd.concat(frames, ignore_index=True)
    combined["error"] = combined["pred"] - combined["actual"]
    board, naive_mae = mm.scoreboard(combined)

    out_dir = os.path.join(_DIR, "experiments", "chronos2")
    os.makedirs(out_dir, exist_ok=True)
    ft_resid.to_csv(os.path.join(out_dir, f"{estate}_{ft_label}_multih.csv"), index=False)
    board.to_csv(os.path.join(out_dir, f"{estate}_finetuned_scoreboard_restricted.csv"),
                index=False)

    print("\n" + "=" * 78)
    print(f"Chronos-2 LoRA fine-tuning -- estate {estate}, restricted to "
          f"{len(eval_months)} folds after {cutoff_month}")
    print("=" * 78)
    print(ce._fmt(board))
    inc = board.set_index("model")
    ft_row, zs_row = inc.loc[ft_label], inc.loc[zs_label]
    print(f"\nfine-tuned vs zero-shot (same restricted folds): "
          f"sMAPE {ft_row.sMAPE - zs_row.sMAPE:+.2f}pp, MASE {ft_row.MASE - zs_row.MASE:+.3f}")
    print("Small-sample caveat: this is ~14-16 folds, not 30 -- treat any delta "
         "here as suggestive, not conclusive.")

    _log_mlflow_finetune(estate, board, cutoff_month, len(eval_months),
                         ft_label, zs_label)
    return board


def _log_mlflow_finetune(estate, board, cutoff_month, n_folds, ft_label, zs_label):
    try:
        import mlflow
        mlflow.set_tracking_uri(f"sqlite:///{os.path.join(_REPO, 'mlflow.db')}")
        mlflow.set_experiment(MLFLOW_EXPERIMENT)
        row = board.set_index("model")
        with mlflow.start_run(run_name=f"{estate}_{ft_label}"):
            mlflow.log_params({"estate": estate, "cutoff_month": str(cutoff_month),
                              "n_eval_folds": n_folds})
            for label in (ft_label, zs_label, "Ensemble_workdone"):
                if label in row.index:
                    r = row.loc[label]
                    mlflow.log_metrics({
                        f"{label}_smape": float(r.sMAPE), f"{label}_mase": float(r.MASE)})
        print(f"MLflow: logged fine-tuning run to '{MLFLOW_EXPERIMENT}'")
    except Exception as exc:
        print(f"MLflow logging skipped: {type(exc).__name__}: {exc}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role-arn", default=DEFAULT_ROLE_ARN,
                    help="IAM execution role ARN the SageMaker job assumes")
    ap.add_argument("--s3-bucket", default=f"{DEFAULT_S3_ROOT}/chronos2-lora-k3",
                    help="s3://bucket/prefix for job code + the resulting predictor artifact")
    ap.add_argument("--region", default=DEFAULT_REGION,
                    help="region the role/bucket above live in -- must match, or the "
                         "job launches in your default CLI region and can't find either")
    ap.add_argument("--instance-type", default="ml.g5.xlarge",
                    help="GPU instance for fine-tuning (default: 1x A10G, matches "
                         "Chronos-2's target hardware; this account's quota allows "
                         "exactly 1 concurrent ml.g5.xlarge training job)")
    ap.add_argument("--cutoff-frac", type=float, default=0.6,
                    help="fraction of k3's usable months used for fine-tuning; "
                         "the rest are held out as walk-forward eval origins")
    ap.add_argument("--no-covariates", action="store_true",
                    help="fine-tune target-only, skipping weather covariates")
    ap.add_argument("--fine-tune-steps", type=int, default=200)
    ap.add_argument("--fine-tune-lr", type=float, default=1e-5)
    ap.add_argument("--fine-tune-batch-size", type=int, default=32)
    ap.add_argument("--fine-tune-context-length", type=int, default=2048)
    ap.add_argument("--max-runtime-s", type=int, default=3600)
    ap.add_argument("--predictor-dir", default=None,
                    help="skip training and evaluate an already-downloaded predictor "
                         "directory instead (e.g. a previous run's output)")
    args = ap.parse_args()

    use_cov = not args.no_covariates
    train_df, known_names, cutoff_month = prepare_finetune_data(
        args.cutoff_frac, use_cov=use_cov)

    if args.predictor_dir:
        predictor_dir = args.predictor_dir
    else:
        if not args.role_arn or not args.s3_bucket:
            ap.error("--role-arn and --s3-bucket are required unless "
                     "--predictor-dir is given")
        uri = launch_finetune_job(
            train_df, known_names, args.role_arn, args.s3_bucket,
            instance_type=args.instance_type, region=args.region,
            fine_tune_steps=args.fine_tune_steps, fine_tune_lr=args.fine_tune_lr,
            fine_tune_batch_size=args.fine_tune_batch_size,
            fine_tune_context_length=args.fine_tune_context_length,
            max_runtime_in_seconds=args.max_runtime_s)
        local_dir = os.path.join(_DIR, "experiments", "chronos2", "finetuned_predictor")
        shutil.rmtree(local_dir, ignore_errors=True)
        predictor_dir = download_predictor(uri, local_dir, region=args.region)

    evaluate_finetuned(predictor_dir, cutoff_month, use_cov=use_cov)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
