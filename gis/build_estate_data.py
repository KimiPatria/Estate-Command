"""Snapshot the map's estates out of their EPMS databases.

Run once per data refresh:  python gis/build_estate_data.py
Writes, per estate:         gis/data/estates/<CODE>/overlay.csv
                            gis/data/estates/<CODE>/oph.csv
                            gis/data/estates/<CODE>/block_forecast.csv
                            gis/data/estates/<CODE>/manifest.json

Each estate is one restored EPMS database (DATABASE_URL_EC / _EA / _EB). The
map never queries them live: gis/ontology.py reads these files, which keeps the
page deterministic, fast, and up when Postgres is not.

The two CSVs keep the column shapes of the EC export the map was built on
(forecast/EC/EC_overlay.csv, forecast/EC/EC_oph.csv), 'divison_code' typo
included, so every existing reader takes them unchanged.

What is kept and what is dropped
--------------------------------
Block polygons come from m_overlay, filtered to the estate's own code: every
EPMS database carries the whole company's overlay, and the copies are not all
equally fresh (EB's own database has the 2026-02-19 revision, EA's has not).

Harvest comes from t_oph, minus rows flagged oph_is_deleted, restricted to the
estate's recording window. The window is the run of months that each hold at
least WINDOW_SHARE of the busiest month's records. The databases carry records
past their real window (EA has 209 records spread over 2026, EB a four-day
February 2026 tail of 10,947 plus a trickle into April), and a month holding a
few days of cards would render on the time scrubber as a collapse in yield.
What each estate drops is written to its manifest, never silently.

The harvest date is oph_created_date, as in the EC export: the card is written
the day the fruit is cut.
"""

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

log = logging.getLogger("estate-command.estate-data")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent / "data" / "estates"

# Map estate code -> the env var naming its database. Order is display order.
ESTATES = {"EC": "DATABASE_URL_EC", "EA": "DATABASE_URL_EA", "EB": "DATABASE_URL_EB"}

# A month belongs to the recording window when it holds at least this share of
# the estate's busiest month's records.
WINDOW_SHARE = 0.25

OVERLAY_COLS = ["overlay_estate_code", "overlay_division_code", "overlay_block_code",
                "overlay_section_code", "overlay_coordinates", "overlay_properties"]

# Output name -> t_oph column. The EPMS export spells division 'divison_code'.
OPH_COLS = {
    "harvest_date": "oph_created_date",
    "divison_code": "oph_division_code",
    "block_code": "oph_block_code",
    "bunches_total": "bunches_total",
    "bunches_ripe": "bunches_ripe",
    "bunches_overripe": "bunches_overripe",
    "bunches_underripe": "bunches_underripe",
    "bunches_unripe": "bunches_unripe",
    "bunches_wet": "bunches_wet",
    "bunches_rotten": "bunches_rotten",
    "bunches_long_stalk": "bunches_long_stalk",
    "bunches_empty": "bunches_empty",
    "bunches_dirty": "bunches_dirty",
    "bunches_unfresh": "bunches_unfresh",
    "bunches_old": "bunches_old",
    "bunches_pest_damaged_old": "bunches_pest_damaged_old",
    "bunches_pest_damaged_new": "bunches_pest_damaged_new",
    "bunches_diseased": "bunches_diseased",
    "loose_fruits": "loose_fruits",
}

OVERLAY_SQL = text(
    """
    select overlay_estate_code, overlay_division_code, overlay_block_code,
           overlay_section_code, overlay_coordinates, overlay_properties
    from m_overlay
    where overlay_estate_code = :estate and overlay_type = 'BLOK'
    order by overlay_division_code, overlay_block_code
    """
)

MONTHS_SQL = text(
    """
    select to_char(oph_created_date, 'YYYY-MM') as month, count(*) as n,
           coalesce(sum(bunches_total), 0) as bunches
    from t_oph
    where oph_estate_code = :estate and coalesce(oph_is_deleted, 0) = 0
      and oph_created_date is not null
    group by 1 order by 1
    """
)

OPH_SQL = text(
    "select " + ", ".join(f"{src} as {dst}" for dst, src in OPH_COLS.items()) + """
    from t_oph
    where oph_estate_code = :estate and coalesce(oph_is_deleted, 0) = 0
      and oph_created_date between :lo and :hi
    order by oph_created_date, oph_division_code, oph_block_code, oph_id
    """
)

DELETED_SQL = text(
    "select count(*) from t_oph where oph_estate_code = :estate and oph_is_deleted = 1"
)

LAST_DAY_SQL = text(
    """
    select min(oph_created_date), max(oph_created_date) from t_oph
    where oph_estate_code = :estate and coalesce(oph_is_deleted, 0) = 0
      and to_char(oph_created_date, 'YYYY-MM') between :m0 and :m1
    """
)


def recording_window(months: list[tuple[str, int, int]]) -> tuple[str, str]:
    """First and last month of the longest contiguous run of busy months."""
    peak = max(n for _, n, _ in months)
    busy = [m for m, n, _ in months if n >= WINDOW_SHARE * peak]

    def nxt(m):
        y, mo = int(m[:4]), int(m[5:])
        return f"{y + mo // 12}-{mo % 12 + 1:02d}"

    runs, cur = [], [busy[0]]
    for m in busy[1:]:
        if m == nxt(cur[-1]):
            cur.append(m)
        else:
            runs.append(cur)
            cur = [m]
    runs.append(cur)
    best = max(runs, key=len)
    return best[0], best[-1]


def _write_csv(path: Path, header: list[str], rows) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_NONNUMERIC)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
            n += 1
    return n


def build_estate(code: str, url: str) -> dict:
    out = OUT_DIR / code
    out.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"options": "-c default_transaction_read_only=on"})
    with engine.connect() as conn:
        overlay = conn.execute(OVERLAY_SQL, {"estate": code}).fetchall()
        months = [(r.month, r.n, int(r.bunches))
                  for r in conn.execute(MONTHS_SQL, {"estate": code})]
        deleted = conn.execute(DELETED_SQL, {"estate": code}).scalar()
        if not overlay or not months:
            raise SystemExit(f"{code}: no overlay or no harvest in {url.rsplit('/', 1)[-1]}")

        m0, m1 = recording_window(months)
        lo, hi = conn.execute(LAST_DAY_SQL, {"estate": code, "m0": m0, "m1": m1}).one()
        oph = conn.execute(OPH_SQL, {"estate": code, "lo": lo, "hi": hi}).fetchall()
        db_name = conn.execute(text("select current_database()")).scalar()

    n_overlay = _write_csv(out / "overlay.csv", OVERLAY_COLS, overlay)
    n_oph = _write_csv(out / "oph.csv", list(OPH_COLS),
                       ([r[0].isoformat()] + list(r[1:]) for r in oph))

    # Per-block next-month forecast: the existing trailing-mean builder, run
    # on this snapshot. Imported by path because forecast/ is not a package.
    sys.path.insert(0, str(ROOT / "forecast"))
    import build_block_forecast
    fc = build_block_forecast.build(str(out / "oph.csv"), str(out / "overlay.csv"))
    fc.to_csv(out / "block_forecast.csv", index=False)

    dropped = [{"month": m, "records": n, "bunches": b}
               for m, n, b in months if not (m0 <= m <= m1)]
    # Harvest zero-pads block codes ('075'), the overlay does not ('75').
    blocks_harvested = len({(int(r[1]), int(r[2])) for r in oph})
    manifest = {
        "estate_code": code,
        "database": db_name,
        "built": date.today().isoformat(),
        "blocks": n_overlay,
        "blocks_with_harvest": blocks_harvested,
        "harvest_records": n_oph,
        "window": {"from": lo.isoformat(), "to": hi.isoformat()},
        "window_rule": (f"contiguous months each holding >= {WINDOW_SHARE:.0%} "
                        f"of the busiest month's records"),
        "excluded_deleted_records": deleted,
        "excluded_outside_window": dropped,
        "block_forecasts": len(fc),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--estate", action="append", choices=list(ESTATES),
                    help="Build only this estate (repeatable). Default: all.")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    for code in args.estate or ESTATES:
        url = os.getenv(ESTATES[code])
        if not url:
            log.error("%s is not set, skipping %s", ESTATES[code], code)
            continue
        m = build_estate(code, url)
        dropped = sum(d["records"] for d in m["excluded_outside_window"])
        print(f"  {code}  {m['database']:10s} {m['blocks']:4d} blocks "
              f"({m['blocks_with_harvest']} harvested)  {m['harvest_records']:7d} records  "
              f"{m['window']['from']} .. {m['window']['to']}  "
              f"dropped {dropped} outside window, {m['excluded_deleted_records']} deleted")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
