"""
Harvest series from the EPMS staging database, for the forecast pages.

The forecast pages never query a database: they read files built offline. This
is the build step for the staging source. Per estate it writes, under
forecast/staging/<ESTATE>/ (gitignored, company production data):

  oph.csv       daily bunches per block: harvest_date, division_code,
                block_code, bunches_total. The same shape as
                gis/data/estates/<CODE>/oph.csv, so block_weekly.py and the
                Forecast Lab read it unchanged.
  monthly.csv   month, bunches_total, n_harvest_days, days_covered,
                days_in_month, is_partial
  meta.json     recording window, row counts, what was excluded, gate result

What a bunch count is here
--------------------------
OPH is the harvest ticket table: one row per harvester per harvest point per
day. ZMASAK is the ripe-bunch count and the only count the table carries (no
unripe/overripe split, no weight), so `bunches_total` = SUM(ZMASAK). Tonnage
lives in FDN and is not used.

Rows left out
-------------
STATE = 'D'. Measured 2026-11-10: 13 rows / 214 bunches across EA, EB and EC,
against 2.57 million rows marked 'N'. 'D' is read as "deleted"; the count is
too small to move any series either way. SAP_FLAG (1/2) and METHD (M/0) are
posting and method codes, not validity flags, and are kept. There are no
duplicate ticket ids, no negative or null counts, and every OPH block has a
current BLK row (all checked on that date; the gate below re-checks the ones
that would corrupt a total).

The gate
--------
The block-level file is built through a join to BLK (for the division), and a
join is where a total gets silently multiplied. So the estate's daily total is
queried a second time with no join at all, and the block file must add up to
it, day for day, and the monthly file must add up to both. A failed gate
writes nothing.

Against the live EC database (gis/data/estates/EC/oph.csv), staging EC agreed
to the bunch in 15 of the 18 full months they share. The differences: one day,
2025-04-02, where live holds 164,822 bunches and staging 88,303 (April -3.9%);
February 2025 +1.1%; January 2025 -0.1%.

Usage (dashboard env -- it needs the SQL Server driver and DATABASE_URL_STAGING):
    python forecast/build_staging_series.py
    python forecast/build_staging_series.py --estates EC
"""

import argparse
import json
import os
import sys
from datetime import datetime

import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_DIR)
OUT_ROOT = os.path.join(_DIR, "staging")

# Estates built by default. The plasma schemes (P1, P2) are in the database but
# left out for now.
ESTATES = ("EA", "EB", "EC")
COMPLETE_SHARE = 0.9          # as forecast/lab/registry.py: a month counts when >= 90% recorded

_BLOCK_SQL = """
SELECT o.BUDAT AS harvest_date, b.DIVNR AS division_code, o.BLOCK AS block_code,
       SUM(CAST(o.ZMASAK AS decimal(18, 2))) AS bunches_total
FROM OPH o
LEFT JOIN BLK b ON b.ESTNR = RIGHT(o.WERKS, 2) AND b.BLOCK = o.BLOCK AND b.KDATE = '9999-12-31'
WHERE o.WERKS = :werks AND (o.STATE IS NULL OR o.STATE <> 'D')
GROUP BY o.BUDAT, b.DIVNR, o.BLOCK
"""

_DAILY_SQL = """
SELECT BUDAT AS harvest_date, SUM(CAST(ZMASAK AS decimal(18, 2))) AS bunches_total, COUNT(*) AS tickets
FROM OPH
WHERE WERKS = :werks AND (STATE IS NULL OR STATE <> 'D')
GROUP BY BUDAT
"""

_EXCLUDED_SQL = """
SELECT COUNT(*) AS n, COALESCE(SUM(CAST(ZMASAK AS decimal(18, 2))), 0) AS bunches
FROM OPH WHERE WERKS = :werks AND STATE = 'D'
"""


def _engine():
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    import data_sources as ds
    src = ds.get(ds.STAGING)
    if not src.available:
        raise SystemExit("staging database is not configured (DATABASE_URL_STAGING in .env)")
    return src.engine


def fetch(conn, estate):
    """(block-level daily frame, estate daily frame, excluded-row counts) for one estate."""
    from sqlalchemy import text
    werks = "00" + estate
    blocks = pd.read_sql(text(_BLOCK_SQL), conn, params={"werks": werks})
    daily = pd.read_sql(text(_DAILY_SQL), conn, params={"werks": werks})
    excl = conn.execute(text(_EXCLUDED_SQL), {"werks": werks}).one()
    for df in (blocks, daily):
        df["harvest_date"] = pd.to_datetime(df["harvest_date"])
        df["bunches_total"] = df["bunches_total"].astype(float)
    blocks["division_code"] = blocks["division_code"].astype("string").str.strip()
    blocks["block_code"] = blocks["block_code"].astype("string").str.strip()
    return blocks, daily, {"rows": int(excl[0]), "bunches": float(excl[1])}


def monthly(daily):
    """Monthly totals with how much of each month the recording window covers.

    `daily` is the estate's daily frame. A month is partial when the window
    (first to last recorded day) covers under COMPLETE_SHARE of its days --
    the registry's rule, so the page and the lab agree on which months count.
    """
    s = daily.set_index("harvest_date")["bunches_total"].sort_index()
    first, last = s.index.min(), s.index.max()
    rows = []
    for p in pd.period_range(first.to_period("M"), last.to_period("M"), freq="M"):
        lo, hi = max(p.start_time, first), min(p.end_time.normalize(), last)
        covered = (hi - lo).days + 1
        part = s.loc[lo:hi]
        rows.append({"month": str(p), "bunches_total": float(part.sum()),
                     "n_harvest_days": int((part > 0).sum()),
                     "days_covered": covered, "days_in_month": p.days_in_month,
                     "is_partial": int(covered / p.days_in_month < COMPLETE_SHARE)})
    return pd.DataFrame(rows)


def gate(blocks, daily, months):
    """The block file, the daily total and the monthly file must be one set of numbers.

    Returns {"ok", "checks": [{"name", "ok", "detail"}]}.
    """
    tol = 0.5                     # counts are whole bunches
    checks = []

    by_day = blocks.groupby("harvest_date")["bunches_total"].sum()
    ref = daily.set_index("harvest_date")["bunches_total"]
    diff = by_day.reindex(ref.index).fillna(0.0) - ref
    bad = diff[diff.abs() > tol]
    checks.append({"name": "block file adds up to the estate total, day for day",
                   "ok": bad.empty and len(by_day) == len(ref),
                   "detail": (f"{len(ref)} days, max difference {float(diff.abs().max()):,.1f}"
                              if bad.empty else
                              f"{len(bad)} days differ, first {bad.index[0].date()} by {float(bad.iloc[0]):,.0f}")})

    m_ref = ref.groupby(ref.index.to_period("M")).sum()
    m_got = months.set_index(pd.PeriodIndex(months["month"], freq="M"))["bunches_total"]
    m_diff = (m_got - m_ref).abs()
    checks.append({"name": "monthly file adds up to the daily totals",
                   "ok": bool((m_diff <= tol).all()) and len(m_got) == len(m_ref),
                   "detail": f"{len(m_got)} months, max difference {float(m_diff.max()):,.1f}"})

    no_div = blocks[blocks["division_code"].isna() | (blocks["division_code"] == "")]
    checks.append({"name": "every block has a division",
                   "ok": no_div.empty,
                   "detail": ("all blocks matched a current BLK row" if no_div.empty else
                              f"{no_div['block_code'].nunique()} blocks without one: "
                              + ", ".join(sorted(no_div['block_code'].dropna().unique())[:8]))})

    keys = blocks[["division_code", "block_code"]].drop_duplicates()
    twice = keys[keys.duplicated("block_code", keep=False)]
    checks.append({"name": "no block sits in two divisions",
                   "ok": twice.empty,
                   "detail": ("each block code maps to one division" if twice.empty else
                              f"{twice['block_code'].nunique()} block codes under more than one division")})

    neg = blocks[blocks["bunches_total"] < 0]
    checks.append({"name": "no negative counts", "ok": neg.empty,
                   "detail": "none" if neg.empty else f"{len(neg)} negative block-days"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def build(conn, estate):
    """Fetch, gate and (if the gate passes) write one estate. Returns its meta dict."""
    blocks, daily, excluded = fetch(conn, estate)
    if daily.empty:
        raise SystemExit(f"[{estate}] no OPH rows for WERKS 00{estate}")
    months = monthly(daily)
    result = gate(blocks, daily, months)
    first, last = daily["harvest_date"].min(), daily["harvest_date"].max()
    full = months[months["is_partial"] == 0]
    meta = {
        "estate": estate, "source": "staging", "table": "OPH", "measure": "SUM(ZMASAK), ripe bunches",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "first_day": str(first.date()), "last_day": str(last.date()),
        "calendar_days": int((last - first).days + 1), "harvest_days": int(len(daily)),
        "tickets": int(daily["tickets"].sum()), "bunches": float(daily["bunches_total"].sum()),
        "blocks": int(blocks[["division_code", "block_code"]].drop_duplicates().shape[0]),
        "months": int(len(months)), "full_months": int(len(full)),
        "first_full_month": str(full["month"].iloc[0]) if len(full) else None,
        "last_full_month": str(full["month"].iloc[-1]) if len(full) else None,
        "excluded": {"rule": "STATE = 'D'", **excluded},
        "gate": result,
    }
    if not result["ok"]:
        return meta

    out = os.path.join(OUT_ROOT, estate)
    os.makedirs(out, exist_ok=True)
    blocks = blocks.sort_values(["harvest_date", "division_code", "block_code"])
    blocks["harvest_date"] = blocks["harvest_date"].dt.strftime("%Y-%m-%d")
    blocks["bunches_total"] = blocks["bunches_total"].round().astype("int64")
    blocks.to_csv(os.path.join(out, "oph.csv"), index=False)
    months = months.copy()
    months["bunches_total"] = months["bunches_total"].round().astype("int64")
    months.to_csv(os.path.join(out, "monthly.csv"), index=False)
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--estates", nargs="+", default=list(ESTATES),
                    help="estate codes as in WERKS without the '00' prefix (default: EA EB EC)")
    args = ap.parse_args()

    failed = []
    with _engine().connect() as conn:
        for estate in [e.upper() for e in args.estates]:
            meta = build(conn, estate)
            print(f"[{estate}] {meta['first_day']} -> {meta['last_day']} | {meta['harvest_days']} harvest days "
                  f"| {meta['blocks']} blocks | {meta['bunches']:,.0f} bunches "
                  f"| full months {meta['first_full_month']} .. {meta['last_full_month']} ({meta['full_months']})")
            print(f"      excluded {meta['excluded']['rows']} rows / {meta['excluded']['bunches']:,.0f} bunches "
                  f"({meta['excluded']['rule']})")
            for c in meta["gate"]["checks"]:
                print(f"      {'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
            if meta["gate"]["ok"]:
                print(f"      wrote {os.path.join(OUT_ROOT, estate)}")
            else:
                print("      GATE FAILED -- nothing written for this estate")
                failed.append(estate)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
