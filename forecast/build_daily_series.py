"""
Daily estate harvest series + the daily->monthly reconciliation gate.

Built for the TimesFM daily track: k3 has only 36 usable MONTHLY points, which
sits right at the floor of TimesFM's stated minimum (>=32 observations and 3-5
full seasonal cycles). The same history as a DAILY series is ~1,242 points --
about 3.4 annual cycles at 40x the observation count, which is the regime
foundation models are actually designed for. Forecast daily, sum to monthly,
and the result is still scoreable on the incumbent's monthly sMAPE/MASE.

Gap-fill policy: ZERO, not NaN
------------------------------
Of 1,242 calendar days, 971 carry harvest rows and 271 have none. Coverage is
spread evenly across all seven weekdays, which reads as genuine non-harvest
days (holidays, rain-outs), not missing data. Zero is also the only choice
consistent with the objective: the monthly figure is a SUM, and a non-harvest
day contributes zero to it. NaN would be actively wrong -- TimesFM linearly
interpolates interior NaN, inventing production on days when none happened and
systematically inflating every monthly aggregate. `--gap-fill nan` exists so
the choice can be measured rather than asserted.

The reconciliation gate
-----------------------
`bunches_total` in features_estate_monthly.csv is a CLEANED target: seasonally
imputed on under-recorded months. The raw daily rows sum to `bunches_total_raw`,
NOT to `bunches_total`. So the gate reconciles against `_raw`, while scoring
still uses the cleaned `bunches_total` the incumbent uses. Where the two differ,
the daily model is being asked to predict recorded harvest while being scored
against imputed harvest -- a real target mismatch, so the gate counts those
months explicitly instead of hiding them.

Run the gate BEFORE modelling. If the actuals do not round-trip, the bridge is
broken and the daily track is invalid.

Usage:
    python forecast/build_daily_series.py                 # k3, run the gate
    python forecast/build_daily_series.py --estate ec
"""

import argparse
import os

import numpy as np
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))

# k3 keeps the legacy top-level layout; everything added since lives in
# forecast/<ESTATE>/ (mirrors ESTATE_DIRS in forecast_router.py).
RAW_FILES = {
    "k3": (_DIR, "epms_production_daily.csv"),
    "ec": (os.path.join(_DIR, "EC"), "EC_oph.csv"),
}


def _estate_dir(estate):
    return RAW_FILES.get(estate, (_DIR, "epms_production_daily.csv"))[0]


def load_raw(path):
    """Daily harvest rows, tolerating both estates' file conventions.

    k3 writes M/D/YYYY dates; EC's export is quoted, ISO-dated and carries the
    'divison_code' typo (the same shim build_estate_features.py already needs).
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().strip('"') for c in df.columns]
    if "divison_code" in df.columns and "division_code" not in df.columns:
        df = df.rename(columns={"divison_code": "division_code"})
    df["harvest_date"] = pd.to_datetime(df["harvest_date"], format="mixed")
    df["bunches_total"] = pd.to_numeric(df["bunches_total"], errors="coerce").fillna(0)
    return df


def build(raw_path, gap_fill="zero"):
    """Daily estate-total series on a COMPLETE calendar index.

    Returns a float Series indexed by day from first to last harvest date, with
    absent days filled per `gap_fill`. Trailing NaN is never produced (the index
    ends on a real harvest day), which matters because TimesFM leaves trailing
    NaN to the caller.
    """
    if gap_fill not in ("zero", "nan"):
        raise ValueError("gap_fill must be 'zero' or 'nan'")
    df = load_raw(raw_path)
    s = df.groupby("harvest_date")["bunches_total"].sum().sort_index()
    full = pd.date_range(s.index.min(), s.index.max(), freq="D")
    s = s.reindex(full)
    if gap_fill == "zero":
        s = s.fillna(0.0)
    s.index.name = "date"
    s.name = "bunches_total"
    return s.astype(float)


def monthly_bridge_check(daily, estate_csv):
    """Prove the daily series reconciles to the monthly target before modelling.

    Compares zero-filled daily sums against `bunches_total_raw` per month, and
    separately reports how far the CLEANED `bunches_total` diverges -- that
    divergence is the imputation the incumbent trains on, and it is the honest
    upper bound on what a daily model can be scored at.
    """
    est = pd.read_csv(estate_csv)
    est["month"] = pd.PeriodIndex(est["month"], freq="M")

    agg = daily.fillna(0.0).groupby(daily.index.to_period("M")).sum()
    agg.index.name = "month"

    m = est.merge(agg.rename("daily_sum"), left_on="month", right_index=True,
                  how="left")
    m["d_raw"] = m["daily_sum"] - m["bunches_total_raw"]
    m["d_clean"] = m["daily_sum"] - m["bunches_total"]

    # Partial months are endpoints where the daily file itself is truncated, so
    # exclude them from the reconciliation the same way the model excludes them.
    full = m[m["is_partial"] == 0]
    tol = 0.5  # counts are integers; anything above this is a real mismatch
    bad = full[full["d_raw"].abs() > tol]

    scoreable = m[m["exclude_from_model"] == 0]
    mismatch = scoreable[scoreable["d_clean"].abs() > tol]

    return {
        "ok": len(bad) == 0,
        "n_months": len(m),
        "n_full_months": len(full),
        "n_raw_mismatch": len(bad),
        "max_abs_raw_diff": float(full["d_raw"].abs().max()) if len(full) else 0.0,
        "raw_mismatch_months": [str(x) for x in bad["month"]],
        "n_scoreable": len(scoreable),
        "n_imputed_scoreable": len(mismatch),
        "imputed_months": [str(x) for x in mismatch["month"]],
        "imputed_share_pct": (
            100.0 * mismatch["d_clean"].abs().sum() / scoreable["bunches_total"].sum()
            if len(scoreable) else 0.0),
        "frame": m,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--estate", default="k3", choices=sorted(RAW_FILES))
    ap.add_argument("--gap-fill", default="zero", choices=["zero", "nan"])
    args = ap.parse_args()

    d, fname = RAW_FILES[args.estate]
    raw_path = os.path.join(d, fname)
    estate_csv = os.path.join(d, "features_estate_monthly.csv")

    daily = build(raw_path, gap_fill=args.gap_fill)
    n_zero = int((daily == 0).sum())
    print(f"[{args.estate}] daily series: {len(daily)} calendar days "
          f"{daily.index.min().date()} -> {daily.index.max().date()}")
    print(f"          harvest days {len(daily) - n_zero} | zero-filled {n_zero} "
          f"({100 * n_zero / len(daily):.1f}%)")
    print(f"          mean {daily.mean():,.0f} | std {daily.std():,.0f} | "
          f"max {daily.max():,.0f} | CV {100 * daily.std() / daily.mean():.0f}%")

    print(f"\n--- daily -> monthly reconciliation gate ---")
    r = monthly_bridge_check(daily, estate_csv)
    print(f"  months {r['n_months']} | non-partial {r['n_full_months']} | "
          f"scoreable {r['n_scoreable']}")
    print(f"  vs bunches_total_raw : {r['n_raw_mismatch']} mismatches, "
          f"max abs diff {r['max_abs_raw_diff']:,.1f}")
    if r["raw_mismatch_months"]:
        print(f"    {r['raw_mismatch_months']}")
    print(f"  vs bunches_total (cleaned): {r['n_imputed_scoreable']}"
          f"/{r['n_scoreable']} scoreable months are imputed, "
          f"{r['imputed_share_pct']:.1f}% of total volume")
    if r["imputed_months"]:
        print(f"    {r['imputed_months']}")
    print(f"\n  GATE: {'PASS' if r['ok'] else 'FAIL'}"
          f"{'' if r['ok'] else '  -- daily->monthly bridge is broken; do not model'}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
