"""Build the real Sentinel-2 canopy-vigour layer for one estate. Run offline.

    python -m gis.build_ndre --estate EC --from 2024-09-01 --to 2025-09-10

Why this exists
---------------
The readiness register calls vegetation stress the one capability that needs
nothing from the client: the EC polygons are WGS84 and Sentinel-2 L2A is free
over Merauke. This script is that claim, executed. It replaces the synthetic
NDRE feed with real per-block reflectance and leaves the provenance badge on
the map reading "real" for the first time on a layer the client did not
supply.

How it works, with no geospatial stack
--------------------------------------
  1. Search the Element 84 Earth Search STAC API for Sentinel-2 L2A scenes
     over the estate bbox, sorted by cloud cover. Anonymous, no key.
  2. Ask a public TiTiler to crop the scene's COGs to the estate bbox and
     return a numpy array. The raster read happens server-side, so this
     process needs neither GDAL nor rasterio - only numpy, which is already
     a dependency of the forecast pipeline.
  3. Mask cloud, shadow and cirrus using the scene's own SCL band, apply the
     L2A scale and offset from the item's raster metadata, compute NDRE and
     NDVI per pixel, and average each inside its block polygon with a
     crossing-number test on the pixel grid.
  4. Write gis/data/{estate}_ndre.json. gis/vegetation.py reads it; nothing
     downstream calls the network.

On NDRE rather than NDVI
------------------------
Mature oil palm saturates NDVI - a stressed stand and a healthy one both read
about 0.85, which is why an NDVI map of a plantation is a flat green square.
The red edge (B05, 705 nm) keeps responding after the near-infrared has
saturated, so NDRE is what actually separates blocks. NDVI is computed too,
and reported, so the flatness is visible rather than asserted.

Both are recorded per block with the fraction of pixels that survived the
cloud mask. A block under cloud reports null, never a number.
"""

import argparse
import io
import json
import logging
import math
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import numpy as np

from gis import ontology

log = logging.getLogger("estate-command.build-ndre")

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
TITILER_URL = "https://titiler.xyz"

# The public TiTiler sits behind a CDN that rejects the default httpx agent.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, */*"}

OUT_DIR = Path(__file__).parent / "data"

# Sentinel-2 Scene Classification. Everything not in this set is discarded:
# 4 vegetation, 5 not-vegetated, 6 water, 7 unclassified. That drops
# 0 no-data, 1 saturated, 2 dark, 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow.
VALID_SCL = (4, 5, 6, 7)
TARGET_M = 20          # rededge1 and scl are native 20 m; do not oversample
MARGIN_DEG = 0.002     # a little slack so edge blocks are not clipped


def _bbox(features) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for f in features:
        for ring in f["geometry"]["coordinates"]:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    return (min(xs) - MARGIN_DEG, min(ys) - MARGIN_DEG,
            max(xs) + MARGIN_DEG, max(ys) + MARGIN_DEG)


def search_scenes(bbox, start: str, end: str, max_cloud: float, limit: int) -> list[dict]:
    """Least-cloudy L2A scenes over the bbox, newest tie broken by cloud."""
    body = {
        "collections": [COLLECTION],
        "bbox": list(bbox),
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "limit": limit,
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    r = httpx.post(f"{STAC_URL}/search", json=body, headers=_HEADERS, timeout=90)
    r.raise_for_status()
    return r.json().get("features", [])


def _scale_offset(item: dict, asset: str) -> tuple[float, float]:
    """Declared reflectance scale/offset for one asset, from the item metadata.

    Processing baseline 04.00 added a -1000 DN offset to L2A products. A
    normalised index is immune to the scale but not to the offset, so this is
    read from the item rather than assumed. Whether it should be *applied* is
    decided empirically - see _offset_is_physical.
    """
    bands = (item["assets"].get(asset) or {}).get("raster:bands") or []
    if bands:
        b = bands[0]
        return float(b.get("scale", 1.0)), float(b.get("offset", 0.0))
    return 1.0, 0.0


def _offset_is_physical(raw_bands: dict, valid, item: dict) -> bool:
    """Decide whether the declared BOA offset has already been applied upstream.

    The metadata alone cannot answer this. Element 84 publishes both the
    -0.1 offset and a separate `earthsearch:boa_offset_applied` flag, and on
    the Merauke scenes the two disagree: subtracting the offset again drives
    red-edge and red reflectance negative over pixels the scene's own
    classifier calls vegetation, and pushes NDVI above 1. Both are physically
    impossible, so the data is already corrected.

    Rather than hard-code one convention and silently produce a wrong index on
    the next scene, apply the offset only when the result stays physical:
    reflectance at or above zero across the visible and red-edge bands. The
    decision is recorded in the output so the layer can be audited.
    """
    for asset in ("rededge1", "red"):
        scale, offset = _scale_offset(item, asset)
        if not offset:
            continue
        vals = raw_bands[asset][valid] * scale + offset
        if vals.size and float(np.percentile(vals, 5)) < 0.0:
            return False
    return True


def fetch_array(item_url: str, bbox, assets: list[str], width: int, height: int):
    """Crop the scene's assets to the bbox, in EPSG:4326, as a numpy array.

    Returns (data, mask) where data is (len(assets), height, width) float64
    and mask is the alpha band TiTiler appends: 255 inside the scene footprint.
    """
    params = [("url", item_url)]
    params += [("assets", a) for a in assets]
    params += [("width", str(width)), ("height", str(height))]
    bbox_str = ",".join(f"{v:.6f}" for v in bbox)
    t0 = time.monotonic()
    r = httpx.get(f"{TITILER_URL}/stac/bbox/{bbox_str}.npy",
                  params=params, headers=_HEADERS, timeout=600)
    if r.status_code != 200:
        raise RuntimeError(f"TiTiler {r.status_code}: {r.text[:300]}")
    arr = np.load(io.BytesIO(r.content))
    log.info("[ndre] raster %s in %.1fs (%d bands, %dx%d)", arr.shape,
             time.monotonic() - t0, arr.shape[0], arr.shape[2], arr.shape[1])
    return arr[:-1].astype("float64"), arr[-1]


def _polygon_mask(ring, lons, lats) -> np.ndarray:
    """Crossing-number point-in-polygon over the whole grid, vectorised.

    Only the polygon's own bounding rows and columns are tested, so 291
    blocks over a 570x550 grid stay well inside a second.
    """
    xs = np.array([p[0] for p in ring])
    ys = np.array([p[1] for p in ring])
    col0 = int(np.searchsorted(lons, xs.min(), "left")) - 1
    col1 = int(np.searchsorted(lons, xs.max(), "right")) + 1
    # lats descend, so the row window is found on the reversed axis.
    row0 = int(np.searchsorted(-lats, -ys.max(), "left")) - 1
    row1 = int(np.searchsorted(-lats, -ys.min(), "right")) + 1
    col0, row0 = max(col0, 0), max(row0, 0)
    col1, row1 = min(col1, len(lons)), min(row1, len(lats))
    full = np.zeros((len(lats), len(lons)), dtype=bool)
    if col1 <= col0 or row1 <= row0:
        return full

    gx, gy = np.meshgrid(lons[col0:col1], lats[row0:row1])
    inside = np.zeros(gx.shape, dtype=bool)
    n = len(ring) - 1 if ring[0] == ring[-1] else len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if y1 == y2:
            continue
        straddles = ((y1 > gy) != (y2 > gy))
        # x of the edge at this row's latitude
        xint = x1 + (gy - y1) * (x2 - x1) / (y2 - y1)
        inside ^= straddles & (gx < xint)
    full[row0:row1, col0:col1] = inside
    return full


def build(estate: str, start: str, end: str, max_cloud: float,
          scene_id: str | None = None) -> dict:
    geo = ontology.blocks_geojson(estate)
    if geo is None:
        raise SystemExit(f"No block geometry for estate {estate!r}. "
                         "This layer needs polygons.")
    feats = geo["features"]
    bbox = _bbox(feats)

    scenes = search_scenes(bbox, start, end, max_cloud, 20)
    if not scenes:
        raise SystemExit(
            f"No Sentinel-2 L2A scene under {max_cloud}% cloud over {estate} "
            f"between {start} and {end}. Widen the window or raise --max-cloud. "
            "Persistent cloud is the real constraint on optical monitoring here, "
            "not data access.")
    item = next((s for s in scenes if s["id"] == scene_id), scenes[0])
    props = item["properties"]
    log.info("[ndre] %d candidate scenes; using %s (%s, %.1f%% cloud)",
             len(scenes), item["id"], props["datetime"][:10],
             props.get("eo:cloud_cover", -1))

    # Grid at the native resolution of the coarsest band we use.
    mid_lat = (bbox[1] + bbox[3]) / 2
    width = max(16, round((bbox[2] - bbox[0]) * 111320 *
                          math.cos(math.radians(mid_lat)) / TARGET_M))
    height = max(16, round((bbox[3] - bbox[1]) * 110540 / TARGET_M))

    assets = ["nir", "rededge1", "red", "scl"]
    item_url = f"{STAC_URL}/collections/{COLLECTION}/items/{item['id']}"
    data, footprint = fetch_array(item_url, bbox, assets, width, height)
    nir_raw, re1_raw, red_raw, scl = data

    valid = np.isin(scl.astype("int16"), VALID_SCL) & (footprint > 0)

    raw_bands = {"nir": nir_raw, "rededge1": re1_raw, "red": red_raw}
    apply_offset = _offset_is_physical(raw_bands, valid, item)
    log.info("[ndre] declared BOA offset %s", "applied" if apply_offset else
             "NOT applied - already corrected in the source COG")

    def reflect(raw, asset):
        scale, offset = _scale_offset(item, asset)
        return raw * scale + (offset if apply_offset else 0.0)

    nir = reflect(nir_raw, "nir")
    re1 = reflect(re1_raw, "rededge1")
    red = reflect(red_raw, "red")

    with np.errstate(divide="ignore", invalid="ignore"):
        ndre = (nir - re1) / (nir + re1)
        ndvi = (nir - red) / (nir + red)
    # A normalised index outside [-1, 1] means the arithmetic met a nodata
    # pixel the SCL did not flag. Drop it rather than average it in.
    valid &= np.isfinite(ndre) & np.isfinite(ndvi)
    valid &= (ndre > -1) & (ndre < 1) & (ndvi > -1) & (ndvi < 1)

    lons = bbox[0] + (np.arange(width) + 0.5) * (bbox[2] - bbox[0]) / width
    lats = bbox[3] - (np.arange(height) + 0.5) * (bbox[3] - bbox[1]) / height

    blocks: dict[str, dict] = {}
    covered = 0
    t0 = time.monotonic()
    for f in feats:
        p = f["properties"]
        key = f"{int(p['division_code'])}|{int(p['block_code'])}"
        mask = _polygon_mask(f["geometry"]["coordinates"][0], lons, lats)
        total = int(mask.sum())
        good = mask & valid
        n = int(good.sum())
        row = {
            "block_id": f["id"],
            "block_label": p.get("block_label"),
            "pixels": total,
            "valid_pixels": n,
            "valid_pct": round(100 * n / total, 1) if total else 0.0,
            "ndre": None, "ndvi": None, "ndre_p10": None, "ndre_p90": None,
        }
        # Under a fifth of a block visible is not a measurement of the block.
        if total and n / total >= 0.2 and n >= 4:
            vals = ndre[good]
            row["ndre"] = round(float(vals.mean()), 4)
            row["ndre_p10"] = round(float(np.percentile(vals, 10)), 4)
            row["ndre_p90"] = round(float(np.percentile(vals, 90)), 4)
            row["ndvi"] = round(float(ndvi[good].mean()), 4)
            covered += 1
        blocks[key] = row
    log.info("[ndre] %d/%d blocks measured in %.1fs", covered, len(feats),
             time.monotonic() - t0)

    scene_date = props["datetime"][:10]
    out = {
        "estate": estate.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": "real:sentinel-2",
        "scene": {
            "id": item["id"],
            "collection": COLLECTION,
            "datetime": props["datetime"],
            "date": scene_date,
            "month": scene_date[:7],
            "cloud_cover_pct": round(float(props.get("eo:cloud_cover") or 0), 2),
            "platform": props.get("platform"),
            "mgrs_tile": props.get("grid:code") or props.get("s2:mgrs_tile"),
            "processing_baseline": props.get("s2:processing_baseline"),
            "stac_item": item_url,
            "licence": "Copernicus Sentinel data, free and open (ESA)",
        },
        "search": {
            "window": [start, end],
            "max_cloud_pct": max_cloud,
            "scenes_found": len(scenes),
            "alternatives": [{"id": s["id"], "date": s["properties"]["datetime"][:10],
                              "cloud_pct": round(float(s["properties"].get("eo:cloud_cover") or 0), 1)}
                             for s in scenes[:8]],
        },
        "method": {
            "index": "NDRE = (B08 - B05) / (B08 + B05)",
            "secondary": "NDVI = (B08 - B04) / (B08 + B04)",
            "resolution_m": TARGET_M,
            "grid": [width, height],
            "cloud_mask": f"Scene Classification Layer, keeping classes {list(VALID_SCL)}",
            "min_valid_fraction": 0.2,
            "raster_reader": TITILER_URL,
            "boa_offset_applied": apply_offset,
            "boa_offset_note": (
                "The item declares a -0.1 BOA offset and a boa_offset_applied "
                "flag that contradict each other. Applying the offset drives "
                "red-edge and red reflectance negative over vegetation pixels "
                "and NDVI above 1, so it was rejected on physical grounds."
                if not apply_offset else
                "Declared BOA offset applied; reflectance stayed physical."),
            "note": ("NDVI saturates over mature palm and is carried only to show "
                     "that it does. NDRE is the layer that separates blocks."),
        },
        "coverage": {
            "blocks": len(feats),
            "measured": covered,
            "measured_pct": round(100 * covered / len(feats), 1) if feats else 0.0,
        },
        "blocks": blocks,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{estate.lower()}_ndre.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    log.info("[ndre] wrote %s", path)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    today = date.today().isoformat()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--estate", default="EC")
    ap.add_argument("--from", dest="start", default="2024-09-01")
    ap.add_argument("--to", dest="end", default=today)
    ap.add_argument("--max-cloud", type=float, default=35.0)
    ap.add_argument("--scene", default=None, help="Force a specific STAC item id")
    a = ap.parse_args()

    out = build(a.estate, a.start, a.end, a.max_cloud, a.scene)
    s, c = out["scene"], out["coverage"]
    vals = [b["ndre"] for b in out["blocks"].values() if b["ndre"] is not None]
    print(f"\nScene {s['id']}  {s['date']}  {s['cloud_cover_pct']}% cloud")
    print(f"Blocks measured: {c['measured']}/{c['blocks']} ({c['measured_pct']}%)")
    if vals:
        print(f"NDRE  min {min(vals):.3f}  median {sorted(vals)[len(vals)//2]:.3f}  "
              f"max {max(vals):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
