"""
Synthetic estates in both source shapes, for CI (buildplan_forecast_mlops.md Phase 7.1).

No company figures: two made-up estates, SA and SB, with a seasonal harvest on a
handful of blocks, written as

  live/<CODE>.csv        a per-record export in the live shape (grade columns,
                         M/D/YYYY dates) -- read by the live_k3_file adapter
  staging/<CODE>/        a file drop in the staging shape: oph.csv, daily.csv and
                         columns.json carrying the staging adapter's own registry
                         (ZMASAK, ZBRONDOLAN, EMPNR, ...) -- read by file_drop
  estates.yaml           both sources pointing at them, series written under out/

The data ends yesterday, so freshness checks behave the same on any day.
`inject` puts known problems in, so the monitors can be shown to fire:
  {"empty_column": ("SA", "ZBRONDOLAN"), "volume_drop": ("SB", 0.5)}

    python forecast/fixtures/make_synthetic.py <out_dir>
"""

import json
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
_FORECAST = os.path.dirname(_DIR)
if _FORECAST not in sys.path:
    sys.path.insert(0, _FORECAST)

ESTATES = {"SA": {"blocks": 8, "base": 120.0}, "SB": {"blocks": 12, "base": 90.0}}
MONTHS = 30
GRADES = ["bunches_ripe", "bunches_overripe", "bunches_underripe", "bunches_unripe",
          "bunches_wet", "bunches_rotten", "bunches_long_stalk", "bunches_empty",
          "bunches_dirty", "bunches_unfresh", "bunches_old", "bunches_pest_damaged_old",
          "bunches_pest_damaged_new", "bunches_diseased"]


def _records(code, spec, end, rng):
    start = (pd.Timestamp(end) - pd.DateOffset(months=MONTHS)).normalize() + pd.Timedelta(days=1)
    rows = []
    for day in pd.date_range(start, end, freq="D"):
        if day.day_of_week == 6:                       # no harvest on Sundays
            continue
        season = 1 + 0.45 * np.sin(2 * np.pi * (day.month - 3) / 12)
        for b in range(spec["blocks"]):
            if (day.day_of_year + b) % 3:              # each block harvested every third day
                continue
            div = 1 + b // 4
            for rec in range(2):                     # two harvest records per block-day
                total = max(0, int(rng.normal(spec["base"] * season / 2, 6)))
                rows.append({"harvest_date": day, "division_code": div, "block_code": f"{b + 1:03d}",
                             "bunches_total": total, "loose_fruits": int(total * 1.4),
                             "harvester": f"H{(b * 7 + rec) % 25:03d}", "tph": f"T{b:02d}{rec}",
                             "foreman": f"M{div}", "checker": f"K{div}"})
    return pd.DataFrame(rows)


def _live_export(df, path):
    out = df[["harvest_date", "division_code", "block_code", "bunches_total", "loose_fruits"]].copy()
    out["harvest_date"] = out["harvest_date"].map(lambda d: f"{d.month}/{d.day}/{d.year}")
    for g in GRADES:
        out[g] = 0
    out["bunches_ripe"] = out["bunches_total"]
    out.loc[out.index % 50 == 0, "bunches_overripe"] = 1          # sparse, like the real files
    out.to_csv(path, index=False)


def _staging_drop(df, folder):
    """The files the staging adapter would have written, built from ticket rows."""
    from adapters.base import Adapter
    from adapters.staging_sqlserver import REGISTRY
    os.makedirs(folder, exist_ok=True)
    t = df.rename(columns={"bunches_total": "ZMASAK", "loose_fruits": "ZBRONDOLAN", "harvester": "EMPNR",
                           "tph": "TPH", "foreman": "EMPNR_M", "checker": "EMPNR_K"})
    t["OPH"] = [f"OPH{i:07d}" for i in range(len(t))]
    t["METHD"] = "M"
    t["CARR_1"] = ""
    t["IS_RAIN"] = 0
    t["RAIN_DURT"] = 0
    t = t.rename(columns={"harvest_date": "BUDAT"})
    stats = Adapter.frame_stats(t, [c for c in REGISTRY if c["source_name"] != "WB_QTY_TOTAL"], month_col="BUDAT")
    stats["WB_QTY_TOTAL"] = {"rows": int(len(t)), "filled": int(len(t) // 40), "sum": None,
                             "first_month": None, "last_month": None,
                             "note": "share of harvest tickets on a weighed delivery note"}
    g = t.groupby(["BUDAT", "division_code", "block_code"])
    blocks = g.agg(bunches_total=("ZMASAK", "sum"), loose_fruits=("ZBRONDOLAN", "sum"),
                   tickets=("OPH", "count"), harvesters=("EMPNR", "nunique"),
                   harvest_points=("TPH", "nunique")).reset_index().rename(columns={"BUDAT": "harvest_date"})
    blocks["harvest_date"] = blocks["harvest_date"].dt.strftime("%Y-%m-%d")
    blocks.to_csv(os.path.join(folder, "oph.csv"), index=False)
    daily = t.groupby("BUDAT").agg(bunches_total=("ZMASAK", "sum"), tickets=("OPH", "count")).reset_index()
    daily = daily.rename(columns={"BUDAT": "harvest_date"})
    daily["harvest_date"] = daily["harvest_date"].dt.strftime("%Y-%m-%d")
    daily.to_csv(os.path.join(folder, "daily.csv"), index=False)
    with open(os.path.join(folder, "columns.json"), "w", encoding="utf-8") as fh:
        json.dump({"registry": REGISTRY, "stats": stats}, fh, indent=1)


def make(out_dir, inject=None, seed=7):
    inject = inject or {}
    out_dir = os.path.abspath(out_dir)
    os.makedirs(os.path.join(out_dir, "live"), exist_ok=True)
    end = date.today() - timedelta(days=1)
    rng = np.random.default_rng(seed)
    for code, spec in ESTATES.items():
        df = _records(code, spec, end, rng)
        _live_export(df, os.path.join(out_dir, "live", f"{code}.csv"))
        st = df.copy()
        if inject.get("volume_drop", (None,))[0] == code:
            last = st["harvest_date"].max().to_period("M") - 1         # the newest full month
            sel = st["harvest_date"].dt.to_period("M") == last
            st.loc[sel, "bunches_total"] = (st.loc[sel, "bunches_total"] * inject["volume_drop"][1]).astype(int)
        if inject.get("empty_column", (None,))[0] == code and inject["empty_column"][1] == "ZBRONDOLAN":
            st["loose_fruits"] = 0
        _staging_drop(st, os.path.join(out_dir, "staging", code))
    cfg = {
        "contract_version": 1,
        "status_thresholds": {"populated": 0.20, "sparse": 0.001},
        "sources": {
            "live": {"enabled": True, "label": "Live estates", "description": "synthetic live exports",
                     "series_dir": "unused", "estates": {
                         code: {"lab_id": code.lower(), "label": code, "adapter": "live_k3_file",
                                "code": code, "path": os.path.join(out_dir, "live", f"{code}.csv"),
                                "region": "syn"} for code in ESTATES}},
            "staging": {"enabled": True, "label": "EPMS staging", "description": "synthetic staging drops",
                        "series_dir": "unused", "estates": {
                            code: {"lab_id": f"stg_{code.lower()}", "label": code, "adapter": "file_drop",
                                   "code": code, "path": os.path.join(out_dir, "staging", "{code}"),
                                   "region": "syn"} for code in ESTATES}},
        },
    }
    import yaml
    path = os.path.join(out_dir, "estates.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    return path


if __name__ == "__main__":
    print(make(sys.argv[1] if len(sys.argv) > 1 else "synthetic"))
