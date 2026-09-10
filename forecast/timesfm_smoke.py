"""
Phase-0 install gate for the TimesFM R&D benchmark.

Proves the checkpoint actually loads and infers on THIS machine before any
harness code is written. Nothing here touches the served pipeline; it only
reads forecast/features_estate_monthly.csv.

Two things this is specifically designed to catch, both real risks on this box:
  * huggingface_hub is 1.14.0 (a 1.x major) while timesfm declares >=0.28.0.
    1.x dropped several legacy download APIs, so checkpoint resolution is the
    first suspect if loading fails.
  * Native Windows is only "partial support" upstream (WSL2 is the fully
    supported path). If a backend fails here, that is the fallback.

Weights cache under ~/.cache/huggingface, deliberately OUTSIDE the
OneDrive-synced repo tree -- do not point the cache into the repo.

Usage:
    python forecast/timesfm_smoke.py                 # both backends
    python forecast/timesfm_smoke.py --backend 3.0   # just one
"""

import argparse
import os
import sys
import time

import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
ESTATE_FILE = os.path.join(_DIR, "features_estate_monthly.csv")

CHECKPOINTS = {
    "3.0": "google/timesfm-3.0-pytorch",       # 330M, NON-COMMERCIAL weights
    "2.5": "google/timesfm-2.5-200m-pytorch",  # 200M, Apache-2.0 weights
}
LICENSES = {
    "3.0": "timesfm-non-commercial-license-v1.0 (benchmark only)",
    "2.5": "Apache-2.0 (shippable)",
}

HORIZON = 12


def _k3_monthly():
    """Real k3 target series, or None if the CSV is absent."""
    if not os.path.exists(ESTATE_FILE):
        return None
    import pandas as pd
    df = pd.read_csv(ESTATE_FILE)
    return df["bunches_total"].astype("float32").to_numpy()


def _check(name, point, quant, horizon, n_q, q_start=0):
    """Shape / sanity assertions shared by both backends.

    q_start marks where the actual quantiles begin. 2.5 returns 10 columns
    whose FIRST column is the mean, not a quantile -- including it in the
    monotonicity test would fail by construction, since the mean has no reason
    to sit below q0.1. 3.0 returns 9 pure quantiles, so q_start=0 there.
    """
    problems = []
    if point.shape != (horizon,):
        problems.append(f"point shape {point.shape} != ({horizon},)")
    if quant.shape != (horizon, n_q):
        problems.append(f"quantile shape {quant.shape} != ({horizon}, {n_q})")
    if not np.isfinite(point).all():
        problems.append("point contains non-finite values")
    if not np.isfinite(quant).all():
        problems.append("quantiles contain non-finite values")
    # quantiles proper must be non-decreasing across the quantile axis
    q = quant[:, q_start:] if quant.ndim == 2 else quant
    if q.ndim == 2 and q.shape[1] == 9 and np.any(np.diff(q, axis=-1) < -1e-3):
        problems.append("quantiles cross (not monotone along quantile axis)")
    status = "OK" if not problems else "PROBLEM"
    print(f"      {name:22s} point[:3]={np.round(point[:3], 1)}  "
          f"q.shape={quant.shape}  {status}")
    for p in problems:
        print(f"        !! {p}")
    return problems


def smoke_30():
    from timesfm3 import ModelConfig, TimesFM3Evaluator

    t0 = time.perf_counter()
    fc = TimesFM3Evaluator(ModelConfig(
        checkpoint_path=CHECKPOINTS["3.0"],
        per_core_batch_size=16,
        device="cpu",
    ))
    print(f"    load: {time.perf_counter() - t0:.1f}s")

    problems = []
    sine = np.sin(np.linspace(0, 24, 100)).astype("float32")
    t0 = time.perf_counter()
    # predict_batch returns a GENERATOR -- indexing the raw return raises.
    out = list(fc.predict_batch(contexts=[sine], horizon=HORIZON,
                                return_quantiles=True,
                                use_symmetric_averaging=False))
    infer_s = time.perf_counter() - t0
    problems += _check("synthetic sine", out[0].forecast, out[0].quantiles,
                       HORIZON, 9)
    print(f"    single-context inference: {infer_s:.2f}s")

    k3 = _k3_monthly()
    if k3 is not None:
        # The real batching pattern the harness will use: every backtest origin
        # is a PREFIX of the same series, so all origins go in ONE call.
        ctxs = [k3[:n] for n in range(6, len(k3))]
        t0 = time.perf_counter()
        outs = list(fc.predict_batch(contexts=ctxs, horizon=HORIZON,
                                     return_quantiles=True,
                                     use_symmetric_averaging=False))
        batch_s = time.perf_counter() - t0
        print(f"    k3 batched: {len(ctxs)} prefix contexts "
              f"(len {len(ctxs[0])}..{len(ctxs[-1])}) in {batch_s:.2f}s")
        problems += _check("k3 monthly (last)", outs[-1].forecast,
                           outs[-1].quantiles, HORIZON, 9)
        neg = sum(int((o.forecast < 0).any()) for o in outs)
        print(f"      {neg}/{len(outs)} contexts produced negative values "
              f"(harness clips at 0, matching monthly_model)")
    return problems


def smoke_25():
    import timesfm

    t0 = time.perf_counter()
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(CHECKPOINTS["2.5"])
    model.compile(timesfm.ForecastConfig(
        max_context=1024, max_horizon=64,
        normalize_inputs=True,
        use_continuous_quantile_head=True,
        force_flip_invariance=True,
        infer_is_positive=True,
        fix_quantile_crossing=True,
    ))
    print(f"    load+compile: {time.perf_counter() - t0:.1f}s")

    problems = []
    sine = np.sin(np.linspace(0, 24, 100)).astype("float32")
    t0 = time.perf_counter()
    point, quant = model.forecast(horizon=HORIZON, inputs=[sine])
    infer_s = time.perf_counter() - t0
    # 2.5 returns 10 slices: col 0 is the MEAN, cols 1..9 are q0.1..q0.9.
    problems += _check("synthetic sine", point[0], quant[0], HORIZON, 10, q_start=1)
    print(f"    single-context inference: {infer_s:.2f}s")

    k3 = _k3_monthly()
    if k3 is not None:
        ctxs = [k3[:n] for n in range(6, len(k3))]
        t0 = time.perf_counter()
        point, quant = model.forecast(horizon=HORIZON, inputs=ctxs)
        batch_s = time.perf_counter() - t0
        print(f"    k3 batched: {len(ctxs)} prefix contexts in {batch_s:.2f}s")
        problems += _check("k3 monthly (last)", point[-1], quant[-1],
                           HORIZON, 10, q_start=1)
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["3.0", "2.5", "both"], default="both")
    args = ap.parse_args()

    import importlib.metadata as md
    print("=" * 66)
    print("TimesFM Phase-0 smoke test")
    print("=" * 66)
    print(f"  python           {sys.version.split()[0]}")
    print(f"                   {sys.executable}")
    for p in ("timesfm", "torch", "numpy", "huggingface_hub", "safetensors"):
        try:
            print(f"  {p:16s} {md.version(p)}")
        except Exception:
            print(f"  {p:16s} MISSING")
    try:
        import jax  # noqa: F401
        print("  !! jax installed -- unexpected; [xreg]/[flax] must not be used here")
    except ImportError:
        print("  jax              absent (correct for [torch])")

    k3 = _k3_monthly()
    print(f"  k3 series        "
          f"{str(len(k3)) + ' months' if k3 is not None else 'CSV not found'}")

    backends = ["3.0", "2.5"] if args.backend == "both" else [args.backend]
    results = {}
    for b in backends:
        print(f"\n--- backend {b}  ({CHECKPOINTS[b]}) ---")
        print(f"    license: {LICENSES[b]}")
        try:
            problems = (smoke_30 if b == "3.0" else smoke_25)()
            results[b] = "PASS" if not problems else f"FAIL ({len(problems)} problems)"
        except Exception as exc:
            import traceback
            traceback.print_exc()
            results[b] = f"FAIL ({type(exc).__name__}: {exc})"

    print("\n" + "=" * 66)
    for b, r in results.items():
        print(f"  backend {b:4s} {r}")
    print("=" * 66)
    return 0 if all(r == "PASS" for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
