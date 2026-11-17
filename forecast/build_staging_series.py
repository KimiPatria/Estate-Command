"""
Harvest series from the EPMS staging database, for the forecast pages.

SINCE 2026-11-12 this is a thin wrapper: the extract lives in
forecast/adapters/staging_sqlserver.py (the queries below, unchanged), the gate
and the monthly file in forecast/contract.py, and the build in
forecast/build_series.py (buildplan_forecast_mlops.md Phase 3). This command
still works and runs `build_series.py --source staging`; monthly() and gate()
are re-exported for older callers. The notes below describe the data and
still hold.

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

import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from contract import COMPLETE_SHARE, gate, monthly   # noqa: E402,F401  (re-exported)
import build_series                                  # noqa: E402
import estate_config                                 # noqa: E402

OUT_ROOT = os.path.join(_DIR, "staging")
ESTATES = tuple(estate_config.estates("staging"))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Wrapper for forecast/build_series.py --source staging")
    ap.add_argument("--estates", nargs="+", default=None)
    args = ap.parse_args(argv)
    return build_series.main(["--source", "staging"] + (["--estates", *args.estates] if args.estates else []))


if __name__ == "__main__":
    sys.exit(main())
