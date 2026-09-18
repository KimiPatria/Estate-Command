"""Derive real estate footprints from harvester GPS in t_oph.

Run once:  python gis/build_footprints.py
Writes:    gis/data/estate_footprints.geojson

Why only footprints, and not block boundaries
---------------------------------------------
t_oph.oph_lat / oph_long has excellent coverage (94% on K3, 100% on BA) but
carries no usable per-block signal. Measured on K3:

    block centroids sit a median of  54 m apart
    each block's own fixes spread   879 m from its own centroid (median)

Within-block scatter is more than an order of magnitude larger than
between-block separation, so the blocks are not separable. The fix is almost
certainly recorded where the device was when the harvest card was created or
synced, not where the fruit was cut. Trimming outliers does not rescue it —
even the tightest decile sits at 269 m.

What the GPS *does* support is the estate outline: K3's fixes span roughly
6.8 x 4.7 km, which is a plausible estate. So this script hulls the cloud and
stops there. Anything finer than an estate outline from this source would be
a fabrication dressed as a reconstruction.

m_tph is not used at all: all 840 populated rows read latitude '1',
longitude '1', and the remaining 12 are empty. There are no real TPH
coordinates in either database.
"""

import json
import logging
import math
import os
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from scipy.spatial import ConvexHull
from sqlalchemy import create_engine, text

log = logging.getLogger("estate-command.footprints")

OUT = Path(__file__).parent / "data" / "estate_footprints.geojson"

# Fraction of fixes kept, measured by distance from the estate's robust centre.
# 0.98 keeps the estate outline honest while dropping the handful of fixes
# recorded off-estate (a phone syncing from town, a mistyped block).
KEEP_QUANTILE = 0.98

FIXES_SQL = text(
    """
    select oph_estate_code   as estate,
           oph_division_code as division,
           oph_lat::float    as lat,
           oph_long::float   as lon
    from t_oph
    where oph_lat is not null and oph_long is not null
      and oph_lat::float  <> 0
      and oph_long::float <> 0
    """
)


def _metres(lat, lon, lat0, lon0):
    """Local equirectangular projection, good enough at estate scale."""
    k = 111320.0
    return (lon - lon0) * k * math.cos(math.radians(lat0)), (lat - lat0) * k


def _hull(lats, lons):
    """Convex hull of a trimmed point cloud, returned as a closed lon/lat ring."""
    lat0, lon0 = float(np.median(lats)), float(np.median(lons))
    x, y = _metres(lats, lons, lat0, lon0)
    r = np.hypot(x, y)
    keep = r <= np.quantile(r, KEEP_QUANTILE)
    pts = np.c_[lons[keep], lats[keep]]
    if len(pts) < 3:
        return None, 0
    h = ConvexHull(pts)
    ring = [[float(p[0]), float(p[1])] for p in pts[h.vertices]]
    ring.append(ring[0])
    return ring, int(keep.sum())


def _ring_area_ha(ring):
    """Shoelace area of a lon/lat ring, in hectares."""
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = 111320.0
    xy = [((p[0]) * k * math.cos(math.radians(lat0)), p[1] * k) for p in ring]
    a = 0.0
    for i in range(len(xy) - 1):
        a += xy[i][0] * xy[i + 1][1] - xy[i + 1][0] * xy[i][1]
    return abs(a) / 2.0 / 10_000.0


def build() -> dict:
    load_dotenv()
    features = []
    for env_key in ("DATABASE_URL", "DATABASE_URL_BA"):
        url = os.getenv(env_key)
        if not url:
            log.warning("%s is not set, skipping", env_key)
            continue
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(FIXES_SQL).fetchall()
        if not rows:
            continue
        by_estate: dict[str, list] = {}
        for r in rows:
            by_estate.setdefault(r.estate, []).append((r.lat, r.lon, r.division))

        for estate, pts in by_estate.items():
            lats = np.array([p[0] for p in pts])
            lons = np.array([p[1] for p in pts])
            ring, kept = _hull(lats, lons)
            if ring is None:
                continue
            divisions = sorted({p[2] for p in pts})
            features.append(
                {
                    "type": "Feature",
                    "id": f"estate:{estate}",
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {
                        "entity": "estate",
                        "estate_code": estate,
                        "provenance": "real:gps-hull",
                        "source": f"t_oph harvester GPS via {env_key}",
                        "gps_fixes": len(pts),
                        "gps_fixes_kept": kept,
                        "divisions": len(divisions),
                        "hull_area_ha": round(_ring_area_ha(ring), 1),
                        "caveat": (
                            "Estate outline only. Harvester GPS cannot resolve "
                            "block boundaries: block centroids sit ~54 m apart "
                            "while each block's fixes spread ~879 m."
                        ),
                    },
                }
            )
            log.info("hulled estate %s from %d fixes", estate, len(pts))

    return {"type": "FeatureCollection", "features": features}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    fc = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc), encoding="utf-8")
    for f in fc["features"]:
        p = f["properties"]
        print(f"  {p['estate_code']:4s} {p['hull_area_ha']:9.1f} ha  "
              f"{p['gps_fixes']:7d} fixes  {p['divisions']} divisions")
    print(f"wrote {OUT} ({len(fc['features'])} estate footprints)")
