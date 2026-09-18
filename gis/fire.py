"""UC-06 - fire and force majeure threat assessment for Estate Command.

What is real here and what is not
---------------------------------
REAL, fetched live:
  * hotspots      NASA FIRMS VIIRS SNPP NRT, the same feed and key the
                  forecast intelligence layer already uses
  * wind, humidity, recent rainfall   Open-Meteo, already in the stack
  * block geometry, planted area, palm counts   the client's ArcGIS export

SYNTHETIC, invented because the source systems record none of it:
  * fire posts, watch towers, reservoirs, canals   (gis/build_fire_assets.py)
  * crew-on-shift numbers and vehicle response speeds

The value of this layer is not detection - NASA does the detecting and gives
it away. It is triage: which of *these* blocks are downwind of *that* fire,
how long until it reaches them, what is standing in its path, and who is on
shift to move.

On the spread model
-------------------
This is a screening model, not a validated fire-behaviour model. Rate of
spread is estimated from wind speed and a dryness index derived from recent
rainfall and humidity. It carries no fuel load, no slope, no fuel moisture
and no suppression response, which are exactly the terms a real fire
behaviour model is built from. Treat the arrival times as an ordering of
which blocks to worry about first, never as a countdown clock. Every payload
this module returns says so in its `model` block, and the UI prints it.
"""

import csv
import io
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("estate-command.fire")

_DIR = Path(__file__).parent
_ASSETS = _DIR / "data" / "ec_fire_assets.geojson"

_HTTP_TIMEOUT = 20.0
_TTL = 30 * 60          # external feeds cached half an hour
_CACHE: dict = {}

FIRMS_WINDOW_DAYS = 5   # capped at 5 by this key/source
FIRMS_BOX_DEG = 0.45    # ~50 km half-width; triage range, not haze range
CLUSTER_KM = 2.0        # VIIRS pixels this close are one fire, not several

# Spread model constants. See the module docstring before quoting these.
ROS_BASE_KMH = 0.15     # creeping spread with no wind
# Open grass runs at roughly 10% of wind speed. A mature palm block is not
# open grass: the canopy is closed, the fuel is the ground layer, and roads
# and drainage canals break the run every few hundred metres. Half the grass
# coefficient is still generous for plantation fuel.
ROS_WIND_COEF = 0.045
CONE_HALF_ANGLE_DEG = 22.0   # wind direction wanders; this is the envelope
# Twelve hours, not twenty-four. This drives a mobilisation decision taken now,
# and a 24-hour envelope over an 11 km estate flags most of the estate, which
# is an evacuation notice rather than a triage.
HORIZON_HOURS = 12
ROAD_FACTOR = 1.4       # straight line to road distance, standard planning factor

THREAT_BANDS = [
    ("critical", 2),
    ("high", 6),
    ("watch", 12),
]


# ── geometry ───────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial compass bearing from point 1 to point 2."""
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_delta(a: float, b: float) -> float:
    """Smallest absolute difference between two compass bearings."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _ring_centroid(ring):
    n = len(ring) - 1
    return (sum(p[0] for p in ring[:n]) / n, sum(p[1] for p in ring[:n]) / n)


def _offset(lat, lon, bearing, km):
    """Point `km` away from (lat, lon) along a compass bearing."""
    d_lat = km * math.cos(math.radians(bearing)) / 111.32
    d_lon = km * math.sin(math.radians(bearing)) / (111.32 * math.cos(math.radians(lat)))
    return [round(lon + d_lon, 6), round(lat + d_lat, 6)]


# ── live feeds ─────────────────────────────────────────────────────────────

def _cached(key, ttl, producer):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = producer()
    _CACHE[key] = (time.time(), value)
    return value


def fetch_hotspots(lat: float, lon: float, firms_key: str | None) -> dict:
    """Live VIIRS detections around the estate.

    Low-confidence pixels are dropped: VIIRS grades each detection l/n/h and
    the low grade is where most false positives live. Never raises - a dead
    feed degrades to available: False and the UI says the feed is down rather
    than implying there is no fire.
    """
    if not firms_key:
        return {"available": False, "reason": "no FIRMS_MAP_KEY", "detections": []}

    def go():
        d = FIRMS_BOX_DEG
        bbox = f"{lon - d:.4f},{lat - d:.4f},{lon + d:.4f},{lat + d:.4f}"
        url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
               f"{firms_key}/VIIRS_SNPP_NRT/{bbox}/{FIRMS_WINDOW_DAYS}")
        r = httpx.get(url, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        text = r.text.strip()
        if not text or "," not in text.splitlines()[0]:
            return {"available": False, "reason": "unexpected response", "detections": []}

        out = []
        dropped = 0
        for row in csv.DictReader(io.StringIO(text)):
            conf = str(row.get("confidence", "")).strip().lower()
            if conf.startswith("l"):
                dropped += 1
                continue
            try:
                out.append({
                    "lat": float(row["latitude"]),
                    "lon": float(row["longitude"]),
                    "frp": float(row["frp"]),
                    "bright_ti4": float(row.get("bright_ti4") or 0) or None,
                    "confidence": conf,
                    "acq_date": row.get("acq_date"),
                    "acq_time": row.get("acq_time"),
                    "daynight": row.get("daynight"),
                    "satellite": row.get("satellite"),
                })
            except (KeyError, TypeError, ValueError):
                dropped += 1
        return {
            "available": True,
            "source": "NASA FIRMS VIIRS_SNPP_NRT",
            "provenance": "real:firms",
            "window_days": FIRMS_WINDOW_DAYS,
            "detections": out,
            "dropped_low_confidence": dropped,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    try:
        return _cached(f"firms:{lat:.3f},{lon:.3f}", _TTL, go)
    except Exception as exc:
        log.warning("[fire] FIRMS fetch failed: %s", exc)
        return {"available": False, "reason": str(exc)[:120], "detections": []}


def fetch_weather(lat: float, lon: float) -> dict:
    """Current wind plus the dryness context the spread estimate needs."""

    def go():
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "wind_speed_10m,wind_direction_10m,temperature_2m,relative_humidity_2m",
                "daily": "precipitation_sum", "past_days": 14, "forecast_days": 1,
                "timezone": "auto",
            },
            timeout=_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        j = r.json()
        cur = j.get("current", {})
        rain = [x for x in (j.get("daily", {}).get("precipitation_sum") or [])
                if x is not None][:14]
        return {
            "available": True,
            "provenance": "real:open-meteo",
            "wind_kmh": cur.get("wind_speed_10m"),
            "wind_from_deg": cur.get("wind_direction_10m"),
            "temp_c": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "rain_14d_mm": round(sum(rain), 1) if rain else None,
            "dry_days_14d": sum(1 for x in rain if x < 1.0) if rain else None,
            "observed_at": cur.get("time"),
        }

    try:
        return _cached(f"wx:{lat:.3f},{lon:.3f}", _TTL, go)
    except Exception as exc:
        log.warning("[fire] weather fetch failed: %s", exc)
        return {"available": False, "reason": str(exc)[:120]}


# ── synthetic scenarios ────────────────────────────────────────────────────
#
# Used when the live feed is quiet, or when the demo needs a specific
# situation on screen. Shaped on the live values measured near EC in
# September 2026: FRP median 4.6 / max 97.7 MW, brightness 305-355 K,
# confidence overwhelmingly nominal, VIIRS passes near 01:30 and 13:30 local.

SCENARIOS = {
    "live": "Live NASA FIRMS detections, whatever is burning right now.",
    "near_miss": "Synthetic: an active front 4 km upwind, blocks in the cone.",
    "severe": "Synthetic: multiple fronts 2.5 km upwind under a stronger, drier wind.",
}


def _upwind_half_extent_km(feats, clat, clon, wind_from_deg) -> float:
    """How far the estate itself reaches into the upwind direction.

    Synthetic fires are placed relative to the estate *edge*, not its centre.
    EC spans 11 km, so a cluster placed 4 km from the centroid would sit in
    the middle of the plantation and read as a fire already inside the estate.
    """
    ux = math.sin(math.radians(wind_from_deg))
    uy = math.cos(math.radians(wind_from_deg))
    k = 111.32
    best = 0.0
    for f in feats:
        for x, y in f["geometry"]["coordinates"][0]:
            dx = (x - clon) * k * math.cos(math.radians(clat))
            dy = (y - clat) * k
            best = max(best, dx * ux + dy * uy)
    return best


def _synthetic_hotspots(lat, lon, wind_from_deg, spec, feats) -> dict:
    """VIIRS-shaped detections placed just beyond the estate's upwind edge."""
    import random
    rng = random.Random(4771)
    dets = []
    now = datetime.now(timezone.utc)
    edge = _upwind_half_extent_km(feats, lat, lon, wind_from_deg)
    for cluster in spec["clusters"]:
        # Upwind means sitting at the bearing the wind is coming FROM,
        # offset beyond the estate boundary rather than from its centre.
        c_lat_lon = _offset(lat, lon, (wind_from_deg + cluster["bearing_offset"]) % 360,
                            edge + cluster["km_beyond_edge"])
        c_lon, c_lat = c_lat_lon
        for _ in range(cluster["pixels"]):
            # VIIRS resolves at 375 m, so a front reads as a scatter of pixels.
            j_lat = c_lat + rng.uniform(-0.006, 0.006)
            j_lon = c_lon + rng.uniform(-0.006, 0.006)
            dets.append({
                "lat": round(j_lat, 5),
                "lon": round(j_lon, 5),
                "frp": round(rng.uniform(*cluster["frp"]), 2),
                "bright_ti4": round(rng.uniform(320, 360), 2),
                "confidence": rng.choice(["n", "n", "n", "h"]),
                "acq_date": now.date().isoformat(),
                "acq_time": rng.choice(["0332", "0410", "1616"]),
                "daynight": "D",
                "satellite": "N",
            })
    return {
        "available": True,
        "source": "synthetic scenario",
        "provenance": "synthetic",
        "window_days": FIRMS_WINDOW_DAYS,
        "detections": dets,
        "dropped_low_confidence": 0,
        "fetched_at": now.isoformat(timespec="seconds"),
    }


_SCENARIO_SPECS = {
    "near_miss": {
        "clusters": [{"km_beyond_edge": 1.6, "bearing_offset": 0,
                      "pixels": 9, "frp": (8, 45)}],
        "wind_override": None,
    },
    "severe": {
        "clusters": [
            {"km_beyond_edge": 0.8, "bearing_offset": -8, "pixels": 14, "frp": (25, 98)},
            {"km_beyond_edge": 3.0, "bearing_offset": 14, "pixels": 10, "frp": (15, 60)},
        ],
        "wind_override": {"wind_kmh": 21.0, "humidity_pct": 38, "rain_14d_mm": 4.2,
                          "dry_days_14d": 13},
    },
}


# ── clustering and the spread estimate ─────────────────────────────────────

def cluster_detections(dets, radius_km=CLUSTER_KM):
    """Greedy spatial clustering. VIIRS pixels within ~2 km are one fire."""
    clusters = []
    for d in sorted(dets, key=lambda x: -x["frp"]):
        for c in clusters:
            if haversine_km(c["lat"], c["lon"], d["lat"], d["lon"]) <= radius_km:
                c["members"].append(d)
                n = len(c["members"])
                c["lat"] += (d["lat"] - c["lat"]) / n
                c["lon"] += (d["lon"] - c["lon"]) / n
                break
        else:
            clusters.append({"lat": d["lat"], "lon": d["lon"], "members": [d]})

    out = []
    for i, c in enumerate(clusters):
        frp = sum(m["frp"] for m in c["members"])
        latest = max((f"{m['acq_date']} {m['acq_time']}" for m in c["members"]),
                     default=None)
        out.append({
            "id": f"cluster:{i}",
            "lat": round(c["lat"], 5),
            "lon": round(c["lon"], 5),
            "pixels": len(c["members"]),
            "frp_total": round(frp, 2),
            "frp_max": round(max(m["frp"] for m in c["members"]), 2),
            "confidence_high": sum(1 for m in c["members"] if m["confidence"].startswith("h")),
            "latest_detection": latest,
        })
    return sorted(out, key=lambda c: -c["frp_total"])


def dryness_index(wx: dict) -> dict:
    """0-1 dryness, from 14-day rainfall and current humidity.

    Crude on purpose. It exists to stop the spread estimate quoting the same
    rate in the wet season as in the dry, not to be a fire danger rating.
    A real deployment would use a Keetch-Byram index or the Indonesian FDRS.
    """
    rain = wx.get("rain_14d_mm")
    rh = wx.get("humidity_pct")
    if rain is None and rh is None:
        return {"value": 0.5, "basis": "no data, assumed mid-range"}
    parts, basis = [], []
    if rain is not None:
        # 0 mm over 14 days -> 1.0; 80 mm or more -> 0.0
        parts.append(max(0.0, min(1.0, 1.0 - rain / 80.0)))
        basis.append(f"{rain} mm rain in 14 days")
    if rh is not None:
        # 30% RH -> 1.0; 90% -> 0.0
        parts.append(max(0.0, min(1.0, (90.0 - rh) / 60.0)))
        basis.append(f"{rh}% humidity")
    return {"value": round(sum(parts) / len(parts), 2), "basis": ", ".join(basis)}


def rate_of_spread_kmh(wind_kmh: float | None, dryness: float) -> float:
    w = wind_kmh if wind_kmh is not None else 8.0
    # Dryness scales the whole rate between half and full.
    return round((ROS_BASE_KMH + ROS_WIND_COEF * w) * (0.5 + 0.5 * dryness), 3)


def cone_polygon(lat, lon, travel_deg, length_km, half_angle=CONE_HALF_ANGLE_DEG):
    """Downwind spread envelope from a fire cluster, as a GeoJSON ring."""
    ring = [[round(lon, 6), round(lat, 6)]]
    steps = 14
    for i in range(steps + 1):
        a = travel_deg - half_angle + (2 * half_angle) * i / steps
        ring.append(_offset(lat, lon, a % 360, length_km))
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


# ── assets ─────────────────────────────────────────────────────────────────

def load_assets() -> dict:
    if not _ASSETS.exists():
        log.warning("[fire] %s missing - run gis/build_fire_assets.py", _ASSETS)
        return {"type": "FeatureCollection", "features": []}
    return json.loads(_ASSETS.read_text(encoding="utf-8"))


def _asset_point(f):
    g = f["geometry"]
    if g["type"] == "Point":
        return g["coordinates"][1], g["coordinates"][0]
    pts = g["coordinates"]
    return pts[len(pts) // 2][1], pts[len(pts) // 2][0]


# ── the assessment ─────────────────────────────────────────────────────────

def assess(blocks_fc: dict, estate: str, firms_key: str | None,
           scenario: str = "live") -> dict:
    """Full fire triage for one estate: threat, exposure, mobilisation."""
    feats = blocks_fc["features"]
    pts = [p for f in feats for p in f["geometry"]["coordinates"][0]]
    clat = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
    clon = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2

    wx = fetch_weather(clat, clon)
    spec = _SCENARIO_SPECS.get(scenario)
    if spec and spec.get("wind_override"):
        wx = {**wx, **spec["wind_override"], "provenance": "synthetic override"}

    wind_from = wx.get("wind_from_deg")
    if wind_from is None:
        wind_from = 116.0  # ESE monsoon default for southern Papua
    travel = (wind_from + 180.0) % 360.0

    if spec:
        hot = _synthetic_hotspots(clat, clon, wind_from, spec, feats)
    else:
        hot = fetch_hotspots(clat, clon, firms_key)

    dry = dryness_index(wx)
    ros = rate_of_spread_kmh(wx.get("wind_kmh"), dry["value"])
    reach_km = round(ros * HORIZON_HOURS, 2)

    clusters = cluster_detections(hot.get("detections", []))
    for c in clusters:
        c["distance_km"] = round(haversine_km(clat, clon, c["lat"], c["lon"]), 2)
        c["bearing_from_estate"] = round(bearing_deg(clat, clon, c["lat"], c["lon"]), 1)
        c["threatens_blocks"] = 0

    # -- per-block threat --------------------------------------------------
    #
    # Every cluster is tested against every block rather than pre-filtered on
    # distance to the estate centroid. A fire only has to reach the nearest
    # block, and on an 11 km estate the centroid is up to 7 km further away
    # than the boundary it would actually hit first.
    by_id = {c["id"]: c for c in clusters}
    threatened, band_counts = [], {}
    for f in feats:
        p = f["properties"]
        blon, blat = _ring_centroid(f["geometry"]["coordinates"][0])
        best = None
        for c in clusters:
            dist = haversine_km(c["lat"], c["lon"], blat, blon)
            if dist > reach_km:
                continue
            off = angle_delta(bearing_deg(c["lat"], c["lon"], blat, blon), travel)
            if off > CONE_HALF_ANGLE_DEG:
                continue
            eta = dist / ros if ros > 0 else None
            if eta is None:
                continue
            if best is None or eta < best["eta_hours"]:
                best = {
                    "cluster_id": c["id"], "distance_km": round(dist, 2),
                    "eta_hours": round(eta, 1), "bearing_offset_deg": round(off, 1),
                    "frp_total": c["frp_total"],
                }
        if not best:
            continue
        band = next((name for name, hrs in THREAT_BANDS if best["eta_hours"] <= hrs), None)
        if band is None:
            continue
        band_counts[band] = band_counts.get(band, 0) + 1
        by_id[best["cluster_id"]]["threatens_blocks"] += 1
        threatened.append({
            "block_id": f["id"],
            "block_code": p.get("block_code"),
            "block_label": p.get("block_label"),
            "division_code": p.get("division_code"),
            "planted_ha": p.get("planted_ha"),
            "palms": p.get("palms"),
            "lat": round(blat, 6), "lon": round(blon, 6),
            "band": band,
            **best,
        })
    threatened.sort(key=lambda b: b["eta_hours"])
    for c in clusters:
        c["upwind_of_estate"] = c["threatens_blocks"] > 0
    threatening = [c for c in clusters if c["upwind_of_estate"]]

    exposure = {
        "blocks": len(threatened),
        "planted_ha": round(sum(b["planted_ha"] or 0 for b in threatened), 1),
        "palms": sum(b["palms"] or 0 for b in threatened),
        "by_band": band_counts,
        "valuation": None,
        "valuation_note": ("No tonnage or price is quoted. The export carries no "
                           "average bunch weight, so hectares and palms are the "
                           "honest unit of exposure here."),
    }

    assets = load_assets()
    mobilisation = _mobilise(assets, threatened)

    # -- renderable layers -------------------------------------------------
    hotspot_feats = [{
        "type": "Feature",
        "id": c["id"],
        "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
        "properties": {**{k: v for k, v in c.items() if k not in ("lat", "lon")},
                       "entity": "fire_cluster",
                       "provenance": hot.get("provenance")},
    } for c in clusters]

    cone_feats = [{
        "type": "Feature",
        "id": f"cone:{c['id']}",
        "geometry": cone_polygon(c["lat"], c["lon"], travel, reach_km),
        "properties": {"entity": "spread_cone", "cluster_id": c["id"],
                       "frp_total": c["frp_total"],
                       "threatens_blocks": c["threatens_blocks"],
                       "reach_km": reach_km, "travel_deg": round(travel, 1),
                       "provenance": "derived"},
    } for c in threatening]

    route_feats = ([{
        "type": "Feature",
        "id": "mobilisation:route",
        "geometry": mobilisation["route"],
        "properties": {"entity": "mobilisation_route",
                       "post": mobilisation["post"]["name"],
                       "travel_minutes": mobilisation["post"]["travel_minutes"],
                       "provenance": "synthetic"},
    }] if mobilisation.get("route") else [])

    return {
        "estate": estate.upper(),
        "scenario": scenario,
        "scenario_label": SCENARIOS.get(scenario, scenario),
        "centre": {"lat": round(clat, 6), "lon": round(clon, 6)},
        "weather": wx,
        "wind": {
            "from_deg": wind_from,
            "travel_deg": round(travel, 1),
            "speed_kmh": wx.get("wind_kmh"),
            "provenance": wx.get("provenance", "unknown"),
        },
        "hotspots": {
            "available": hot.get("available", False),
            "provenance": hot.get("provenance"),
            "source": hot.get("source"),
            "reason": hot.get("reason"),
            "detections": len(hot.get("detections", [])),
            "dropped_low_confidence": hot.get("dropped_low_confidence"),
            "fetched_at": hot.get("fetched_at"),
            # Threatening clusters first so the cap can never drop one.
            "clusters": sorted(clusters,
                               key=lambda c: (not c["upwind_of_estate"],
                                              -c["frp_total"]))[:40],
            "clusters_total": len(clusters),
            "threatening": len(threatening),
        },
        "model": {
            "rate_of_spread_kmh": ros,
            "horizon_hours": HORIZON_HOURS,
            "reach_km": reach_km,
            "cone_half_angle_deg": CONE_HALF_ANGLE_DEG,
            "dryness": dry,
            "caveat": ("Screening model. Rate of spread is estimated from wind "
                       "speed and a rainfall/humidity dryness index, with no fuel "
                       "load, slope, fuel moisture or suppression term. Use the "
                       "arrival times to order which blocks to worry about first, "
                       "not as a countdown."),
        },
        "threatened_blocks": threatened,
        "exposure": exposure,
        "mobilisation": mobilisation,
        "layers": {
            "hotspots": {"type": "FeatureCollection", "features": hotspot_feats},
            "cones": {"type": "FeatureCollection", "features": cone_feats},
            "route": {"type": "FeatureCollection", "features": route_feats},
            "assets": assets,
        },
        "provenance_summary": {
            "real": ["block geometry and palm counts (client ArcGIS)",
                     "wind, humidity, rainfall (Open-Meteo)"]
                    + (["fire detections (NASA FIRMS VIIRS)"]
                       if hot.get("provenance") == "real:firms" else []),
            "synthetic": ["fire posts, watch towers, reservoirs, canals",
                          "crew on shift and vehicle response speeds"]
                         + (["fire detections (scenario)"]
                            if hot.get("provenance") == "synthetic" else []),
        },
    }


def _mobilise(assets: dict, threatened: list) -> dict:
    """Nearest post, nearest water, and a response estimate for the front block."""
    if not threatened:
        return {"required": False,
                "note": "No block is currently downwind of an active detection."}

    target = threatened[0]
    posts = [f for f in assets["features"]
             if f["properties"]["entity"] == "fire_post"
             and f["properties"].get("crew_on_shift", 0) > 0]
    water = [f for f in assets["features"]
             if f["properties"]["entity"] == "water_source"]
    if not posts:
        return {"required": True, "note": "No fire post has crew on shift."}

    def nearest(items):
        best, best_d = None, 1e9
        for f in items:
            la, lo = _asset_point(f)
            d = haversine_km(target["lat"], target["lon"], la, lo)
            if d < best_d:
                best, best_d = f, d
        return best, best_d

    post, post_km = nearest(posts)
    wat, wat_km = nearest(water) if water else (None, None)
    pp = post["properties"]
    road_km = round(post_km * ROAD_FACTOR, 2)
    speed = pp.get("response_kmh") or 25
    travel_min = round(road_km / speed * 60, 1)

    la, lo = _asset_point(post)
    route = {"type": "LineString",
             "coordinates": [[round(lo, 6), round(la, 6)],
                             [target["lon"], target["lat"]]]}

    # The nearest post is rarely the whole answer. Total crew reachable inside
    # the fire's own arrival window is the number that decides whether this is
    # a containable incident or a call to the regional office.
    reachable = 0
    for f in posts:
        fla, flo = _asset_point(f)
        d = haversine_km(target["lat"], target["lon"], fla, flo) * ROAD_FACTOR
        spd = f["properties"].get("response_kmh") or 25
        if target["eta_hours"] is None or d / spd <= target["eta_hours"]:
            reachable += f["properties"].get("crew_on_shift", 0)

    def water_supply(props):
        if props.get("continuous"):
            return f"continuous draw at {props.get('draw_rate_lpm')} L/min"
        m3 = props.get("usable_m3")
        return f"{m3} m3 usable" if m3 is not None else "capacity not recorded"

    return {
        "required": True,
        "target_block": target["block_label"] or target["block_code"],
        "target_block_id": target["block_id"],
        "eta_fire_hours": target["eta_hours"],
        "crew_reachable_in_window": reachable,
        "post": {
            "id": post["id"], "name": pp["name"], "kind": pp["kind"],
            "crew_on_shift": pp["crew_on_shift"],
            "straight_km": round(post_km, 2), "road_km": road_km,
            "travel_minutes": travel_min,
            "tanker_litres": pp.get("tanker_litres"),
            "pumps": pp.get("pumps"),
        },
        "water": ({"id": wat["id"], "name": wat["properties"]["name"],
                   "kind": wat["properties"]["kind"],
                   "supply": water_supply(wat["properties"]),
                   "usable_m3": wat["properties"].get("usable_m3"),
                   "distance_km": round(wat_km, 2)} if wat else None),
        "route": route,
        "margin_hours": (round(target["eta_hours"] - travel_min / 60, 1)
                         if target["eta_hours"] is not None else None),
        "note": (f"Road distance assumes a {ROAD_FACTOR}x detour factor on straight-line "
                 "distance. The estate road network is not in EPMS, so no route is "
                 "actually solved."),
        "provenance": "synthetic (posts, crew, speeds); real (block position)",
    }
