"""Estate Command ontology - estates, divisions, blocks, and their metrics.

Pure file reads. No database access and no model, so this imports cheaply and
serves fast, exactly like forecast/blocks.py. The database-derived artefact
(estate footprints) is precomputed by gis/build_footprints.py.

Provenance is a first-class property on every feature, because the whole
pitch depends on the client being able to tell what is theirs from what we
invented:

    real:arcgis     EC block polygons, exported from the client's own GIS
    real:gps-hull   K3 / BA estate outlines, hulled from harvester GPS
    real:epms       metrics computed from recorded harvest
    synthetic       anything the source systems do not record

Estates with no shapefile return None for their blocks rather than a
fabricated tessellation, so the UI renders an honest "no geometry yet" state.
That gap is the client ask, and instrumenting it is the point (UC-15).
"""

import csv
import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from threading import Lock

log = logging.getLogger("estate-command.ontology")

csv.field_size_limit(10 ** 9)

_DIR = Path(__file__).parent
_FORECAST_DIR = _DIR.parent / "forecast"
_FOOTPRINTS = _DIR / "data" / "estate_footprints.geojson"

_CACHE: dict = {}
_LOCK = Lock()

# Estates whose block geometry came from the client's ArcGIS export. Keyed by
# the estate code used on the map; the value is the forecast/ subdirectory the
# overlay and harvest CSVs live in.
_SHAPEFILE_ESTATES = {"EC": "EC"}

# Palm age is quoted against this year rather than date.today() so a card
# rendered in a demo reads the same next January.
_REFERENCE_YEAR = 2026


def _key(division, block) -> str:
    """Join key for EPMS block identity.

    The harvest export zero-pads block codes to three digits ('075') while the
    ArcGIS overlay does not ('75'). Divisions 4-7 matched by accident because
    their block numbers were already three digits. Normalising both sides to
    integers takes the join from 195/291 to 291/291.
    """
    return f"{int(str(division).strip())}|{int(str(block).strip())}"


# -- geometry helpers -------------------------------------------------------

def _ring_area_ha(ring):
    """Shoelace area of a lon/lat ring, in hectares."""
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = 111320.0
    xy = [(p[0] * k * math.cos(math.radians(lat0)), p[1] * k) for p in ring]
    a = 0.0
    for i in range(len(xy) - 1):
        a += xy[i][0] * xy[i + 1][1] - xy[i + 1][0] * xy[i][1]
    return abs(a) / 2.0 / 10_000.0


def _convex_hull(points):
    """Andrew monotone chain. Returns a closed lon/lat ring."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return None

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                ox, oy = out[-2]
                bx, by = out[-1]
                if (bx - ox) * (p[1] - oy) - (by - oy) * (p[0] - ox) > 0:
                    break
                out.pop()
            out.append(p)
        return out

    lower = half(pts)
    upper = half(list(reversed(pts)))
    ring = [list(p) for p in lower[:-1] + upper[:-1]]
    ring.append(list(ring[0]))
    return ring


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# -- EC: real polygons from the client's ArcGIS export ----------------------

def _load_overlay(code: str):
    """Block polygons keyed by _key(). Returns {} when the estate has none."""
    path = _FORECAST_DIR / code / f"{code}_overlay.csv"
    if not path.exists():
        return {}
    out = {}
    skipped = 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                ring = json.loads(row["overlay_coordinates"])
                props = json.loads(row.get("overlay_properties") or "{}")
            except (KeyError, TypeError, json.JSONDecodeError):
                skipped += 1
                continue
            if not isinstance(props, dict) or not isinstance(ring, list) or len(ring) < 4:
                skipped += 1
                continue
            out[_key(row["overlay_division_code"], row["overlay_block_code"])] = {
                "ring": ring,
                "props": props,
                "division_code": str(row["overlay_division_code"]).strip(),
                "block_code": str(row["overlay_block_code"]).strip(),
                "section_code": (row.get("overlay_section_code") or "").strip(),
            }
    if skipped:
        log.warning("[ontology] %s: skipped %d unparseable overlay rows", code, skipped)
    return out


# Grading columns summed into a single deduction count. This is the T0 proxy
# UC-04 leans on: it detects that something is wrong with a block's fruit,
# never what. Nothing here identifies a pest or a pathogen.
_DEDUCTION_COLS = (
    "bunches_unripe", "bunches_underripe", "bunches_overripe", "bunches_rotten",
    "bunches_empty", "bunches_dirty", "bunches_unfresh", "bunches_old",
    "bunches_diseased", "bunches_pest_damaged_old", "bunches_pest_damaged_new",
)


def _load_harvest(code: str):
    """Per-block harvest aggregates from the EPMS OPH export."""
    path = _FORECAST_DIR / code / f"{code}_oph.csv"
    if not path.exists():
        return {}, None

    agg = defaultdict(lambda: {"bunches": 0, "ripe": 0, "deducted": 0,
                               "loose": 0, "loose_records": 0,
                               "days": set(), "records": 0,
                               "by_month": defaultdict(int),
                               "loose_by_month": defaultdict(int),
                               "days_by_month": defaultdict(set)})
    lo = hi = None
    months = set()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            # EPMS spells this column 'divison_code' in the export. Kept as-is.
            k = _key(row["divison_code"], row["block_code"])
            d = row["harvest_date"]
            if lo is None or d < lo:
                lo = d
            if hi is None or d > hi:
                hi = d
            month = d[:7]
            months.add(month)
            bunches = _i(row.get("bunches_total")) or 0
            a = agg[k]
            a["records"] += 1
            a["days"].add(d)
            a["bunches"] += bunches
            a["ripe"] += _i(row.get("bunches_ripe")) or 0
            a["deducted"] += sum(_i(row.get(c)) or 0 for c in _DEDUCTION_COLS)
            # Loose fruit is the one harvesting loss the export actually
            # counts: detached fruitlets picked up at the palm, per record.
            # A zero may mean none collected or none written down, so the
            # number of records carrying a count travels with the total.
            loose = _i(row.get("loose_fruits")) or 0
            a["loose"] += loose
            if loose:
                a["loose_records"] += 1
            a["by_month"][month] += bunches
            a["loose_by_month"][month] += loose
            a["days_by_month"][month].add(d)

    out = {
        k: {
            "bunches": v["bunches"],
            "ripe": v["ripe"],
            "deducted": v["deducted"],
            "harvest_days": len(v["days"]),
            "records": v["records"],
            "by_month": dict(v["by_month"]),
            "harvest_days_by_month": {m: len(s) for m, s in v["days_by_month"].items()},
            "loose_fruits": v["loose"],
            "loose_records": v["loose_records"],
            "loose_by_month": dict(v["loose_by_month"]),
        }
        for k, v in agg.items()
    }
    return out, {"from": lo, "to": hi, "months": sorted(months)}


def _load_block_forecast(code: str):
    path = _FORECAST_DIR / code / "block_forecast.csv"
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out[_key(row["division_code"], row["block_code"])] = {
                    "forecast_bunches": int(float(row["forecast_bunches"])),
                    "n_months_used": int(float(row["n_months_used"])),
                    "confidence": row.get("confidence"),
                }
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _sort_key(k: str):
    div, blk = k.split("|")
    return (int(div), int(blk))


def _build_estate(code: str, subdir: str) -> dict:
    overlay = _load_overlay(subdir)
    harvest, window = _load_harvest(subdir)
    fcast = _load_block_forecast(subdir)

    features = []
    divisions = defaultdict(list)
    matched = 0

    for k in sorted(overlay, key=_sort_key):
        blk = overlay[k]
        p = blk["props"]
        # Tanam is net planted hectares; Kerangka is the surveyed frame area.
        planted_ha = _f(p.get("Tanam")) or _f(p.get("Kerangka"))
        planted_year = _i(p.get("TT"))
        h = harvest.get(k)
        if h:
            matched += 1
        f = fcast.get(k)
        bunches = h["bunches"] if h else None

        features.append({
            "type": "Feature",
            "id": f"block:{code}:{k}",
            "geometry": {"type": "Polygon", "coordinates": [blk["ring"]]},
            "properties": {
                "entity": "block",
                "estate_code": code,
                "division_code": blk["division_code"],
                "block_code": blk["block_code"],
                "block_label": p.get("Blok") or blk["section_code"],
                "block_sap": p.get("BlokSAP"),
                "provenance": "real:arcgis",
                "planted_year": planted_year,
                "palm_age_years": (_REFERENCE_YEAR - planted_year) if planted_year else None,
                "seed_variety": p.get("JnsBibit"),
                "ownership": p.get("Ownership"),
                "planted_ha": planted_ha,
                "palms": _i(p.get("JlhPokok")),
                "sph": _i(p.get("SPH")),
                "polygon_area_ha": round(_ring_area_ha(blk["ring"]), 2),
                "bunches_total": bunches,
                "harvest_days": h["harvest_days"] if h else None,
                "bunches_per_ha": (round(bunches / planted_ha, 1)
                                   if bunches and planted_ha else None),
                "ripe_rate": (round(h["ripe"] / bunches, 4) if h and bunches else None),
                "deduction_rate": (round(h["deducted"] / bunches, 4)
                                   if h and bunches else None),
                "metrics_provenance": "real:epms" if h else None,
                # Monthly series drives the time scrubber. Five months on this
                # export, so it rides inline rather than costing a second fetch.
                "bunches_by_month": h["by_month"] if h else {},
                "harvest_days_by_month": h["harvest_days_by_month"] if h else {},
                # Loose fruit, real: the export counts it on every record.
                "loose_fruits": h["loose_fruits"] if h else None,
                "loose_per_bunch": (round(h["loose_fruits"] / bunches, 3)
                                    if h and bunches else None),
                "loose_by_month": h["loose_by_month"] if h else {},
                "harvest_records": h["records"] if h else None,
                "loose_records": h["loose_records"] if h else None,
                "forecast_bunches": f["forecast_bunches"] if f else None,
                "forecast_confidence": f["confidence"] if f else None,
            },
        })
        divisions[blk["division_code"]].append(blk["ring"])

    log.info("[ontology] %s: %d polygons, %d with harvest, %d with forecast",
             code, len(features), matched, len(fcast))

    return {
        "blocks": {"type": "FeatureCollection", "features": features},
        "divisions": _dissolve_divisions(code, divisions),
        "harvest_window": window,
        "matched": matched,
    }


def _dissolve_divisions(code: str, rings_by_division) -> dict:
    """Division outlines as the convex hull of their blocks' vertices.

    A hull, not a true dissolve: divisions are contiguous here so the hull is
    a fair outline, and it avoids a polygon-union dependency for a layer that
    exists to be a zoom target.
    """
    feats = []
    for div in sorted(rings_by_division, key=int):
        rings = rings_by_division[div]
        pts = [(round(x, 7), round(y, 7)) for r in rings for x, y in r]
        hull = _convex_hull(pts)
        if not hull:
            continue
        feats.append({
            "type": "Feature",
            "id": f"division:{code}:{div}",
            "geometry": {"type": "Polygon", "coordinates": [hull]},
            "properties": {
                "entity": "division",
                "estate_code": code,
                "division_code": div,
                "provenance": "real:arcgis",
                "blocks": len(rings),
                "hull_area_ha": round(_ring_area_ha(hull), 1),
            },
        })
    return {"type": "FeatureCollection", "features": feats}


# -- public API -------------------------------------------------------------

def _state() -> dict:
    with _LOCK:
        if "state" in _CACHE:
            return _CACHE["state"]

        estates = {code: _build_estate(code, subdir)
                   for code, subdir in _SHAPEFILE_ESTATES.items()}

        footprints = {"type": "FeatureCollection", "features": []}
        if _FOOTPRINTS.exists():
            footprints = json.loads(_FOOTPRINTS.read_text(encoding="utf-8"))
        else:
            log.warning("[ontology] %s missing - run gis/build_footprints.py",
                        _FOOTPRINTS)

        _CACHE["state"] = {"estates": estates, "footprints": footprints}
        return _CACHE["state"]


def reload_ontology() -> None:
    """Drop the cache so edited CSVs are picked up without a restart."""
    with _LOCK:
        _CACHE.clear()


def estate_index() -> list[dict]:
    """One row per estate on the map, saying exactly what it has."""
    st = _state()
    out = []
    for code, e in st["estates"].items():
        blocks = e["blocks"]["features"]
        ha = sum(b["properties"]["planted_ha"] or 0 for b in blocks)
        out.append({
            "estate_code": code,
            "label": f"Estate {code}",
            "geometry_provenance": "real:arcgis",
            "has_block_geometry": True,
            "blocks": len(blocks),
            "divisions": len(e["divisions"]["features"]),
            "planted_ha": round(ha, 1),
            "blocks_with_harvest": e["matched"],
            "harvest_window": e["harvest_window"],
            "caveat": None,
        })
    for f in st["footprints"]["features"]:
        p = f["properties"]
        out.append({
            "estate_code": p["estate_code"],
            "label": f"Estate {p['estate_code']}",
            "geometry_provenance": p["provenance"],
            "has_block_geometry": False,
            "blocks": 0,
            "divisions": p.get("divisions"),
            "planted_ha": p.get("hull_area_ha"),
            "blocks_with_harvest": 0,
            "harvest_window": None,
            "caveat": p.get("caveat"),
        })
    return sorted(out, key=lambda r: r["estate_code"])


def estates_geojson() -> dict:
    """Estate outlines: ArcGIS-derived where blocks exist, GPS hulls otherwise."""
    st = _state()
    feats = list(st["footprints"]["features"])
    for code, e in st["estates"].items():
        pts = [(round(x, 7), round(y, 7))
               for f in e["blocks"]["features"]
               for x, y in f["geometry"]["coordinates"][0]]
        hull = _convex_hull(pts)
        if not hull:
            continue
        feats.append({
            "type": "Feature",
            "id": f"estate:{code}",
            "geometry": {"type": "Polygon", "coordinates": [hull]},
            "properties": {
                "entity": "estate",
                "estate_code": code,
                "provenance": "real:arcgis",
                "source": f"{code}_overlay.csv block polygons, dissolved",
                "blocks": len(e["blocks"]["features"]),
                "divisions": len(e["divisions"]["features"]),
                "hull_area_ha": round(_ring_area_ha(hull), 1),
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def blocks_geojson(estate: str):
    """Block polygons for one estate, or None when it has no shapefile."""
    e = _state()["estates"].get((estate or "").upper())
    return e["blocks"] if e else None


# Metrics the map can colour by. Deliberately all bunch-count based: the EC
# export carries no t_abw, and inventing a bunch weight to quote tonnage
# would produce a number the client's agronomists would reject on sight.
METRICS = {
    "bunches_per_ha": "Bunches per hectare",
    "bunches_total": "Bunches harvested",
    "peer_index": "Yield vs age-matched peers",
    "palm_age_years": "Palm age",
    "deduction_rate": "Grading deduction rate",
    "forecast_bunches": "Forecast bunches (next month)",
}


def block_metrics(estate: str, metric: str = "bunches_per_ha",
                  month: str | None = None) -> dict | None:
    """Per-block values for one metric, optionally restricted to one month.

    peer_index is the block's bunches per hectare over the median of the
    blocks planted the same year. Above 1.0 is outperforming its cohort. It is
    the honest T0 form of UC-03: it says a block is behind its peers, never
    why, and it controls for age only because age is the one covariate this
    export actually carries.
    """
    e = _state()["estates"].get((estate or "").upper())
    if e is None:
        return None
    if metric not in METRICS:
        return None

    feats = e["blocks"]["features"]
    raw: dict[str, float | None] = {}

    def yield_per_ha(p):
        ha = p.get("planted_ha")
        if not ha:
            return None
        if month:
            b = (p.get("bunches_by_month") or {}).get(month)
        else:
            b = p.get("bunches_total")
        return round(b / ha, 1) if b else None

    if metric == "peer_index":
        by_cohort: dict[int, list] = defaultdict(list)
        for f in feats:
            p = f["properties"]
            v = yield_per_ha(p)
            if v and p.get("planted_year"):
                by_cohort[p["planted_year"]].append(v)
        medians = {}
        for year, vals in by_cohort.items():
            vals = sorted(vals)
            n = len(vals)
            medians[year] = (vals[n // 2] if n % 2
                             else (vals[n // 2 - 1] + vals[n // 2]) / 2)
        for f in feats:
            p = f["properties"]
            v = yield_per_ha(p)
            med = medians.get(p.get("planted_year"))
            raw[f["id"]] = round(v / med, 3) if v and med else None
    elif metric == "bunches_per_ha":
        for f in feats:
            raw[f["id"]] = yield_per_ha(f["properties"])
    elif metric == "bunches_total":
        for f in feats:
            p = f["properties"]
            raw[f["id"]] = ((p.get("bunches_by_month") or {}).get(month)
                            if month else p.get("bunches_total"))
    else:
        for f in feats:
            raw[f["id"]] = f["properties"].get(metric)

    vals = sorted(v for v in raw.values() if v is not None)
    return {
        "estate": estate.upper(),
        "metric": metric,
        "label": METRICS[metric],
        "month": month,
        "months": (e["harvest_window"] or {}).get("months", []),
        "provenance": "real:epms" if metric != "palm_age_years" else "real:arcgis",
        "values": raw,
        "domain": [vals[0], vals[-1]] if vals else None,
        "median": (vals[len(vals) // 2] if vals else None),
        "n": len(vals),
    }


def divisions_geojson(estate: str):
    e = _state()["estates"].get((estate or "").upper())
    return e["divisions"] if e else None
