"""
Estate block-level spatial geometry — GeoJSON conversion.

Reads forecast/<ESTATE>/<ESTATE>_overlay.csv (an EPMS block-boundary export:
one row per block, a WGS84 [lon, lat] polygon ring, and a JSON properties
blob with planting year / seed variety / tree count / area) and turns it
into a standard GeoJSON FeatureCollection for the Block Map.

No live database access and no model — this is a spatial lookup only, kept
self-contained like monthly_model.py so it can be imported by path or by
module name without pulling in the rest of the app. `load_block_geometry`
returns None (not an error) when an estate has no overlay file, so callers
render an honest "no geometry for this estate" state instead of guessing.
"""

import csv
import json
import logging
from pathlib import Path
from threading import Lock

log = logging.getLogger("epms-forecast")

_DIR = Path(__file__).parent

# In-process cache — the overlay file is static per estate, so there's no
# reason to re-read and re-parse a ~700KB CSV on every scope-chip click.
_CACHE: dict = {}
_LOCK = Lock()


def _overlay_path(estate_id: str) -> Path:
    code = estate_id.upper()
    return _DIR / code / f"{code}_overlay.csv"


def has_block_geometry(estate_id: str) -> bool:
    """Cheap existence check for the scope payload — no parsing."""
    return _overlay_path(estate_id).exists()


def _parse(path: Path, estate_id: str) -> dict:
    features = []
    skipped = 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ring = json.loads(row["overlay_coordinates"])
                props = json.loads(row.get("overlay_properties") or "{}")
            except (KeyError, TypeError, json.JSONDecodeError):
                skipped += 1
                continue
            if not isinstance(props, dict):
                props = {}
            # Raw CSV join keys, kept alongside (not instead of) the richer
            # EPMS properties blob (Blok/Divisi/TT/...) in case the two ever
            # need reconciling — never overwrites an existing properties key.
            props.setdefault("_block_code", row.get("overlay_block_code"))
            props.setdefault("_division_code", row.get("overlay_division_code"))
            props.setdefault("_section_code", row.get("overlay_section_code"))
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": props,
            })
    if skipped:
        log.warning("[forecast.blocks] %s: skipped %d malformed row(s)", estate_id, skipped)
    return {
        "type": "FeatureCollection",
        "estate": estate_id,
        "block_count": len(features),
        "features": features,
    }


def load_block_geometry(estate_id: str) -> dict | None:
    """FeatureCollection of block polygons for `estate_id`, or None if the
    estate has no overlay file. Cached in-process after the first parse."""
    estate_id = estate_id.lower()
    with _LOCK:
        if estate_id in _CACHE:
            return _CACHE[estate_id]
        path = _overlay_path(estate_id)
        if not path.exists():
            _CACHE[estate_id] = None
            return None
        geo = _parse(path, estate_id)
        _CACHE[estate_id] = geo
        return geo
