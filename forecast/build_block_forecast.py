"""
Block-level FFB forecast — populates the Block Map "Forecast" overlay and the
Block drill-down table in forecast_static/forecast.html (both containers have
existed since the Head Office overhaul, permanently empty: "no per-block model
exists yet").

Deliberately NOT the estate-level LightGBM+SARIMAX+conformal pipeline
(monthly_model.py) run 291 times. monthly_model.py's own docstring already
retired an earlier block-level LightGBM panel as infeasible on far more
estate-level history than any single block gets; per block here every block
has the exact same handful of monthly points as the whole estate did in
train_ec.py (see forecast/build_estate_features.py) — walk-forward
backtesting, SARIMAX and gradient-boosted trees need far more than that to
mean anything. Point forecast is a trailing mean of each block's own recorded
non-partial months (the same "Trailing3" concept already used estate-wide as
the SARIMAX fallback) — simple, honest about what a ~4-point series can
support, and cheap enough to run for every block.

"vs LY" and "Yield (t/ha)" are left for the caller/UI to render as N/A: no
prior-year block data exists yet, and yield needs an average-bunch-weight
conversion this codebase doesn't have anywhere (same reason estate-level FFB
tonnage is N/A). Don't fabricate either here.

Usage:
  python build_block_forecast.py --raw forecast/EC/EC_oph.csv \
      --overlay forecast/EC/EC_overlay.csv --out forecast/EC/block_forecast.csv
"""

import argparse
import csv
import json

import numpy as np
import pandas as pd

TRAILING_WINDOW = 3


def _load_raw(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    if "divison_code" in df.columns and "division_code" not in df.columns:
        df = df.rename(columns={"divison_code": "division_code"})
    df["harvest_date"] = pd.to_datetime(df["harvest_date"])
    return df


def _partial_months(raw: pd.DataFrame) -> set:
    """Calendar months at the start/end of the estate's raw file whose date
    range doesn't span the full month — the same rule build_estate_features.py
    uses at estate level (a per-block gap wouldn't count; this is a
    whole-file recording-window boundary, so it's computed once, estate-wide,
    not per block)."""
    months = raw["harvest_date"].dt.to_period("M")
    if months.empty:
        return set()
    first, last = months.min(), months.max()
    partial = set()
    if raw["harvest_date"].min() > first.start_time:
        partial.add(first)
    if raw["harvest_date"].max() < last.end_time.normalize():
        partial.add(last)
    return partial


def _planted_area_by_block(overlay_path: str) -> dict:
    """(division_code, block_code) -> planted area in ha, from the overlay's
    'Tanam' property (net planted area; 'Kerangka' is the gross framework
    area, 'Shape_Area' the raw polygon area used elsewhere for map popups —
    'Tanam' is the closest match to the drill-down table's own column name)."""
    area = {}
    with open(overlay_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                props = json.loads(row.get("overlay_properties") or "{}")
                key = (str(row["overlay_division_code"]), str(row["overlay_block_code"]))
                if "Tanam" in props:
                    area[key] = float(props["Tanam"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return area


def build(raw_path: str, overlay_path: str) -> pd.DataFrame:
    raw = _load_raw(raw_path)
    raw["month"] = raw["harvest_date"].dt.to_period("M")
    partial = _partial_months(raw)

    monthly = (raw.groupby(["division_code", "block_code", "month"])["bunches_total"]
               .sum().reset_index())
    usable = monthly[~monthly["month"].isin(partial)].sort_values("month")

    area_by_block = _planted_area_by_block(overlay_path)

    rows = []
    for (div, blk), g in usable.groupby(["division_code", "block_code"]):
        vals = g["bunches_total"].to_numpy(dtype=float)
        tail = vals[-TRAILING_WINDOW:]
        mean = float(tail.mean())
        cv = float(tail.std() / mean) if mean and len(tail) > 1 else None
        # Never claim better than "Moderate" — every block here has at most a
        # handful of points, nowhere near enough for a "High" confidence read.
        confidence = "Moderate" if (cv is not None and cv <= 0.15) else "Low"
        rows.append({
            "division_code": str(div),
            "block_code": str(blk),
            "planted_area_ha": area_by_block.get((str(div), str(blk))),
            "forecast_bunches": round(mean),
            "n_months_used": len(tail),
            "cv": round(cv, 3) if cv is not None else None,
            "confidence": confidence,
        })
    # An estate whose every month is partial has nothing to forecast from; say
    # so with an empty table rather than a KeyError on the sort.
    cols = ["division_code", "block_code", "planted_area_ha", "forecast_bunches",
            "n_months_used", "cv", "confidence"]
    return pd.DataFrame(rows, columns=cols).sort_values(["division_code", "block_code"])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--overlay", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df = build(args.raw, args.overlay)
    df.to_csv(args.out, index=False)
    n_area = df["planted_area_ha"].notna().sum()
    print(f"Wrote {len(df)} block(s) -> {args.out} "
          f"({n_area} with a matched planted-area, {len(df) - n_area} without)")
    print(df["confidence"].value_counts().to_string())


if __name__ == "__main__":
    main()
