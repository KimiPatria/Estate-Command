"""The read side of the two public real feeds: terrain and rainfall.

gis/build_terrain.py and gis/build_rainfall.py do the network work offline and
write gis/data/{estate}_terrain.json and {estate}_rainfall.json. This module
reads them, exactly as gis/vegetation.py reads the Sentinel-2 layer: no
network, no raster libraries, just the files and the questions the map, the
models and the copilot ask of them.

Both feeds share the property that makes the canopy layer valuable. They are
neither the client's export nor a generator - they are real measurements over
the client's own blocks that the client does not currently hold, obtained free
from public sources. Every value here is badged real, and both are struck off
the data request as a result.

Terrain and rainfall live together because they are the same kind of thing and
neither is large enough to justify its own module. Where a block has no DEM
coverage the value is null and nothing is interpolated, on the same principle
the cloud-masked canopy layer follows.
"""

import json
import logging
from pathlib import Path
from threading import Lock

log = logging.getLogger("estate-command.environment")

_DIR = Path(__file__).parent / "data"
_CACHE: dict = {}
_LOCK = Lock()


def _load(estate: str, kind: str) -> dict | None:
    path = _DIR / f"{estate.lower()}_{kind}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("[environment] %s unreadable: %s", path, exc)
        return None


def _state(estate: str, kind: str) -> dict | None:
    key = f"{estate.upper()}:{kind}"
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        data = _load(estate, kind)
        _CACHE[key] = data
        if data:
            log.info("[environment] %s %s loaded", estate.upper(), kind)
        return data


def reload_environment() -> None:
    with _LOCK:
        _CACHE.clear()


# ── terrain ────────────────────────────────────────────────────────────────

def terrain_available(estate: str = "EC") -> bool:
    return _state(estate, "terrain") is not None


def terrain_by_block(estate: str = "EC") -> dict:
    """{division|block: row}. Empty when no DEM has been pulled."""
    st = _state(estate, "terrain")
    return st["blocks"] if st else {}


def terrain_summary(estate: str = "EC") -> dict:
    """The terrain layer and what it means, or why it is not there yet."""
    st = _state(estate, "terrain")
    if not st:
        return {
            "available": False,
            "reason": "No DEM has been pulled for this estate.",
            "command": "python gis/build_terrain.py --estate EC",
            "cost": "Free. Copernicus DEM GLO-30, no client data required.",
        }
    return {
        "available": True,
        "estate": st["estate"],
        "provenance": st["provenance"],
        "source": st["source"],
        "method": st["method"],
        "coverage": st["coverage"],
        "summary": st["summary"],
        "steepest": _extremes(st["blocks"], "slope_deg", lowest=False),
        "roughest": _extremes(st["blocks"], "canopy_roughness_m", lowest=False),
    }


def _extremes(blocks: dict, field: str, lowest: bool, n: int = 8) -> list[dict]:
    rows = [b for b in blocks.values() if b.get(field) is not None]
    rows.sort(key=lambda b: b[field], reverse=not lowest)
    return [{"block_label": b["block_label"], "block_id": b["block_id"],
             field: b[field]} for b in rows[:n]]


# ── rainfall ───────────────────────────────────────────────────────────────

def rainfall_available(estate: str = "EC") -> bool:
    return _state(estate, "rainfall") is not None


def rainfall_months(estate: str = "EC") -> list[dict]:
    """Monthly totals, oldest first. Empty when nothing has been pulled."""
    st = _state(estate, "rainfall")
    return st["months"] if st else []


def rainfall_by_month(estate: str = "EC") -> dict:
    """{YYYY-MM: row}, for joining a lag feature onto a target month."""
    return {m["month"]: m for m in rainfall_months(estate)}


def rainfall_by_day(estate: str = "EC") -> dict:
    """{YYYY-MM-DD: rain_mm}, the daily series behind the monthly totals.

    Empty when the archive was pulled before the daily series was kept, in
    which case the operations generator falls back to spreading each month's
    real total across its real rain-day count and says so in its note.
    """
    st = _state(estate, "rainfall")
    if not st or not st.get("daily"):
        return {}
    return {d["date"]: d["rain_mm"] for d in st["daily"] if d.get("rain_mm") is not None}


def rainfall_summary(estate: str = "EC") -> dict:
    st = _state(estate, "rainfall")
    if not st:
        return {
            "available": False,
            "reason": "No rainfall history has been pulled for this estate.",
            "command": "python gis/build_rainfall.py --estate EC",
            "cost": "Free. Open-Meteo archive, no key, no client data required.",
        }
    return {
        "available": True,
        "estate": st["estate"],
        "provenance": st["provenance"],
        "source": st["source"],
        "method": st["method"],
        "window": st["window"],
        "summary": st["summary"],
        "seasonal": st["seasonal"],
        "months": st["months"],
    }


def lagged_rainfall(month: str, lags: range | list, estate: str = "EC") -> dict:
    """Rainfall at each lag before a target month, as model features.

    The whole reason this feed exists. Oil palm decides its bunch load 20-24
    months out, so a yield model reads rainfall at lag 20-24, not lag 0. Keys
    are `rain_lag_{n}` in millimetres and `dry_lag_{n}` in days, so a feature
    frame can be built straight from the return value.

    A lag that reaches back past the start of the archive is returned as None
    rather than zero, because a model that reads a missing month as a drought
    will learn the wrong thing.
    """
    by_month = rainfall_by_month(estate)
    if not by_month:
        return {}
    y, m = int(month[:4]), int(month[5:7])
    out = {}
    for n in lags:
        total = (y * 12 + m - 1) - n
        key = f"{total // 12:04d}-{total % 12 + 1:02d}"
        row = by_month.get(key)
        out[f"rain_lag_{n}"] = row["rain_mm"] if row else None
        out[f"dry_lag_{n}"] = row["longest_dry_spell_days"] if row else None
    return out


def readiness_evidence(kind: str, estate: str = "EC") -> dict | None:
    """Live evidence for one register row, once its feed has been pulled.

    Written the way gis/vegetation.readiness_evidence is: a row whose layer
    actually runs should say what was measured, not predict that it could be.
    Returns None when the feed is absent, leaving the static row in place.
    """
    st = _state(estate, kind)
    if not st:
        return None

    if kind == "terrain":
        c, s = st["coverage"], st["summary"]
        return {"evidence": (
            f"Measured on {c['measured']} of {c['blocks']} blocks from "
            f"{st['source']['name']} at {st['source']['resolution_m']} m. Mean "
            f"landform slope {s['slope_mean_deg']} degrees, elevation "
            f"{s['elevation_range_m'][0]}-{s['elevation_range_m'][1]} m. The "
            f"raw surface gradient reads {s['surface_slope_mean_deg']} degrees "
            "because the DEM sees canopy, not ground; the landform figure is "
            "the one reported.")}

    w, s = st["window"], st["summary"]
    return {"evidence": (
        f"{w['months']} months pulled, {w['from']} to {w['to']}, averaging "
        f"{s['annual_mean_mm']:.0f} mm a year. Wettest month "
        f"{s['wettest_month']['month']} at {s['wettest_month']['rain_mm']} mm, "
        f"longest dry spell {s['longest_dry_spell']['days']} days in "
        f"{s['longest_dry_spell']['month']}.")}
