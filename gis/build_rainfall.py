"""Multi-year rainfall history over the estate, from Open-Meteo. REAL feed.

Run once:  python gis/build_rainfall.py
Writes:    gis/data/ec_rainfall.json

Why this exists
---------------
Oil palm sets its bunch load a long way ahead: inflorescence sex determination
happens 20-24 months before a bunch is cut, and abortion 8-10 months before.
Any yield model worth the name reads rainfall at those lags. The estate export
carries no weather at all.

It does not have to. Open-Meteo's historical archive is free, needs no key, and
reaches back decades, and gis/fire.py already uses the same service's forecast
endpoint for wind and humidity. So this is a REAL feed that costs the client
nothing and that we can supply ourselves, exactly like the Sentinel-2 canopy
layer and the Copernicus terrain layer.

That matters for the pitch. The lagged forecast needs three things: harvest
history, rainfall, and fertiliser actuals. This module removes rainfall from
the ask, which sharpens the request rather than diluting it - the client is
then being asked for two things, both of which they already hold.

One grid cell, and why that is honest here
------------------------------------------
The reanalysis grid is coarser than the estate, so this is one rainfall series
for all 291 blocks, not a per-block surface. Pretending otherwise would invent
spatial detail the model does not have. Stated in `method.spatial`.

What it computes beyond the raw series
--------------------------------------
Monthly totals, rain days, and the longest dry spell in each month. Dry spell
matters more than the total for palm: 200 mm falling in three days is not the
same growing month as 200 mm spread over twenty, and the stress that aborts
bunches is consecutive days without rain.
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

# Run directly as the docstring says, as well as via -m.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis import ontology

log = logging.getLogger("estate-command.build-rainfall")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OUT_DIR = Path(__file__).parent / "data"

# Six years. The lag structure the model looks for is 24 months deep, and it
# needs several years of target on top of that to be identifiable at all.
YEARS_BACK = 6

# The archive trails real time by a few days, so asking for today returns
# nulls at the tail. Back off rather than write them.
ARCHIVE_LAG_DAYS = 6

# A day with less than this is not a rain day for a dry-spell count. 1 mm is
# the conventional threshold and is what gis/fire.py already uses.
RAIN_DAY_MM = 1.0


def _centroid(features) -> tuple[float, float]:
    """Estate centroid, as the mean of every block's vertices."""
    xs, ys = [], []
    for f in features:
        for ring in f["geometry"]["coordinates"]:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def fetch(lat: float, lon: float, start: str, end: str) -> dict:
    """Daily rainfall and temperature over the window. Raises on failure."""
    r = httpx.get(ARCHIVE_URL, params={
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": ("precipitation_sum,temperature_2m_max,temperature_2m_min,"
                  "et0_fao_evapotranspiration"),
        "timezone": "Asia/Jakarta",
    }, timeout=180)
    r.raise_for_status()
    return r.json()


def _monthly(days: list[str], rain: list, et0: list) -> list[dict]:
    """Monthly totals, rain days, and the longest dry spell within each month.

    The dry spell is computed inside the calendar month rather than across the
    boundary. A spell straddling month end is therefore split, which
    understates it; the alternative attributes one drought to two months and
    double-counts it in any model that reads the column.
    """
    by_month: dict[str, dict] = defaultdict(
        lambda: {"mm": 0.0, "days": 0, "rain_days": 0, "et0": 0.0,
                 "dry_run": 0, "dry_max": 0, "missing": 0})
    for d, mm, e in zip(days, rain, et0):
        m = by_month[d[:7]]
        m["days"] += 1
        if mm is None:
            m["missing"] += 1
            continue
        m["mm"] += mm
        if e is not None:
            m["et0"] += e
        if mm >= RAIN_DAY_MM:
            m["rain_days"] += 1
            m["dry_run"] = 0
        else:
            m["dry_run"] += 1
            m["dry_max"] = max(m["dry_max"], m["dry_run"])

    out = []
    for month in sorted(by_month):
        v = by_month[month]
        out.append({
            "month": month,
            "rain_mm": round(v["mm"], 1),
            "rain_days": v["rain_days"],
            "days": v["days"],
            "longest_dry_spell_days": v["dry_max"],
            "et0_mm": round(v["et0"], 1) if v["et0"] else None,
            # Water balance: rainfall minus reference evapotranspiration. A
            # negative month is one where the stand drew down soil moisture.
            "water_balance_mm": (round(v["mm"] - v["et0"], 1) if v["et0"] else None),
            "incomplete": bool(v["missing"]),
        })
    return out


def build(estate: str = "EC", years: int = YEARS_BACK) -> dict:
    blocks = ontology.blocks_geojson(estate)
    if blocks is None:
        raise SystemExit(f"No block geometry for estate {estate!r}.")
    lon, lat = _centroid(blocks["features"])

    end = date.today() - timedelta(days=ARCHIVE_LAG_DAYS)
    start = date(end.year - years, 1, 1)
    log.info("[rainfall] %s at %.4f, %.4f: %s to %s",
             estate, lat, lon, start, end)

    j = fetch(lat, lon, start.isoformat(), end.isoformat())
    daily = j.get("daily") or {}
    days = daily.get("time") or []
    rain = daily.get("precipitation_sum") or []
    et0 = daily.get("et0_fao_evapotranspiration") or [None] * len(days)
    if not days:
        raise SystemExit("Open-Meteo returned no daily series.")

    months = _monthly(days, rain, et0)
    # A trailing partial month would read as a drought in any lag feature that
    # divides by it, so it is dropped rather than carried.
    if months and months[-1]["days"] < 26:
        log.info("[rainfall] dropping partial month %s (%d days)",
                 months[-1]["month"], months[-1]["days"])
        months = months[:-1]

    totals = [m["rain_mm"] for m in months]
    wettest = max(months, key=lambda m: m["rain_mm"])
    driest = min(months, key=lambda m: m["rain_mm"])
    longest = max(months, key=lambda m: m["longest_dry_spell_days"])

    # Mean by calendar month, which is the seasonal shape a forecast leans on.
    by_cal: dict[int, list] = defaultdict(list)
    for m in months:
        by_cal[int(m["month"][5:])].append(m["rain_mm"])
    seasonal = [{"month_of_year": k,
                 "mean_rain_mm": round(sum(v) / len(v), 1),
                 "years": len(v)}
                for k, v in sorted(by_cal.items())]

    doc = {
        "estate": estate.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": "real:open-meteo",
        "source": {
            "service": "Open-Meteo historical archive",
            "url": ARCHIVE_URL,
            "licence": "Free for non-commercial and commercial use, no key.",
            "point": {"lat": round(lat, 5), "lon": round(lon, 5)},
            "variables": ["precipitation_sum", "temperature_2m_max",
                          "temperature_2m_min", "et0_fao_evapotranspiration"],
        },
        "method": {
            "spatial": ("One series for the whole estate. The reanalysis grid "
                        "cell is larger than EC, so a per-block rainfall "
                        "surface would be invented detail."),
            "rain_day_mm": RAIN_DAY_MM,
            "dry_spell": ("Longest run of consecutive days under the rain-day "
                          "threshold, computed within each calendar month."),
            "note": ("The client supplies nothing for this layer. Rainfall is "
                     "removed from the data request as a result."),
        },
        "window": {"from": months[0]["month"], "to": months[-1]["month"],
                   "months": len(months), "days": len(days)},
        "summary": {
            "annual_mean_mm": round(sum(totals) / len(totals) * 12, 0),
            "monthly_mean_mm": round(sum(totals) / len(totals), 1),
            "wettest_month": {"month": wettest["month"], "rain_mm": wettest["rain_mm"]},
            "driest_month": {"month": driest["month"], "rain_mm": driest["rain_mm"]},
            "longest_dry_spell": {"month": longest["month"],
                                  "days": longest["longest_dry_spell_days"]},
        },
        "seasonal": seasonal,
        "months": months,
        # The daily series itself, kept because the operations ledger needs
        # rainfall ON THE DAY a crew was sent out, not the month's total. A
        # wet Tuesday is what turns a planned 58 ha into an actual 41, and
        # that correlation only exists in the generated ledger if the day
        # figure driving it is real. Nulls are the archive's own gaps.
        "daily": [{"date": d, "rain_mm": (round(mm, 1) if mm is not None else None)}
                  for d, mm in zip(days, rain)],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{estate.lower()}_rainfall.json"
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    log.info("[rainfall] %s: %d months -> %s", estate, len(months), path)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--estate", default="EC")
    ap.add_argument("--years", type=int, default=YEARS_BACK)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        doc = build(a.estate, a.years)
    except (httpx.HTTPError, SystemExit) as e:
        log.error("[rainfall] %s", e)
        return 1
    s, w = doc["summary"], doc["window"]
    print(f"\n  {w['months']} months, {w['from']} to {w['to']}")
    print(f"  annual mean {s['annual_mean_mm']:.0f} mm")
    print(f"  wettest {s['wettest_month']['month']} at {s['wettest_month']['rain_mm']} mm")
    print(f"  driest  {s['driest_month']['month']} at {s['driest_month']['rain_mm']} mm")
    print(f"  longest dry spell {s['longest_dry_spell']['days']} days "
          f"in {s['longest_dry_spell']['month']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
