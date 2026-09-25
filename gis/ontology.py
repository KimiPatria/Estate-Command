"""Estate Command ontology - estates, divisions, blocks, and their metrics.

Pure file reads. No database access and no model, so this imports cheaply and
serves fast, exactly like forecast/blocks.py. The estates on the map are the
snapshots gis/build_estate_data.py writes out of each estate's EPMS database
into gis/data/estates/<CODE>/: block polygons from m_overlay, harvest from
t_oph. An estate is on the map when its snapshot directory exists.

Provenance is a first-class property on every feature, because the whole
pitch depends on the client being able to tell what is theirs from what we
invented:

    real:arcgis     block polygons, the client's GIS export held in m_overlay
    real:epms       metrics computed from recorded harvest
    synthetic       anything the source systems do not record

Two views of the synthetic estate
---------------------------------
The synthetic feeds (gis/build_synthetic.py) were generated for EC around a
harvest export that ended on build_synthetic.WINDOW_END. EC's database now
runs well past that day. The map shows all of it, but the operations world
built on the synthetic feeds - the ledger, the rates it divides by the
window's length - has to keep seeing the harvest it was generated from, or
its numbers stop meaning anything. So SYNTHETIC_ESTATE is also built a second
time with its harvest cut at WINDOW_END, and callers inside that world ask for
it with synthetic_world=True. Moving WINDOW_END and regenerating the feeds is
the re-anchor; nothing else in here needs to change for it.
"""

import csv
import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from threading import Lock

from gis.build_synthetic import WINDOW_END as SYNTHETIC_WINDOW_END

log = logging.getLogger("estate-command.ontology")

csv.field_size_limit(10 ** 9)

_DIR = Path(__file__).parent
_ESTATES_DIR = _DIR / "data" / "estates"

_CACHE: dict = {}
_LOCK = Lock()

# The one estate the synthetic feeds were generated for.
SYNTHETIC_ESTATE = "EC"

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


# -- block polygons and harvest, from gis/build_estate_data.py snapshots ----

def _estate_codes() -> list[str]:
    """Every estate with a snapshot on disk, in code order."""
    if not _ESTATES_DIR.exists():
        return []
    return sorted(p.name for p in _ESTATES_DIR.iterdir()
                  if (p / "overlay.csv").exists())


def _load_overlay(code: str):
    """Block polygons keyed by _key(). Returns {} when the estate has none."""
    path = _ESTATES_DIR / code / "overlay.csv"
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


def _n(v) -> int:
    """Count cell to int. The snapshot writes bare integers or '' for null, so
    int() is the fast path; _i() covers anything hand-edited."""
    try:
        return int(v) if v else 0
    except ValueError:
        return _i(v) or 0


def _new_agg():
    return defaultdict(lambda: {"bunches": 0, "ripe": 0, "deducted": 0,
                                "loose": 0, "loose_records": 0,
                                "days": set(), "records": 0,
                                "by_month": defaultdict(int),
                                "loose_by_month": defaultdict(int),
                                "days_by_month": defaultdict(set)})


def _load_harvest(code: str, cutoffs: dict[str, str | None]) -> dict:
    """Per-block harvest aggregates, one set per named cutoff.

    `cutoffs` maps a view name to the last ISO date it includes (None: all).
    EC's file is close to a million rows, so every view is folded in the same
    single pass rather than re-reading it per view.
    """
    path = _ESTATES_DIR / code / "oph.csv"
    if not path.exists():
        return {name: ({}, None) for name in cutoffs}

    aggs = {name: _new_agg() for name in cutoffs}
    bounds = {name: [None, None, set()] for name in cutoffs}   # lo, hi, months
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        head = next(rd)
        # EPMS spells this column 'divison_code' in the export. Kept as-is.
        i_date, i_div, i_blk = (head.index(c) for c in
                                ("harvest_date", "divison_code", "block_code"))
        i_tot, i_ripe, i_loose = (head.index(c) for c in
                                  ("bunches_total", "bunches_ripe", "loose_fruits"))
        i_ded = [head.index(c) for c in _DEDUCTION_COLS]
        keys: dict = {}   # a few hundred blocks over ~a million rows
        for row in rd:
            raw = (row[i_div], row[i_blk])
            k = keys.get(raw)
            if k is None:
                k = keys[raw] = _key(*raw)
            d = row[i_date]
            month = d[:7]
            bunches = _n(row[i_tot])
            ripe = _n(row[i_ripe])
            deducted = sum(_n(row[i]) for i in i_ded)
            # Loose fruit is the one harvesting loss the export actually
            # counts: detached fruitlets picked up at the palm, per record.
            # A zero may mean none collected or none written down, so the
            # number of records carrying a count travels with the total.
            loose = _n(row[i_loose])
            for name, through in cutoffs.items():
                if through is not None and d > through:
                    continue
                b = bounds[name]
                if b[0] is None or d < b[0]:
                    b[0] = d
                if b[1] is None or d > b[1]:
                    b[1] = d
                b[2].add(month)
                a = aggs[name][k]
                a["records"] += 1
                a["days"].add(d)
                a["bunches"] += bunches
                a["ripe"] += ripe
                a["deducted"] += deducted
                a["loose"] += loose
                if loose:
                    a["loose_records"] += 1
                a["by_month"][month] += bunches
                a["loose_by_month"][month] += loose
                a["days_by_month"][month].add(d)

    out = {}
    for name, agg in aggs.items():
        lo, hi, months = bounds[name]
        out[name] = ({
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
        }, {"from": lo, "to": hi, "months": sorted(months)} if lo else None)
    return out


def _load_block_forecast(code: str):
    path = _ESTATES_DIR / code / "block_forecast.csv"
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


def _build_estate(code: str, overlay: dict, harvest: dict, window: dict | None,
                  fcast: dict) -> dict:
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
                # Monthly series drives the time scrubber. Under two years per
                # estate, so it rides inline rather than costing a second fetch.
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

def _manifest(code: str) -> dict:
    path = _ESTATES_DIR / code / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _state() -> dict:
    with _LOCK:
        if "state" in _CACHE:
            return _CACHE["state"]

        estates, synthetic = {}, {}
        for code in _estate_codes():
            overlay = _load_overlay(code)
            fcast = _load_block_forecast(code)
            cutoffs = {"full": None}
            if code == SYNTHETIC_ESTATE:
                cutoffs["synthetic"] = SYNTHETIC_WINDOW_END.isoformat()
            views = _load_harvest(code, cutoffs)
            estates[code] = _build_estate(code, overlay, *views["full"], fcast)
            if "synthetic" in views:
                synthetic[code] = _build_estate(code, overlay, *views["synthetic"], fcast)
            estates[code]["manifest"] = _manifest(code)
        if not estates:
            log.warning("[ontology] no estate snapshots in %s - run "
                        "gis/build_estate_data.py", _ESTATES_DIR)

        _CACHE["state"] = {"estates": estates, "synthetic": synthetic}
        return _CACHE["state"]


def _estate(code: str | None, synthetic_world: bool = False) -> dict | None:
    code = (code or "").upper()
    st = _state()
    if synthetic_world and code in st["synthetic"]:
        return st["synthetic"][code]
    return st["estates"].get(code)


def reload_ontology() -> None:
    """Drop the cache so edited CSVs are picked up without a restart."""
    with _LOCK:
        _CACHE.clear()


def estate_index(synthetic_world: bool = False) -> list[dict]:
    """One row per estate on the map, saying exactly what it has.

    synthetic_world=True gives the synthetic estate's row as the synthetic
    feeds see it, harvest window cut at SYNTHETIC_WINDOW_END.
    """
    out = []
    for code in _state()["estates"]:
        e = _estate(code, synthetic_world)
        blocks = e["blocks"]["features"]
        ha = sum(b["properties"]["planted_ha"] or 0 for b in blocks)
        man = _estate(code).get("manifest") or {}
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
            "source_database": man.get("database"),
            "snapshot_built": man.get("built"),
            "has_synthetic_feeds": code == SYNTHETIC_ESTATE,
            "caveat": None,
        })
    return out


def estates_geojson() -> dict:
    """Estate outlines, dissolved from each estate's block polygons."""
    st = _state()
    feats = []
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
                "source": f"m_overlay block polygons ({(e.get('manifest') or {}).get('database', code)}), dissolved",
                "blocks": len(e["blocks"]["features"]),
                "divisions": len(e["divisions"]["features"]),
                "hull_area_ha": round(_ring_area_ha(hull), 1),
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def blocks_geojson(estate: str, synthetic_world: bool = False):
    """Block polygons for one estate, or None when it has none.

    synthetic_world=True returns the synthetic estate's blocks with harvest
    aggregated only up to SYNTHETIC_WINDOW_END - the harvest the synthetic
    feeds were generated from. Any other estate is the same either way.
    """
    e = _estate(estate, synthetic_world)
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
    e = _estate(estate)
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
    e = _estate(estate)
    return e["divisions"] if e else None
