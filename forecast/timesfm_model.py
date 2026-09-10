"""
TimesFM adapter -- one interface over the 3.0 and 2.5 backends (Stage R&D).

This is a BENCHMARK component. Nothing in the served pipeline imports it, and
nothing here writes to monthly_model.joblib or changes /forecast/data. It
exists to answer one question honestly: can a zero-shot foundation model match
the incumbent Ensemble(SARIMAX+Trailing3+LightGBM_workdone) on our data?

Why two backends behind one signature
-------------------------------------
The licenses differ and that decides what a "win" is worth:

  * 3.0 (google/timesfm-3.0-pytorch, 330M) -- weights are
    timesfm-non-commercial-license-v1.0. Non-commercial, non-production. It has
    native multivariate covariate channels, so it answers "what is the ceiling".
    A 3.0 win can inform strategy but CANNOT be served.
  * 2.5 (google/timesfm-2.5-200m-pytorch, 200M) -- weights are Apache-2.0, so
    this is the only shippable backend. Its covariate path (XReg) needs
    jax[cuda], which has no Windows wheels, so on this machine 2.5 is
    univariate-only.

Both are Apache-2.0 in *code*; only the 3.0 checkpoint is restricted.

Normalization contract
----------------------
Backends disagree on nearly every surface, so this module flattens them:

                    3.0                              2.5
    construct       TimesFM3Evaluator(ModelConfig)   from_pretrained + .compile()
    call            .predict_batch(...) -> GENERATOR .forecast(...) -> tuple
    quantiles       (H, 9)  = q0.1..q0.9             (H, 10) = mean, then q0.1..q0.9
    covariates      native channels                  XReg only (unavailable here)

After this module: `q` is ALWAYS (H, 9) holding q0.1..q0.9, median at index 4.

Design constraints this module has to honour
--------------------------------------------
  * Lazy + process-cached load. A 330M checkpoint is ~1.3 GB resident and takes
    tens of seconds to load; it must never load at import and never reload per
    backtest fold. That cache is the whole cost model, not an optimisation.
  * Graceful unavailability. `import timesfm` happens inside calls, guarded --
    mirroring how monthly_model guards the workdone file and MLflow. With
    timesfm uninstalled, importing this module and calling available() must
    both still work, so the live pipeline can never break because of it.
  * Identical preprocessing across backends, so a 3.0-vs-2.5 comparison is not
    confounded by differing input hygiene.
"""

import hashlib
import importlib.util
import threading

import numpy as np

BACKEND_30 = "3.0"
BACKEND_25 = "2.5"
BACKENDS = (BACKEND_30, BACKEND_25)
DEFAULT_BACKEND = BACKEND_30

CHECKPOINTS = {
    BACKEND_30: "google/timesfm-3.0-pytorch",
    BACKEND_25: "google/timesfm-2.5-200m-pytorch",
}
LICENSES = {
    BACKEND_30: "timesfm-non-commercial-license-v1.0",
    BACKEND_25: "Apache-2.0",
}
SHIPPABLE = {BACKEND_30: False, BACKEND_25: True}

# Quantile levels after normalization, for both backends.
QUANTILE_LEVELS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
MEDIAN_INDEX = 4

# Upstream limits worth asserting against rather than discovering at runtime.
MAX_CONTEXT_30 = 15360
MAX_VARIATES_30 = 32   # targets + covariate channels share this budget
INPUT_PATCH_LEN = 32   # contexts shorter than this get left-padded

_LOCK = threading.Lock()
_CACHE = {}


class TimesFMUnavailable(RuntimeError):
    """timesfm is not installed, or the requested backend cannot be used."""


# -- availability -------------------------------------------------------------
def available(backend=DEFAULT_BACKEND):
    """Cheap, never raises. Uses find_spec so it does not import timesfm/torch."""
    info = {
        "available": False,
        "backend": backend,
        "checkpoint": CHECKPOINTS.get(backend),
        "license": LICENSES.get(backend),
        "shippable": SHIPPABLE.get(backend),
        "reason": "",
        "versions": {},
    }
    if backend not in BACKENDS:
        info["reason"] = f"unknown backend {backend!r}; expected one of {BACKENDS}"
        return info
    try:
        if importlib.util.find_spec("timesfm") is None:
            info["reason"] = 'timesfm not installed (pip install "timesfm[torch]")'
            return info
        if importlib.util.find_spec("torch") is None:
            info["reason"] = "torch not installed"
            return info
    except Exception as exc:                    # find_spec can raise on broken installs
        info["reason"] = f"{type(exc).__name__}: {exc}"
        return info

    import importlib.metadata as md
    for pkg in ("timesfm", "torch", "numpy", "huggingface_hub", "safetensors"):
        try:
            info["versions"][pkg] = md.version(pkg)
        except Exception:
            info["versions"][pkg] = None
    if info["versions"].get("safetensors") is None:
        info["reason"] = "safetensors missing; both checkpoints load .safetensors weights"
        return info

    info["available"] = True
    info["reason"] = "ok"
    return info


def _require(backend):
    info = available(backend)
    if not info["available"]:
        raise TimesFMUnavailable(f"backend {backend}: {info['reason']}")
    return info


# -- loading ------------------------------------------------------------------
def load(backend=DEFAULT_BACKEND, checkpoint=None, device="cpu",
         max_context=1024, max_horizon=64, per_core_batch_size=16,
         revision=None):
    """Lazily construct and process-cache a forecaster.

    max_context / max_horizon are part of the cache key because 2.5 bakes them
    into .compile(ForecastConfig(...)) -- monthly and daily genuinely need
    different compiled objects. 3.0 ignores them (its context cap is fixed at
    MAX_CONTEXT_30) but keying uniformly keeps the two backends symmetric.

    `revision` pins the HuggingFace checkpoint SHA. A zero-shot benchmark is
    only reproducible if the weights are pinned, so record whatever is used.
    """
    _require(backend)
    ckpt = checkpoint or CHECKPOINTS[backend]
    key = (backend, ckpt, device, max_context, max_horizon,
           per_core_batch_size, revision)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]

        if backend == BACKEND_30:
            from timesfm3 import ModelConfig, TimesFM3Evaluator
            cfg = dict(checkpoint_path=ckpt, device=device,
                       per_core_batch_size=per_core_batch_size)
            if revision:
                cfg["revision"] = revision
            model = TimesFM3Evaluator(ModelConfig(**cfg))
        else:
            import timesfm
            kw = {"revision": revision} if revision else {}
            model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(ckpt, **kw)
            model.compile(timesfm.ForecastConfig(
                max_context=max_context, max_horizon=max_horizon,
                normalize_inputs=True,          # context-local, safe
                use_continuous_quantile_head=True,
                force_flip_invariance=True,
                infer_is_positive=True,         # FFB counts are non-negative
                fix_quantile_crossing=True,
            ))
        _CACHE[key] = model
        return model


def describe(backend=DEFAULT_BACKEND, checkpoint=None):
    """Provenance block -- goes straight into MLflow params and report headers."""
    info = available(backend)
    return {
        "backend": backend,
        "checkpoint": checkpoint or CHECKPOINTS.get(backend),
        "license": LICENSES.get(backend),
        "shippable": SHIPPABLE.get(backend),
        "available": info["available"],
        "reason": info["reason"],
        "versions": info["versions"],
        "quantile_levels": list(QUANTILE_LEVELS),
    }


# -- input hygiene ------------------------------------------------------------
def _clean_context(x, name="context"):
    """Normalize one context array so both backends see byte-identical input.

    TimesFM 3.0 strips leading NaN and interpolates interior NaN itself, but
    puts TRAILING NaN on the caller; 2.5 documents no NaN handling at all.
    Doing all of it here means a 3.0-vs-2.5 delta reflects the models, not
    their differing preprocessing.
    """
    a = np.asarray(x, dtype=np.float64).ravel()
    if a.size == 0:
        raise ValueError(f"{name}: empty context")

    finite = np.isfinite(a)
    if not finite.any():
        raise ValueError(f"{name}: all values non-finite")
    # trim leading and trailing non-finite
    lo, hi = int(np.argmax(finite)), int(a.size - np.argmax(finite[::-1]))
    a = a[lo:hi]
    finite = np.isfinite(a)
    if not finite.all():                       # interpolate interior gaps
        idx = np.arange(a.size)
        a = np.interp(idx, idx[finite], a[finite])
    if not np.isfinite(a).all():               # fail loud, never forecast garbage
        raise ValueError(f"{name}: non-finite values survived cleaning")

    n_real = a.size
    if a.size < INPUT_PATCH_LEN:               # left-pad to one input patch
        a = np.concatenate([np.zeros(INPUT_PATCH_LEN - a.size), a])
    return np.ascontiguousarray(a, dtype=np.float32), n_real


def _clean_cov(mat, want_len, name, pad=0):
    """Covariate channels -> (C, want_len + pad) float32, no NaN allowed.

    `pad` mirrors the left-padding _clean_context applied to the target when
    the context was shorter than one input patch. Target and covariates must
    stay aligned on the time axis, so whatever padding the target received the
    covariates receive too. Channels are standardized upstream, so zero-padding
    inserts the channel mean -- the neutral value, matching the target's own
    zero-padding rather than inventing signal.
    """
    m = np.atleast_2d(np.asarray(mat, dtype=np.float64))
    if m.shape[1] != want_len:
        raise ValueError(f"{name}: expected {want_len} timesteps, got {m.shape[1]}")
    if not np.isfinite(m).all():
        raise ValueError(
            f"{name}: contains NaN/inf. A channel may only be a past_future "
            "covariate if it is fully populated across the horizon -- demote "
            "partially-defined channels to past_only instead.")
    if pad:
        m = np.hstack([np.zeros((m.shape[0], pad)), m])
    return np.ascontiguousarray(m, dtype=np.float32)


def context_hash(contexts, horizon, backend, extra=""):
    """Stable key for memoising inference results between scoring re-runs."""
    h = hashlib.sha1()
    h.update(f"{backend}|{horizon}|{extra}".encode())
    for c in contexts:
        a = np.asarray(c, dtype=np.float32)
        h.update(str(a.shape).encode())
        h.update(a.tobytes())
    return h.hexdigest()


# -- inference ----------------------------------------------------------------
def forecast_batch(contexts, horizon, backend=DEFAULT_BACKEND,
                   past_only=None, past_future=None,
                   point="model", clip_min=0.0, **load_kw):
    """Forecast a batch of contexts.

    contexts    list of 1-D arrays. Every backtest origin is a PREFIX of the
                same series, so the caller should pass all origins at once --
                one model load, one batched forward pass. TimesFM decodes the
                whole horizon non-autoregressively, so horizon costs far less
                than a recursive model's H sequential steps.
    past_only   list (per context) of (C1, L) channels observed to the origin
                but unknown after it.
    past_future list (per context) of (C2, L+H) channels known across the whole
                horizon -- calendar terms, or climatology-backfilled values.
                3.0 only.
    point       "model" uses the backend's own point head; "median" uses q0.5.
                For skewed positive count data the median is often the better
                point estimate, and it costs nothing to test both.
    clip_min    lower clip, default 0.0 -- matches the np.clip(..., 0, None)
                used throughout monthly_model.

    Returns a list of dicts: {point (H,), q (H, 9), levels, backend, n_context}
    where n_context is the REAL context length before any padding, so the
    report can flag folds that ran below TimesFM's own 32-point minimum.
    """
    if backend not in BACKENDS:
        raise TimesFMUnavailable(f"unknown backend {backend!r}")
    if horizon < 1:
        raise ValueError("horizon must be >= 1")

    cleaned, n_real = [], []
    for i, c in enumerate(contexts):
        a, n = _clean_context(c, name=f"context[{i}]")
        if backend == BACKEND_30 and a.size > MAX_CONTEXT_30:
            a = a[-MAX_CONTEXT_30:]
        cleaned.append(a)
        n_real.append(n)

    if (past_only or past_future) and backend == BACKEND_25:
        raise TimesFMUnavailable(
            "TimesFM 2.5 exposes covariates only through XReg, which requires "
            'timesfm[xreg] -> jax[cuda] and has no Windows wheels. Use backend '
            '"3.0" for covariates, or run 2.5 univariate.')

    model = load(backend=backend, **load_kw)

    if backend == BACKEND_30:
        # Covariates are supplied at the REAL context length; pad them by the
        # same amount _clean_context padded the target, so the time axes align.
        pads = [len(cleaned[i]) - n_real[i] for i in range(len(cleaned))]
        po = pf = None
        if past_only is not None:
            po = [_clean_cov(m, n_real[i], f"past_only[{i}]", pad=pads[i])
                  for i, m in enumerate(past_only)]
        if past_future is not None:
            pf = [_clean_cov(m, n_real[i] + horizon, f"past_future[{i}]",
                             pad=pads[i])
                  for i, m in enumerate(past_future)]
        n_chan = 1 + (po[0].shape[0] if po else 0) + (pf[0].shape[0] if pf else 0)
        if n_chan > MAX_VARIATES_30:
            raise ValueError(
                f"{n_chan} channels exceeds TimesFM 3.0 max_variates="
                f"{MAX_VARIATES_30} (targets and covariates share the budget)")

        # predict_batch returns a GENERATOR -- indexing the raw return raises.
        outs = list(model.predict_batch(
            contexts=cleaned, horizon=horizon,
            past_only_covariates=po, past_future_covariates=pf,
            return_quantiles=True, use_symmetric_averaging=False))
        pts = [np.asarray(o.forecast, dtype=np.float64).reshape(-1)[:horizon]
               for o in outs]
        qs = [np.asarray(o.quantiles, dtype=np.float64).reshape(horizon, -1)
              for o in outs]
        means = [None] * len(outs)          # 3.0 emits no mean head
    else:
        pt, qt = model.forecast(horizon=horizon, inputs=cleaned)
        pts = [np.asarray(p, dtype=np.float64).reshape(-1)[:horizon] for p in pt]
        # 2.5 returns 10 slices: col 0 is the MEAN, cols 1..9 are q0.1..q0.9.
        qs = [np.asarray(q, dtype=np.float64).reshape(horizon, -1)[:, 1:10]
              for q in qt]
        means = [np.asarray(q, dtype=np.float64).reshape(horizon, -1)[:, 0]
                 for q in qt]

    out = []
    for p, q, mu, n in zip(pts, qs, means, n_real):
        if q.shape[1] != len(QUANTILE_LEVELS):
            raise ValueError(f"unexpected quantile width {q.shape[1]}, "
                             f"expected {len(QUANTILE_LEVELS)}")
        if point == "median":
            p = q[:, MEDIAN_INDEX]
        elif point == "mean":
            p = mu if mu is not None else _mean_from_quantiles(q)
        elif point != "model":
            raise ValueError(
                f"point must be 'model', 'median' or 'mean', got {point!r}")
        if clip_min is not None:
            p, q = np.clip(p, clip_min, None), np.clip(q, clip_min, None)
        rec = {"point": p, "q": q, "levels": QUANTILE_LEVELS,
               "backend": backend, "n_context": n,
               "mean": mu if mu is not None else _mean_from_quantiles(q)}
        out.append(rec)
    return out


def _mean_from_quantiles(q):
    """Estimate E[X] per step by integrating the quantile function.

    Needed because the backends' point head is the MEDIAN (verified: point and
    median outputs are identical). That is the right point estimate for a
    single step, but it is the WRONG thing to sum: for a right-skewed series
    (daily harvest is zero-inflated with CV ~180%) the median sits well below
    the mean, so adding up 30 daily medians systematically undershoots the
    monthly total. Aggregation needs E[X], and E[sum] = sum of E[X].

    Only q0.1..q0.9 are available, so integrate the interior with the trapezoid
    rule and extrapolate the two 10% tails linearly from the outermost decile
    gaps. The upper tail of a skewed distribution is exactly where the mass
    the median misses lives, so dropping it would reintroduce the same bias in
    smaller form. 2.5 exposes a real mean head and uses that instead.
    """
    q = np.asarray(q, dtype=float)
    levels = np.asarray(QUANTILE_LEVELS)
    body = np.trapezoid(q, levels, axis=-1)            # covers p in [0.1, 0.9]
    lo_tail = 0.1 * (q[..., 0] - 0.5 * (q[..., 1] - q[..., 0]))
    hi_tail = 0.1 * (q[..., -1] + 0.5 * (q[..., -1] - q[..., -2]))
    return body + lo_tail + hi_tail


def forecast_with_bands(values, horizon=3, backend=DEFAULT_BACKEND,
                        lo=0.1, hi=0.9, **kw):
    """Single-series convenience returning (point, lower, upper).

    Matches the interface convention the repo already used for its previous
    foundation-model attempt (see forecast/serving.py's ttm_model call).
    """
    if lo not in QUANTILE_LEVELS or hi not in QUANTILE_LEVELS:
        raise ValueError(f"lo/hi must be in {QUANTILE_LEVELS}")
    r = forecast_batch([values], horizon, backend=backend, **kw)[0]
    li, hi_i = QUANTILE_LEVELS.index(lo), QUANTILE_LEVELS.index(hi)
    return r["point"], r["q"][:, li], r["q"][:, hi_i]


if __name__ == "__main__":
    import json
    for b in BACKENDS:
        print(json.dumps(describe(b), indent=2, default=str))
