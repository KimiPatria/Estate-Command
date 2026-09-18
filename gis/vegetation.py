"""UC-03 - real canopy vigour, read from the Sentinel-2 layer built offline.

gis/build_ndre.py does the satellite work and writes gis/data/{estate}_ndre.json.
This module is the read side: no network, no raster libraries, just the file
and the questions the map and the copilot ask of it.

The point of this layer is that it is the only one on the page whose numbers
came from neither the client's export nor a generator. Everything else on
Estate Command is either the client's data or an invention labelled as one.
This is a third thing - a real measurement of the client's own blocks that the
client does not currently possess - and it exists because the polygons are
WGS84 and Copernicus gives the imagery away.

Where a block is under cloud the value is null and the map leaves it grey.
There is no interpolation. A satellite that could not see a block has nothing
to say about it.
"""

import json
import logging
from pathlib import Path
from threading import Lock

log = logging.getLogger("estate-command.vegetation")

_DIR = Path(__file__).parent / "data"
_CACHE: dict = {}
_LOCK = Lock()


def _load(estate: str) -> dict | None:
    path = _DIR / f"{estate.lower()}_ndre.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("[vegetation] %s unreadable: %s", path, exc)
        return None


def _state(estate: str = "EC") -> dict | None:
    key = estate.upper()
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        data = _load(key)
        _CACHE[key] = data
        if data:
            c = data["coverage"]
            log.info("[vegetation] %s: scene %s, %d/%d blocks measured",
                     key, data["scene"]["id"], c["measured"], c["blocks"])
        return data


def reload_vegetation() -> None:
    with _LOCK:
        _CACHE.clear()


def available(estate: str = "EC") -> bool:
    return _state(estate) is not None


def scene(estate: str = "EC") -> dict | None:
    st = _state(estate)
    return st["scene"] if st else None


def by_block(estate: str = "EC") -> dict:
    """{division|block: row}. Empty when no scene has been pulled."""
    st = _state(estate)
    return st["blocks"] if st else {}


def month(estate: str = "EC") -> str | None:
    """The month the scene belongs to, for lining the layer up with the scrubber."""
    s = scene(estate)
    return s["month"] if s else None


def summary(estate: str = "EC") -> dict:
    """Scene, coverage, distribution, and the blocks at the weak end.

    Also carries the NDVI comparison, because the reason this layer uses the
    red edge is worth showing rather than asserting: over mature palm NDVI
    collapses into a narrow band while NDRE keeps its spread.
    """
    st = _state(estate)
    if not st:
        return {
            "available": False,
            "reason": ("No Sentinel-2 scene has been pulled for this estate. "
                       "Run: python -m gis.build_ndre --estate " + estate.upper()),
        }

    rows = [dict(r, key=k) for k, r in st["blocks"].items()]
    ndre = sorted(r["ndre"] for r in rows if r["ndre"] is not None)
    ndvi = sorted(r["ndvi"] for r in rows if r["ndvi"] is not None)

    def spread(vals):
        if not vals:
            return None
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return {
            "min": round(vals[0], 4),
            "median": round(vals[len(vals) // 2], 4),
            "max": round(vals[-1], 4),
            "range": round(vals[-1] - vals[0], 4),
            # Coefficient of variation: the honest way to compare the spread of
            # two indices that live on different scales.
            "cv_pct": round(100 * (var ** 0.5) / mean, 2) if mean else None,
        }

    weakest = sorted((r for r in rows if r["ndre"] is not None),
                     key=lambda r: r["ndre"])[:10]
    return {
        "available": True,
        "estate": st["estate"],
        "scene": st["scene"],
        "coverage": st["coverage"],
        "method": st["method"],
        "search": st.get("search", {}),
        "generated_at": st["generated_at"],
        "ndre": spread(ndre),
        "ndvi": spread(ndvi),
        "index_choice": _index_note(spread(ndre), spread(ndvi)),
        "weakest_blocks": [{"block": r["block_label"], "key": r["key"],
                            "ndre": r["ndre"], "ndvi": r["ndvi"],
                            "valid_pct": r["valid_pct"]} for r in weakest],
        "clouded_blocks": [r["block_label"] for r in rows if r["ndre"] is None],
    }


def _index_note(ndre_spread, ndvi_spread) -> str:
    """Say what the two indices actually did here, not what the textbook says.

    NDRE is used because NDVI saturates over mature palm. That is the reason
    for the choice, but it is a claim, and this estate's own numbers either
    support it or they do not. Quote the ratio and let it speak.
    """
    if not ndre_spread or not ndvi_spread:
        return "Not enough measured blocks to compare the two indices."
    a, b = ndre_spread.get("cv_pct"), ndvi_spread.get("cv_pct")
    head = (f"Across measured blocks NDVI spans {ndvi_spread['range']:.3f} "
            f"({b}% CV) and NDRE spans {ndre_spread['range']:.3f} ({a}% CV). ")
    if not a or not b:
        return head
    ratio = a / b
    if ratio >= 1.2:
        return head + (
            f"The red edge carries {ratio:.1f}x the relative variation, which is "
            "the saturation NDRE is chosen to avoid: it separates blocks that "
            "NDVI reports as the same.")
    return head + (
        "The two are closer than the textbook expects, so on this scene NDVI "
        "is less saturated than usual. The map still colours by the red edge, "
        "but the gap between the two indices is not doing much work here.")


def readiness_evidence(estate: str = "EC") -> dict | None:
    """What the UC-15 register should say now the layer actually runs."""
    st = _state(estate)
    if not st:
        return None
    s, c = st["scene"], st["coverage"]
    alts = (st.get("search") or {}).get("scenes_found")
    return {
        "status": "ready",
        "evidence": (
            f"Running. Sentinel-2 L2A scene {s['id']} ({s['date']}, "
            f"{s['cloud_cover_pct']}% scene cloud) was pulled from the free "
            f"Copernicus archive and NDRE computed for {c['measured']} of "
            f"{c['blocks']} EC blocks at {st['method']['resolution_m']} m, "
            f"cloud-masked with the scene's own classification band. Nothing "
            f"was requested from the client to make this work."),
        "degrades_to": (
            "Sentinel-1 SAR where cloud blocks optical. Cloud is the binding "
            f"constraint here, not access: only {alts} scene(s) cleared the "
            "cloud threshold over this estate in a full year of archive."
            if alts is not None else "Sentinel-1 SAR where cloud blocks optical."),
        "ask": ("None for the layer itself. To turn a vigour anomaly into a "
                "diagnosis, the ask is ground truth: which blocks the "
                "agronomists already know to be stressed, so the anomaly can "
                "be calibrated against something."),
    }
