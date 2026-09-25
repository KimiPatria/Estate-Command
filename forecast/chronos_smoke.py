"""
Phase-0 install gate for the Chronos-2 R&D benchmark.

Mirrors timesfm_smoke.py's role: proves the checkpoint actually loads and
infers on THIS machine, through chronos_model.py's AutoGluon-TimeSeries
wrapper, before the full walk-forward harness runs. Nothing here touches the
served pipeline; it only reads forecast/features_estate_monthly.csv.

Must be run from the `chronos-rd` conda env (see requirements-chronos.txt),
NOT the anaconda base env that serves the dashboard -- autogluon.timeseries
pins torch>=2.10, which would upgrade the dashboard's pinned torch 2.9.1.

Usage:
    conda run -n chronos-rd python forecast/chronos_smoke.py
"""

import os
import sys
import time

import numpy as np
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
ESTATE_FILE = os.path.join(_DIR, "features_estate_monthly.csv")
HORIZON = 12

sys.path.insert(0, _DIR)
import chronos_model as cm  # noqa: E402


def _k3_monthly():
    if not os.path.exists(ESTATE_FILE):
        return None
    df = pd.read_csv(ESTATE_FILE)
    return df["bunches_total"].astype("float64").to_numpy()


def _check(name, point, quant, horizon, n_q=9):
    problems = []
    if point.shape != (horizon,):
        problems.append(f"point shape {point.shape} != ({horizon},)")
    if quant.shape != (horizon, n_q):
        problems.append(f"quantile shape {quant.shape} != ({horizon}, {n_q})")
    if not np.isfinite(point).all():
        problems.append("point contains non-finite values")
    if not np.isfinite(quant).all():
        problems.append("quantiles contain non-finite values")
    if np.any(np.diff(quant, axis=-1) < -1e-3):
        problems.append("quantiles cross (not monotone along quantile axis)")
    status = "OK" if not problems else "PROBLEM"
    print(f"      {name:22s} point[:3]={np.round(point[:3], 1)}  "
          f"q.shape={quant.shape}  {status}")
    for p in problems:
        print(f"        !! {p}")
    return problems


def smoke():
    problems = []

    sine = np.sin(np.linspace(0, 24, 100))
    starts = [pd.Timestamp("2020-01-01")]
    t0 = time.perf_counter()
    out = cm.forecast_batch([sine], HORIZON, freq="D", starts=starts)
    infer_s = time.perf_counter() - t0
    problems += _check("synthetic sine", out[0]["point"], out[0]["q"], HORIZON)
    print(f"    single-context inference (incl. model load): {infer_s:.1f}s")

    k3 = _k3_monthly()
    if k3 is not None:
        # Real batching pattern the harness will use: every backtest origin is
        # a PREFIX of the same series, so all origins go in ONE call.
        ctxs = [k3[:n] for n in range(6, len(k3))]
        starts = [pd.Timestamp("2000-01-01")] * len(ctxs)  # MS-aligned, arbitrary
        t0 = time.perf_counter()
        outs = cm.forecast_batch(ctxs, HORIZON, freq="MS", starts=starts)
        batch_s = time.perf_counter() - t0
        print(f"    k3 batched: {len(ctxs)} prefix contexts "
              f"(len {len(ctxs[0])}..{len(ctxs[-1])}) in {batch_s:.1f}s")
        problems += _check("k3 monthly (last)", outs[-1]["point"], outs[-1]["q"],
                           HORIZON)
        neg = sum(int((o["point"] < 0).any()) for o in outs)
        print(f"      {neg}/{len(outs)} contexts produced negative point values "
              f"pre-clip")

        # Covariate smoke: two synthetic calendar channels, past_future style.
        L, H = len(ctxs[-1]), HORIZON
        pf = np.vstack([np.sin(np.arange(L + H) * 2 * np.pi / 12),
                        np.cos(np.arange(L + H) * 2 * np.pi / 12)])
        out_cov = cm.forecast_batch([ctxs[-1]], H, freq="MS",
                                    starts=[pd.Timestamp("2000-01-01")],
                                    past_future=[pf])
        problems += _check("k3 + calendar covariates", out_cov[0]["point"],
                           out_cov[0]["q"], HORIZON)
    return problems


def main():
    import importlib.metadata as md
    print("=" * 66)
    print("Chronos-2 Phase-0 smoke test")
    print("=" * 66)
    print(f"  python           {sys.version.split()[0]}")
    print(f"                   {sys.executable}")

    info = cm.available()
    if not info["available"]:
        print(f"  NOT AVAILABLE: {info['reason']}")
        return 1
    for p, v in info["versions"].items():
        print(f"  {p:24s} {v or 'MISSING'}")
    print(f"  checkpoint       {cm.CHECKPOINT}  ({cm.LICENSE}, shippable={cm.SHIPPABLE})")

    k3 = _k3_monthly()
    print(f"  k3 series        "
          f"{str(len(k3)) + ' months' if k3 is not None else 'CSV not found'}")

    print("\n--- inference ---")
    try:
        problems = smoke()
        result = "PASS" if not problems else f"FAIL ({len(problems)} problems)"
    except Exception:
        import traceback
        traceback.print_exc()
        result = "FAIL (exception, see traceback)"

    print("\n" + "=" * 66)
    print(f"  {result}")
    print("=" * 66)
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
