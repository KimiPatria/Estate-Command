"""Per-block terrain from the Copernicus 30 m DEM. REAL measurement.

Run once:  python gis/build_terrain.py
Writes:    gis/data/ec_terrain.json

Why this exists
---------------
Harvester productivity is not comparable across blocks. A cutter on a slope
carries fruit further and uphill, and quotas set without terrain are the
reason steep blocks get left uncollected. The proposal asks for slope from a
public DEM, and it costs nothing: Copernicus DEM GLO-30 sits in the same STAC
catalogue at earth-search that gis/build_ndre.py already reads Sentinel-2
from, so this is that script with a different collection and no band maths.

This is a REAL feed. The client supplies nothing for it, exactly like the
satellite canopy layer. Everything it produces is badged real:copernicus-dem.

The raster plumbing - bbox, TiTiler crop, point-in-polygon zonal masking - is
imported from build_ndre rather than copied, so a fix to the masking helps
both layers.

Slope in degrees, computed from the elevation grid
--------------------------------------------------
The grid is in EPSG:4326, so a degree of longitude is shorter than a degree of
latitude by cos(lat). The gradient is converted to metres before the arctangent
or every slope would be wrong by that factor. Slope is then the magnitude of
the steepest descent at each cell, and the per-block figure is the mean over
the cells inside the polygon.

GLO-30 is a SURFACE model, and that matters here
------------------------------------------------
Copernicus DEM is a DSM: it measures the top of whatever is standing, and over
a closed palm canopy that is the palms, not the ground. Taking the gradient of
the raw 30 m grid over this estate gives a mean slope of 4.9 degrees, which is
impossible on a coastal plain whose total relief is 29 m across 12 km. That
number is canopy crowns and DEM noise, not landform.

So the grid is smoothed to a landform scale before the gradient is taken.
Slope falls monotonically with the smoothing window - 4.9 degrees raw, 3.6 at
90 m, 2.7 at 150 m, 1.6 at 270 m - which is the signature of high-frequency
texture rather than topography. LANDFORM_M sets the window.

The discarded component is not waste. The residual between the raw surface and
the smoothed one is canopy roughness, and over a uniform stand of one planting
year that is a stand-uniformity reading: gaps, thin patches and uneven crowns
all raise it. It is reported per block as `canopy_roughness_m` and badged real
like everything else here, with the caveat that it also carries DEM noise and
is a relative measure, not an absolute one.
"""

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

# Run directly ("python gis/build_terrain.py") as the docstring says, as well
# as via "python -m gis.build_terrain". Same fix build_synthetic.py applies.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis import ontology
from gis.build_ndre import (STAC_URL, _HEADERS, _bbox, _polygon_mask,
                            fetch_array)

log = logging.getLogger("estate-command.build-terrain")

COLLECTION = "cop-dem-glo-30"
OUT_DIR = Path(__file__).parent / "data"
TARGET_M = 30          # GLO-30 native resolution; do not oversample

# Smoothing window for the landform surface, in metres. 150 m is about five
# DEM cells: wide enough to average away individual palm crowns, narrow enough
# to keep the low ridges and swales that are the real topography here. See the
# module docstring for the measurements behind the choice.
LANDFORM_M = 150

# Copernicus DEM tiles cover one degree each, so an estate near a tile corner
# needs several. They are mosaicked by TiTiler when passed together.
MAX_TILES = 4


def search_tiles(bbox) -> list[dict]:
    """Every DEM tile intersecting the estate bbox."""
    body = {"collections": [COLLECTION], "bbox": list(bbox), "limit": MAX_TILES}
    r = httpx.post(f"{STAC_URL}/search", json=body, headers=_HEADERS, timeout=90)
    r.raise_for_status()
    return r.json().get("features", [])


def _grid_size(bbox) -> tuple[int, int]:
    """Pixel dimensions for the bbox at the DEM's native resolution."""
    w, s, e, n = bbox
    lat0 = (s + n) / 2
    width_m = (e - w) * 111_320 * math.cos(math.radians(lat0))
    height_m = (n - s) * 111_320
    return (max(16, round(width_m / TARGET_M)), max(16, round(height_m / TARGET_M)))


def _box_mean(a: np.ndarray, k: int) -> np.ndarray:
    """Separable k x k moving average, edges handled by shrinking the window.

    Hand-rolled on cumulative sums rather than pulled from scipy, matching the
    choice layers.py already made for rank correlation: this is a dozen lines
    and scipy is not a declared dependency of the project.
    """
    if k < 2:
        return a
    r = k // 2

    def axis_mean(x):
        n = x.shape[1]
        pad = np.concatenate([np.zeros((x.shape[0], 1)), np.cumsum(x, axis=1)], axis=1)
        lo = np.clip(np.arange(n) - r, 0, n)
        hi = np.clip(np.arange(n) + r + 1, 0, n)
        return (pad[:, hi] - pad[:, lo]) / (hi - lo)

    return axis_mean(axis_mean(a).T).T


def _slope_deg(elev: np.ndarray, bbox, shape) -> np.ndarray:
    """Slope in degrees per cell, from the elevation grid.

    np.gradient returns change per cell index; the cell size in metres differs
    between the two axes because the grid is in degrees. Converting first is
    what keeps the arctangent meaningful.
    """
    w, s, e, n = bbox
    h, wd = shape
    lat0 = (s + n) / 2
    x_m = (e - w) * 111_320 * math.cos(math.radians(lat0)) / max(wd - 1, 1)
    y_m = (n - s) * 111_320 / max(h - 1, 1)
    # Rows run north to south, so the sign of the y gradient is flipped. Slope
    # magnitude is unaffected; aspect would care, which is why it is noted.
    dz_dy, dz_dx = np.gradient(elev, y_m, x_m)
    return np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))


def build(estate: str = "EC") -> dict:
    blocks = ontology.blocks_geojson(estate)
    if blocks is None:
        raise SystemExit(f"No block geometry for estate {estate!r}.")
    feats = blocks["features"]
    bbox = _bbox(feats)

    tiles = search_tiles(bbox)
    if not tiles:
        raise SystemExit(f"No {COLLECTION} tiles cover estate {estate!r}.")
    log.info("[terrain] %d DEM tile(s) over %s", len(tiles), estate)

    width, height = _grid_size(bbox)
    log.info("[terrain] grid %dx%d at ~%d m", width, height, TARGET_M)

    # One tile is the normal case for an estate this size. Several are averaged
    # where they overlap, which is what a mosaic does at a tile seam anyway.
    stack, masks = [], []
    for t in tiles:
        url = f"{STAC_URL}/collections/{COLLECTION}/items/{t['id']}"
        data, alpha = fetch_array(url, bbox, ["data"], width, height)
        stack.append(data[0])
        masks.append(alpha > 0)

    elev = np.full((height, width), np.nan)
    for arr, m in zip(stack, masks):
        # Copernicus uses a large negative fill for no-data over water.
        good = m & np.isfinite(arr) & (arr > -1000)
        elev = np.where(good & ~np.isfinite(elev), arr, elev)

    covered = int(np.isfinite(elev).sum())
    if not covered:
        raise SystemExit("DEM returned no valid elevation over the estate.")

    # Slope needs a filled grid; holes are patched with the scene mean so the
    # gradient at their edge is not a cliff. Those cells are excluded again
    # from every per-block statistic below.
    filled = np.where(np.isfinite(elev), elev, np.nanmean(elev))
    valid = np.isfinite(elev)

    # Split the surface into landform and canopy. GLO-30 sees the top of the
    # palms, so the raw gradient is crowns and noise; the smoothed grid is the
    # ground shape and the residual is how rough the stand is.
    k = max(1, round(LANDFORM_M / TARGET_M))
    landform = _box_mean(filled, k)
    slope = _slope_deg(landform, bbox, (height, width))
    surface_slope = _slope_deg(filled, bbox, (height, width))
    roughness = filled - landform
    log.info("[terrain] landform window %d m (%d cells): mean slope %.2f deg, "
             "raw surface %.2f deg", LANDFORM_M, k,
             float(slope[valid].mean()), float(surface_slope[valid].mean()))

    w, s, e, n = bbox
    lons = np.linspace(w, e, width)
    lats = np.linspace(n, s, height)

    out, measured = {}, 0
    for f in feats:
        p = f["properties"]
        key = f'{int(p["division_code"])}|{int(p["block_code"])}'
        mask = _polygon_mask(f["geometry"]["coordinates"][0], lons, lats)
        cells = mask & valid
        px = int(cells.sum())
        if not px:
            out[key] = {"block_id": f["id"], "block_label": p.get("block_label"),
                        "pixels": 0, "elevation_m": None, "slope_deg": None,
                        "slope_max_deg": None, "relief_m": None,
                        "canopy_roughness_m": None}
            continue
        measured += 1
        ev, sv = elev[cells], slope[cells]
        lf, rg = landform[cells], roughness[cells]
        out[key] = {
            "block_id": f["id"],
            "block_label": p.get("block_label"),
            "pixels": px,
            "elevation_m": round(float(ev.mean()), 2),
            "slope_deg": round(float(sv.mean()), 3),
            "slope_max_deg": round(float(sv.max()), 3),
            "slope_p90_deg": round(float(np.percentile(sv, 90)), 3),
            # Relief inside a block is what a cutter actually walks, and it
            # separates a uniformly tilted block from a broken one. Taken on
            # the landform surface so canopy crowns do not inflate it.
            "relief_m": round(float(lf.max() - lf.min()), 2),
            # How rough the canopy top is once the ground shape is removed.
            "canopy_roughness_m": round(float(rg.std()), 3),
            "surface_slope_deg": round(float(surface_slope[cells].mean()), 3),
        }

    slopes = [v["slope_deg"] for v in out.values() if v["slope_deg"] is not None]
    elevs = [v["elevation_m"] for v in out.values() if v["elevation_m"] is not None]
    rough = [v["canopy_roughness_m"] for v in out.values()
             if v.get("canopy_roughness_m") is not None]

    doc = {
        "estate": estate.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": "real:copernicus-dem",
        "source": {
            "collection": COLLECTION,
            "name": "Copernicus DEM GLO-30",
            "resolution_m": TARGET_M,
            "tiles": [t["id"] for t in tiles],
            "licence": "Free and open, ESA/Copernicus.",
        },
        "method": {
            "slope": ("degrees, arctan of the metre-space gradient of the "
                      f"landform surface (DEM smoothed to {LANDFORM_M} m)"),
            "landform_window_m": LANDFORM_M,
            "grid": [height, width],
            "zonal": "mean over DEM cells whose centre falls inside the polygon",
            "dsm_caveat": (
                "GLO-30 is a surface model: over closed palm canopy the raw "
                "gradient measures crowns, not ground. Raw surface slope "
                "averages "
                f"{float(surface_slope[valid].mean()):.2f} degrees here against "
                f"{float(slope[valid].mean()):.2f} on the smoothed landform, on "
                "an estate with 29 m of relief across 12 km. slope_deg is the "
                "landform figure; surface_slope_deg is kept per block so the "
                "difference stays visible."),
            "canopy_roughness": (
                "Standard deviation of the residual between the raw surface and "
                "the landform surface, in metres. A stand-uniformity proxy: "
                "gaps and uneven crowns raise it. Relative, not absolute - it "
                "also carries DEM noise."),
            "note": ("The client supplies nothing for this layer. It is "
                     "measured over their own block polygons from a public "
                     "elevation model, exactly like the Sentinel-2 canopy "
                     "layer."),
        },
        "coverage": {
            "blocks": len(feats),
            "measured": measured,
            "measured_pct": round(100 * measured / len(feats), 1) if feats else 0,
            "dem_cells_valid": covered,
        },
        "summary": {
            "slope_mean_deg": round(sum(slopes) / len(slopes), 3) if slopes else None,
            "slope_max_deg": round(max(slopes), 3) if slopes else None,
            "surface_slope_mean_deg": round(float(surface_slope[valid].mean()), 3),
            "elevation_mean_m": round(sum(elevs) / len(elevs), 2) if elevs else None,
            "elevation_range_m": ([round(min(elevs), 2), round(max(elevs), 2)]
                                  if elevs else None),
            "canopy_roughness_mean_m": round(
                sum(rough) / len(rough), 3) if rough else None,
            "canopy_roughness_range_m": ([round(min(rough), 3), round(max(rough), 3)]
                                         if rough else None),
            "reading": _reading(slopes),
        },
        "blocks": out,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{estate.lower()}_terrain.json"
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    log.info("[terrain] %s: %d/%d blocks measured -> %s",
             estate, measured, len(feats), path)
    return doc


def _reading(slopes) -> str:
    """What the numbers mean for the productivity model that reads them.

    A finding of "this estate is flat" is worth stating plainly: it tells the
    client that terrain will not explain their yield variance here, and stops
    the productivity model being asked to carry a covariate with no range.
    """
    if not slopes:
        return "No slope measured."
    mx = max(slopes)
    mean = sum(slopes) / len(slopes)
    if mx < 3:
        return (f"Effectively flat: mean slope {mean:.1f} degrees, steepest block "
                f"{mx:.1f}. Terrain cannot explain yield or productivity "
                "differences on this estate, and a harvester quota here needs no "
                "slope adjustment. On a hill estate the same layer would carry "
                "real signal.")
    if mx < 8:
        return (f"Gently undulating: mean {mean:.1f} degrees, steepest {mx:.1f}. "
                "Enough range to adjust quotas at the margin.")
    return (f"Broken ground: mean {mean:.1f} degrees, steepest {mx:.1f}. Terrain "
            "is a first-order term in any harvester target here.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--estate", default="EC")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        doc = build(a.estate)
    except (httpx.HTTPError, RuntimeError) as e:
        log.error("[terrain] %s", e)
        return 1
    s = doc["summary"]
    print(f"\n  {doc['coverage']['measured']}/{doc['coverage']['blocks']} blocks measured")
    print(f"  landform slope  mean {s['slope_mean_deg']} deg, max {s['slope_max_deg']} deg")
    print(f"  raw surface     mean {s['surface_slope_mean_deg']} deg  (canopy, not ground)")
    print(f"  elevation       {s['elevation_range_m']} m")
    print(f"  canopy roughness{s['canopy_roughness_range_m']} m")
    print(f"\n  {s['reading']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
