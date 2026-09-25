"""
Weekly per-block harvest series -- the corpus for Chronos-2 fine-tuning v2
(chronos2_blocks_finetune.py).

Why blocks, and why weekly
--------------------------
At estate-monthly grain the new estates add almost nothing: EC has ~19 months,
EB 8, EA under 2. The width is in the blocks -- EC 292 x ~80 weeks, EB 356 x
~34 weeks -- which is the regime a foundation-model fine-tune needs. Weekly
sits between daily (dominated by the harvest-round rhythm, ~65% of block-days
are zero) and monthly (too few points per block).

Binning
-------
Bins are 7-day windows counted BACKWARDS from an anchor day, so the last bin
always ends exactly on the anchor. For the training corpus the anchor is the
estate's last recorded day; for a walk-forward origin it is the origin's month
end, so the context stops precisely at the information cutoff and never spills
a single day past it. A leading partial bin is dropped rather than scaled.

A block's series starts at its first recorded harvest (leading zeros are "not
yet recorded", not "no fruit"); zeros after that are real non-harvest weeks and
are kept, including trailing ones (replanting, abandonment).

Sources
-------
k3      forecast/epms_production_daily.csv (M/D/YYYY)
EC/EB/EA gis/data/estates/<CODE>/oph.csv, the snapshot gis/build_estate_data.py
         cuts to each estate's recording window. forecast/EC/EC_oph.csv is the
         older, shorter EC export and is deliberately not used here.
"""

import importlib.util
import os

import numpy as np
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_DIR)

SOURCES = {
    "k3": os.path.join(_DIR, "epms_production_daily.csv"),
    "EC": os.path.join(_REPO, "gis", "data", "estates", "EC", "oph.csv"),
    "EB": os.path.join(_REPO, "gis", "data", "estates", "EB", "oph.csv"),
    "EA": os.path.join(_REPO, "gis", "data", "estates", "EA", "oph.csv"),
}


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_daily = _load_by_path("epms_daily_series", os.path.join(_DIR, "build_daily_series.py"))


def load_daily_blocks(estate):
    """Daily bunches per block on a complete calendar, zero-filled.

    Returns a DataFrame indexed by day (first to last harvest date), one column
    per "division/block" key. Block codes repeat across divisions in EPMS, so
    the division is part of the key.
    """
    if estate not in SOURCES:
        raise ValueError(f"unknown estate {estate!r}; known: {sorted(SOURCES)}")
    df = _daily.load_raw(SOURCES[estate])
    df["key"] = (df["division_code"].astype(str).str.strip() + "/"
                 + df["block_code"].astype(str).str.strip())
    wide = df.pivot_table(index="harvest_date", columns="key", values="bunches_total",
                          aggfunc="sum", fill_value=0.0)
    full = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
    wide = wide.reindex(full, fill_value=0.0).astype(float)
    wide.index.name = "date"
    return wide


def weekly_bins(daily_wide, end):
    """Sum days into 7-day bins whose last bin ends on `end` (inclusive).

    Days after `end` are ignored, which is what makes this the leakage cutoff
    for a walk-forward origin. Returns a DataFrame indexed by each bin's last
    day, oldest first.
    """
    end = pd.Timestamp(end).normalize()
    d = daily_wide.loc[:end]
    n = len(d) // 7
    if n == 0:
        return d.iloc[0:0]
    d = d.iloc[len(d) - 7 * n:]
    arr = d.to_numpy().reshape(n, 7, d.shape[1]).sum(axis=1)
    return pd.DataFrame(arr, index=d.index[6::7], columns=d.columns)


def block_series(weekly, min_weeks=1):
    """One array per block, starting at its first non-zero week.

    Blocks with fewer than `min_weeks` weeks from that start (or with no
    harvest at all) are dropped. Returns {key: np.ndarray}.
    """
    out = {}
    vals = weekly.to_numpy()
    for j, key in enumerate(weekly.columns):
        col = vals[:, j]
        nz = np.flatnonzero(col > 0)
        if nz.size == 0:
            continue
        s = col[nz[0]:]
        if s.size >= min_weeks:
            out[key] = s.astype(np.float64)
    return out


def daily_to_months(bin_values, cutoff):
    """Spread forecast bins evenly over their days and total them by month.

    bin_values  (H,) values for the H bins after `cutoff`; bin k covers days
                cutoff + 7(k-1) + 1 ... cutoff + 7k.
    Returns a Series indexed by Period[M]. Only months the bins cover in full
    are meaningful; callers pick the ones they score.
    """
    cutoff = pd.Timestamp(cutoff).normalize()
    h = len(bin_values)
    days = pd.date_range(cutoff + pd.Timedelta(days=1), periods=7 * h, freq="D")
    per_day = np.repeat(np.asarray(bin_values, dtype=float) / 7.0, 7)
    return pd.Series(per_day, index=days).groupby(days.to_period("M")).sum()


if __name__ == "__main__":
    for est in ("k3", "EC", "EB", "EA"):
        daily = load_daily_blocks(est)
        wk = weekly_bins(daily, daily.index.max())
        s = block_series(wk)
        lens = np.array([len(v) for v in s.values()])
        print(f"{est}: {daily.index.min().date()} -> {daily.index.max().date()}, "
              f"{wk.shape[0]} weekly bins, {len(s)} blocks, weeks/block "
              f"min {lens.min()} median {int(np.median(lens))} max {lens.max()}")
