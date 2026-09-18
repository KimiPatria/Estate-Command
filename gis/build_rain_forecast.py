"""Pull what the weather forecast SAID, the evening before, for every day.

The plan has always been told the rain that fell. At six in the morning
nobody has that figure; what a mandor has is last night's forecast. This
build fetches exactly that, so the rain model in gis/models/rain.py can learn
how far last night's forecast is to be trusted on this estate, and so a plan
replayed for a past day decides on what was knowable, not on hindsight.

The source
----------
Open-Meteo's Previous Runs API keeps the forecast issued one and two days
before each hour (`precipitation_previous_day1`, `..._day2`). Free, no key,
real. Measured on 2026-09-14: complete from 2024-02-01.

Days are summed in Asia/Jakarta time, NOT the estate's own Asia/Jayapura,
because the recorded rainfall in ec_rainfall.json is summed that way and a
forecast compared against a differently-cut day would look worse than it is.

The truth it is compared with is the Open-Meteo archive (ERA5 reanalysis),
itself a model. The estate's own rain-gauge book is the real ground truth and
is named as the extract that would replace it.

    python gis/build_rain_forecast.py
"""

import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("estate-command.build_rain_forecast")

URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
OUT = Path(__file__).parent / "data" / "ec_rain_forecast.json"
POINT = {"lat": -7.02361, "lon": 140.86947}
FIRST_DAY = date(2024, 2, 1)
TZ = "Asia/Jakarta"
# The previous-runs archive trails real time by a day or two.
LAG_DAYS = 3


def _chunk(start: date, end: date) -> dict:
    r = httpx.get(URL, params={
        "latitude": POINT["lat"], "longitude": POINT["lon"],
        "hourly": "precipitation_previous_day1,precipitation_previous_day2",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "timezone": TZ,
    }, timeout=180)
    r.raise_for_status()
    return r.json()


def build() -> dict:
    end = date.today() - timedelta(days=LAG_DAYS)
    sums: dict = defaultdict(lambda: {"day1": 0.0, "day2": 0.0, "n1": 0, "n2": 0})
    grid = None
    s = FIRST_DAY
    while s <= end:
        e = min(end, s + timedelta(days=180))
        j = _chunk(s, e)
        grid = grid or {"lat": j.get("latitude"), "lon": j.get("longitude")}
        h = j.get("hourly") or {}
        for t, v1, v2 in zip(h.get("time") or [],
                             h.get("precipitation_previous_day1") or [],
                             h.get("precipitation_previous_day2") or []):
            d = sums[t[:10]]
            if v1 is not None:
                d["day1"] += v1
                d["n1"] += 1
            if v2 is not None:
                d["day2"] += v2
                d["n2"] += 1
        log.info("[rain_forecast] %s to %s", s, e)
        s = e + timedelta(days=1)

    days = []
    for k in sorted(sums):
        d = sums[k]
        # A day with most of its hours missing is not a forecast of that day.
        days.append({
            "date": k,
            "day1_mm": round(d["day1"], 1) if d["n1"] >= 20 else None,
            "day2_mm": round(d["day2"], 1) if d["n2"] >= 20 else None,
        })
    return {
        "estate": "EC",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": "real:open-meteo",
        "source": {
            "service": "Open-Meteo Previous Runs API",
            "url": URL,
            "licence": "Free for non-commercial and commercial use, no key.",
            "point": POINT, "grid_point": grid,
            "variables": ["precipitation_previous_day1", "precipitation_previous_day2"],
            "timezone": TZ,
        },
        "note": ("day1_mm is the rain forecast for that day by the run issued the day "
                 "before; day2_mm by the run two days before. Summed over Asia/Jakarta "
                 "days to match the recorded archive."),
        "window": {"from": days[0]["date"] if days else None,
                   "to": days[-1]["date"] if days else None, "days": len(days)},
        "days": days,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = build()
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {OUT.name}: {out['window']['days']} days, "
          f"{out['window']['from']} to {out['window']['to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
