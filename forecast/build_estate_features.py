"""
Daily -> monthly estate feature builder.

Rebuilds the missing merge_block_monthly.py step referenced (but never committed) in
monthly_model.py: turns a raw EPMS daily-harvest export (the epms_production_daily.csv /
EC_oph.csv shape) into the monthly features_estate_monthly.csv shape monthly_model.py
actually trains on.

Scope: this implements the *simple* path, not the full seasonal-imputation algorithm
described in monthly_model.py's docstring (fully-recorded / under-recorded / sparse
months). It assumes every month has >=20 recorded harvest-days except possibly a partial
month at the very start or end of the file (the only case seen in EC's synthetic data,
and the same convention K3's own first row uses). If a future estate's raw export has a
genuinely under-recorded interior month (10-19 harvest-days), extend `_flag_partial`
accordingly rather than assuming this handles it.

Estate-agnostic: no estate id is hardcoded.

Usage:
  python build_estate_features.py --raw forecast/EC/EC_oph.csv \
      --weather forecast/EC/weather_nasa_power_history.csv \
      --out forecast/EC/features_estate_monthly.csv
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monthly_model as mm

RAW_QUALITY_COLS = [
    "bunches_ripe", "bunches_overripe", "bunches_underripe", "bunches_unripe",
    "bunches_wet", "bunches_rotten", "bunches_long_stalk", "bunches_empty",
    "bunches_dirty", "bunches_unfresh", "bunches_old", "bunches_pest_damaged_old",
    "bunches_pest_damaged_new", "bunches_diseased", "loose_fruits",
]
WORKDONE_COLS = ["prune_md_lag1m", "prune_qty_lag5m", "fert_qty_lag3m"]


def _load_raw(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    # EC_oph.csv has a header typo ("divison_code"); tolerate either spelling.
    if "divison_code" in df.columns and "division_code" not in df.columns:
        df = df.rename(columns={"divison_code": "division_code"})
    df["harvest_date"] = pd.to_datetime(df["harvest_date"])
    for c in ["bunches_total"] + RAW_QUALITY_COLS:
        if c not in df.columns:
            df[c] = 0.0
    return df


def _flag_partial(months: pd.PeriodIndex, min_date_by_month: dict, max_date_by_month: dict) -> pd.Series:
    """Partial = a month at the start or end of the file whose recorded date range
    doesn't span the full calendar month. Interior months are never flagged here."""
    flags = pd.Series(0, index=months)
    if len(months) == 0:
        return flags
    first, last = months.min(), months.max()
    if min_date_by_month[first] > first.start_time:
        flags.loc[first] = 1
    if max_date_by_month[last] < last.end_time.normalize():
        flags.loc[last] = 1
    return flags


def build(raw_path: str, weather_path: str) -> pd.DataFrame:
    raw = _load_raw(raw_path)
    raw["month"] = raw["harvest_date"].dt.to_period("M")

    agg = {c: "sum" for c in ["bunches_total"] + RAW_QUALITY_COLS}
    monthly = raw.groupby("month").agg(agg)
    monthly["n_harvest_days"] = raw.groupby("month")["harvest_date"].nunique()

    min_date = raw.groupby("month")["harvest_date"].min().to_dict()
    max_date = raw.groupby("month")["harvest_date"].max().to_dict()
    monthly["is_partial"] = _flag_partial(monthly.index, min_date, max_date)
    monthly["exclude_from_model"] = monthly["is_partial"]
    monthly["is_underrecorded"] = 0  # not exercised for this estate's data (see docstring)
    monthly["completeness_scale"] = (
        monthly["n_harvest_days"] / monthly.index.days_in_month
    ).clip(upper=1.0).round(3)

    monthly["bunches_total_raw"] = monthly["bunches_total"]
    monthly["quality_ratio_ripe"] = (
        monthly["bunches_ripe"] / monthly["bunches_total"].replace(0, np.nan)
    ).fillna(0.0)
    monthly["reject_ratio"] = 1 - monthly["quality_ratio_ripe"]

    monthly = monthly.sort_index()
    y = monthly["bunches_total"]
    monthly["y_lag1"] = y.shift(1)
    monthly["y_lag2"] = y.shift(2)
    monthly["y_lag3"] = y.shift(3)
    monthly["y_lag12"] = y.shift(12)
    monthly["y_roll3_mean"] = y.shift(1).rolling(3).mean()
    monthly["y_roll6_mean"] = y.shift(1).rolling(6).mean()

    wm = mm.build_weather_monthly(path=weather_path)
    monthly = monthly.join(wm, how="left")

    for c in WORKDONE_COLS:
        monthly[c] = np.nan

    month_num = monthly.index.month
    monthly["month_sin"] = np.sin(2 * np.pi * month_num / 12)
    monthly["month_cos"] = np.cos(2 * np.pi * month_num / 12)
    monthly["months_since_start"] = range(len(monthly))
    monthly["month_start"] = monthly.index.start_time
    monthly["month"] = monthly.index.astype(str)

    monthly = monthly.reset_index(drop=True)
    return monthly


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--weather", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df = build(args.raw, args.weather)
    df.to_csv(args.out, index=False)
    print(f"Wrote {len(df)} month(s) -> {args.out}")
    print(df[["month", "n_harvest_days", "is_partial", "exclude_from_model",
              "bunches_total"]].to_string(index=False))


if __name__ == "__main__":
    main()
