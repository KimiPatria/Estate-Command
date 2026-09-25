"""
Chronos-2 adapter -- amazon/chronos-2 via AutoGluon-TimeSeries (Stage R&D).

This is a BENCHMARK component, mirroring timesfm_model.py's role and contract.
Nothing in the served pipeline imports it, and nothing here writes to
monthly_model.joblib or changes /forecast/data.

Why AutoGluon-TimeSeries instead of the raw amazon-science/chronos-forecasting
repo
--------------------------------------------------------------------------
The public chronos-forecasting repo's scripts/training/train.py targets the
legacy T5-based Chronos-1 models, not Chronos-2's encoder-only architecture --
there is no public raw fine-tuning path for Chronos-2 yet. AutoGluon-TimeSeries
(>=1.6.3) wraps Chronos-2 for both zero-shot and LoRA fine-tuned use
(hyperparameters={"Chronos2": {"fine_tune": True, ...}}), with native
past/future covariate support that mirrors the incumbent's weather features.
Using it for zero-shot now keeps Phase 1 and the later fine-tuning phase on
one code path.

Chronos-2 is 120M params, Apache-2.0 (fully shippable -- unlike TimesFM 3.0's
non-commercial weights), context up to 8192 steps, prediction up to 1024 steps.

Environment isolation
----------------------
autogluon.timeseries pins torch>=2.10,<2.14. The dashboard's env has torch
2.9.1, verified and documented (requirements-timesfm.txt) as untouched by the
TimesFM install. Installing autogluon here would silently upgrade that torch
and invalidate that guarantee, so this module is meant to run in the separate
`chronos-rd` conda env (see requirements-chronos.txt), not the anaconda base
env that serves the dashboard.

Design constraints this module has to honour (same as timesfm_model.py)
--------------------------------------------------------------------------
  * Graceful unavailability. `import autogluon.timeseries` happens inside
    calls, guarded. With it uninstalled, importing this module and calling
    available() must both still work.
  * Identical quantile contract: q is always (H, 9) holding q0.1..q0.9,
    median at index 4 -- so chronos_experiment.py's scoring/conformal code is
    interchangeable with timesfm_experiment.py's.
  * No padding. Unlike TimesFM's fixed input-patch length, Chronos-2 is an
    arbitrary-length encoder, so contexts are cleaned but never left-padded --
    n_context is simply len(context).

Departure from timesfm_model's forecast_batch signature
---------------------------------------------------------
AutoGluon's data model (TimeSeriesDataFrame) is timestamp-indexed, unlike
TimesFM's plain arrays. forecast_batch here therefore takes `freq` and
`starts` (one real calendar start timestamp per context) so each context maps
onto real dates -- both callers of this module are in this repo, so this is
an intentional, contained extension rather than a public/frozen contract.
"""

import importlib.util
import shutil
import tempfile

import numpy as np
import pandas as pd

CHECKPOINT = "amazon/chronos-2"
LICENSE = "Apache-2.0"
SHIPPABLE = True

QUANTILE_LEVELS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
MEDIAN_INDEX = 4
MAX_CONTEXT = 8192
MAX_HORIZON = 1024


class ChronosUnavailable(RuntimeError):
    """autogluon.timeseries is not installed in this interpreter."""


# -- availability -------------------------------------------------------------
def available():
    """Cheap, never raises. Uses find_spec so it does not import torch."""
    info = {"available": False, "checkpoint": CHECKPOINT, "license": LICENSE,
             "shippable": SHIPPABLE, "reason": "", "versions": {}}
    try:
        if importlib.util.find_spec("autogluon.timeseries") is None:
            info["reason"] = ('autogluon.timeseries not installed -- run this '
                              "module from the `chronos-rd` conda env "
                              "(see requirements-chronos.txt)")
            return info
    except Exception as exc:
        info["reason"] = f"{type(exc).__name__}: {exc}"
        return info

    import importlib.metadata as md
    for pkg in ("autogluon.timeseries", "torch", "chronos-forecasting",
                "transformers"):
        try:
            info["versions"][pkg] = md.version(pkg)
        except Exception:
            info["versions"][pkg] = None

    info["available"] = True
    info["reason"] = "ok"
    return info


def _require():
    info = available()
    if not info["available"]:
        raise ChronosUnavailable(info["reason"])
    return info


def describe():
    """Provenance block -- goes straight into MLflow params and report headers."""
    info = available()
    return {"checkpoint": CHECKPOINT, "license": LICENSE, "shippable": SHIPPABLE,
             "available": info["available"], "reason": info["reason"],
             "versions": info["versions"], "quantile_levels": list(QUANTILE_LEVELS)}


# -- input hygiene --------------------------------------------------------------
def _clean_context(x, name="context"):
    """Trim leading/trailing non-finite values and interpolate interior gaps.

    No padding -- Chronos-2 has no minimum-input-patch requirement like
    TimesFM's 32-point floor, so the cleaned length IS the real context length.
    """
    a = np.asarray(x, dtype=np.float64).ravel()
    if a.size == 0:
        raise ValueError(f"{name}: empty context")
    finite = np.isfinite(a)
    if not finite.any():
        raise ValueError(f"{name}: all values non-finite")
    lo, hi = int(np.argmax(finite)), int(a.size - np.argmax(finite[::-1]))
    a = a[lo:hi]
    finite = np.isfinite(a)
    if not finite.all():
        idx = np.arange(a.size)
        a = np.interp(idx, idx[finite], a[finite])
    if not np.isfinite(a).all():
        raise ValueError(f"{name}: non-finite values survived cleaning")
    if a.size > MAX_CONTEXT:
        a = a[-MAX_CONTEXT:]
    return np.ascontiguousarray(a, dtype=np.float64)


# -- frame construction ---------------------------------------------------------
def _build_frames(contexts, horizon, freq, starts, past_only, past_future):
    """Assemble one train TimeSeriesDataFrame (+ optional known-covariates frame).

    Every context becomes its own item_id "o{i}", exactly mirroring TimesFM's
    "every origin is a prefix -> one batched call" design: one predictor fit,
    one batched predict.

    past_only[i]   (C1, L)   channels observed to the origin, unknown after --
                    become plain extra columns; AutoGluon auto-detects any
                    column not in known_covariates_names as past-covariate.
    past_future[i] (C2, L+H) channels known across the whole horizon -- split
                    into the history segment (extra train column) and the
                    future segment (the known_covariates frame).
    """
    from autogluon.timeseries import TimeSeriesDataFrame

    n_pf = past_future[0].shape[0] if past_future is not None else 0
    n_po = past_only[0].shape[0] if past_only is not None else 0
    known_names = [f"known_{k}" for k in range(n_pf)]
    past_names = [f"past_{k}" for k in range(n_po)]

    hist_frames, fut_frames = [], []
    for i, ctx in enumerate(contexts):
        item = f"o{i}"
        L = len(ctx)
        idx = pd.date_range(starts[i], periods=L, freq=freq)
        d = pd.DataFrame({"item_id": item, "timestamp": idx, "target": ctx})
        if past_future is not None:
            for k, name in enumerate(known_names):
                d[name] = past_future[i][k, :L]
        if past_only is not None:
            for k, name in enumerate(past_names):
                d[name] = past_only[i][k, :L]
        hist_frames.append(d)

        if past_future is not None:
            fidx = pd.date_range(idx[-1] + idx.freq, periods=horizon, freq=freq)
            fd = pd.DataFrame({"item_id": item, "timestamp": fidx})
            for k, name in enumerate(known_names):
                fd[name] = past_future[i][k, L:L + horizon]
            fut_frames.append(fd)

    train_df = pd.concat(hist_frames, ignore_index=True)
    train_tsdf = TimeSeriesDataFrame.from_data_frame(
        train_df, id_column="item_id", timestamp_column="timestamp")

    known_tsdf = None
    if past_future is not None:
        known_df = pd.concat(fut_frames, ignore_index=True)
        known_tsdf = TimeSeriesDataFrame.from_data_frame(
            known_df, id_column="item_id", timestamp_column="timestamp")

    return train_tsdf, known_tsdf, known_names


def _mean_from_quantiles(q):
    """Estimate E[X] per step by integrating the quantile function.

    AutoGluon's "mean" column for Chronos2 is BYTE-IDENTICAL to q0.5 -- verified
    empirically (forecast/chronos_smoke.py-style probe): Chronos-2 has no real
    distributional mean head, AutoGluon just duplicates the median under the
    "mean" name for API uniformity with models that do have one. Trusting it
    literally is exactly the bug timesfm_model._mean_from_quantiles was written
    to avoid: daily FFB harvest is zero-inflated and right-skewed (many zero
    days, occasional large ones), so the median sits well below the mean, and
    summing ~30 daily medians badly undershoots the monthly total. Aggregation
    needs E[X], and E[sum] = sum of E[X]. Identical formula to timesfm_model's.
    """
    q = np.asarray(q, dtype=float)
    levels = np.asarray(QUANTILE_LEVELS)
    body = np.trapezoid(q, levels, axis=-1)            # covers p in [0.1, 0.9]
    lo_tail = 0.1 * (q[..., 0] - 0.5 * (q[..., 1] - q[..., 0]))
    hi_tail = 0.1 * (q[..., -1] + 0.5 * (q[..., -1] - q[..., -2]))
    return body + lo_tail + hi_tail


def _quantile_matrix(item_pred):
    """Pull the 9 quantile columns out of one item's prediction slice, sorted.

    Column names are whatever quantile_levels stringifies to; matched by
    numeric value rather than assumed literal formatting, and asserted to be
    exactly QUANTILE_LEVELS so a mismatch fails loud instead of silently
    scoring the wrong columns.
    """
    q_cols = [c for c in item_pred.columns if c != "mean"]
    q_cols_sorted = sorted(q_cols, key=lambda c: float(c))
    levels = tuple(round(float(c), 4) for c in q_cols_sorted)
    if levels != QUANTILE_LEVELS:
        raise ValueError(f"unexpected quantile columns {levels}, "
                         f"expected {QUANTILE_LEVELS}")
    return item_pred[q_cols_sorted].to_numpy(dtype=float)


def load_predictor(path):
    """Load an already-fit predictor (e.g. a SageMaker LoRA-fine-tuned one).

    Thin wrapper so callers don't need their own autogluon.timeseries import
    just to reload a predictor for forecast_batch's `predictor=` argument.

    A predictor fine-tuned on SageMaker is pickled on Linux, so its internal
    path fields are PosixPath objects. Windows Python refuses to instantiate
    PosixPath at all (pathlib._abc.UnsupportedOperation) -- confirmed by a
    real crash -- which fires straight out of pickle.load(). Standard
    cross-platform pickle workaround: alias PosixPath to WindowsPath.

    NOT scoped to just this call: confirmed by a second real crash that
    AutoGluon lazily re-unpickles the trainer again from disk on every
    predictor.predict() call (load_trainer() inside learner.predict()), well
    after this function has returned -- so restoring PosixPath in a `finally`
    here left it broken again at predict time. The patch is process-lifetime
    instead, which is fine for a Windows-only compat shim in a script that
    has no other reason to construct a real PosixPath.

    Patching pathlib.PosixPath alone does NOT work on Python 3.13 -- confirmed
    by a third real crash, identical to the second. 3.13 refactored pathlib
    internals so the class's actual home is pathlib._local (PosixPath.
    __module__ == 'pathlib._local'; the top-level `pathlib.PosixPath` is just
    a re-exported reference to it). Pickle resolves classes by the module
    recorded in that attribute, i.e. pathlib._local.find_class(...), so only
    reassigning the name INSIDE pathlib._local actually changes what
    unpickling constructs.
    """
    _require()
    import os
    import pathlib
    from autogluon.timeseries import TimeSeriesPredictor

    if os.name == "nt":
        pathlib.PosixPath = pathlib.WindowsPath
        try:  # Python 3.13+ internal layout; harmless no-op on older Pythons
            import pathlib._local as _pathlib_local
            _pathlib_local.PosixPath = _pathlib_local.WindowsPath
        except ImportError:
            pass
    return TimeSeriesPredictor.load(path)


# -- inference ------------------------------------------------------------------
def forecast_batch(contexts, horizon, freq="MS", starts=None,
                   past_only=None, past_future=None,
                   point="model", clip_min=0.0, batch_size=256, device=None,
                   predictor=None, cross_learning=False):
    """Forecast a batch of contexts with Chronos-2.

    contexts    list of 1-D arrays. Every backtest origin is a PREFIX of the
                same series, so callers should pass all origins in one call --
                one predictor fit (no-op for zero-shot), one batched predict.
    freq        pandas offset alias ("MS" for monthly-start, "D" for daily).
    starts      list of real calendar start timestamps, one per context --
                required because AutoGluon's data model is timestamp-indexed.
    past_only   list (per context) of (C1, L) channels observed to the origin.
    past_future list (per context) of (C2, L+H) channels known across the
                whole horizon.
    point       "model"/"mean" integrate E[X] from the quantiles (Chronos-2 has
                no real mean head -- see _mean_from_quantiles); "median" uses q0.5.
    clip_min    lower clip, default 0.0 -- matches monthly_model's
                np.clip(..., 0, None) (FFB counts are non-negative).
    predictor   None (default) fits a fresh zero-shot predictor from CHECKPOINT
                per call -- Phase 1's path. Pass an already-fit TimeSeriesPredictor
                (e.g. from load_predictor(), after SageMaker LoRA fine-tuning) to
                run inference only -- Phase 2's path. Its prediction_length must
                equal `horizon` and its known_covariates_names must match the
                past_future channel count/order used here (both are set by
                whatever built its training data, so the caller must keep the
                same covariate construction on both sides).
    cross_learning  False (default) forecasts every context independently.
                AutoGluon's own default is True, which puts every item in a
                batch into ONE Chronos-2 attention group. The backtests here
                batch all walk-forward origins of a single series together, so
                with it on, an early origin attends to later origins' contexts,
                which contain the very months it is being scored on. That is
                test-set leakage, so the default is off; True exists only to
                reproduce the Phase 1 numbers produced before this was found.
                Ignored when `predictor` is given: a fit predictor carries the
                value it was fit with.

    Returns a list of dicts: {point (H,), q (H, 9), levels, n_context, mean}
    -- the same shape timesfm_model.forecast_batch returns, so the two are
    interchangeable from the scoring/conformal code's point of view.
    """
    _require()
    from autogluon.timeseries import TimeSeriesPredictor

    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if horizon > MAX_HORIZON:
        raise ValueError(f"horizon {horizon} exceeds Chronos-2 max {MAX_HORIZON}")
    if starts is None:
        raise ValueError("starts is required -- one real calendar timestamp "
                         "per context (AutoGluon is timestamp-indexed)")
    if predictor is not None and predictor.prediction_length != horizon:
        raise ValueError(
            f"predictor was fit with prediction_length={predictor.prediction_length}, "
            f"but horizon={horizon} was requested -- an already-fit predictor's "
            "horizon is fixed at fit time")

    cleaned = [_clean_context(c, name=f"context[{i}]")
               for i, c in enumerate(contexts)]
    n_real = [len(c) for c in cleaned]

    train_tsdf, known_tsdf, known_names = _build_frames(
        cleaned, horizon, freq, starts, past_only, past_future)

    if predictor is not None:
        preds = predictor.predict(train_tsdf, known_covariates=known_tsdf)
    else:
        tmpdir = tempfile.mkdtemp(prefix="chronos2_ag_")
        try:
            predictor = TimeSeriesPredictor(
                prediction_length=horizon,
                target="target",
                known_covariates_names=known_names or None,
                quantile_levels=list(QUANTILE_LEVELS),
                freq=freq,
                path=tmpdir,
                verbosity=1,
            )
            hp = {"model_path": CHECKPOINT, "batch_size": batch_size,
                  "cross_learning": bool(cross_learning)}
            if device:
                hp["device"] = device
            predictor.fit(train_tsdf, hyperparameters={"Chronos2": hp},
                         skip_model_selection=True, enable_ensemble=False)
            preds = predictor.predict(train_tsdf, known_covariates=known_tsdf)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    out = []
    for i in range(len(cleaned)):
        item_pred = preds.loc[f"o{i}"]
        q = _quantile_matrix(item_pred)
        mean = _mean_from_quantiles(q)  # NOT item_pred["mean"] -- see docstring above

        if point in ("model", "mean"):
            p = mean
        elif point == "median":
            p = q[:, MEDIAN_INDEX]
        else:
            raise ValueError(f"point must be 'model', 'median' or 'mean', got {point!r}")

        if clip_min is not None:
            p, q, mean = (np.clip(p, clip_min, None), np.clip(q, clip_min, None),
                         np.clip(mean, clip_min, None))
        out.append({"point": p, "q": q, "levels": QUANTILE_LEVELS,
                    "n_context": n_real[i], "mean": mean})
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(describe(), indent=2, default=str))
