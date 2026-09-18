"""Synthetic fire-response assets for estate EC.

Run once:  python gis/build_fire_assets.py
Writes:    gis/data/ec_fire_assets.geojson

Why this file is synthetic, and says so
---------------------------------------
EPMS records no fire infrastructure. There is no table in the 138-table schema
that can hold a fire post, a water source, a pump, or a standby crew, and the
EC export carries none either. Every feature written here is invented.

It is invented to be *checkable*, not merely plausible. Placement is derived
from the estate's real ArcGIS geometry - posts sit at real division centroids
snapped to real block corners, where estate roads actually run - and the
staffing and equipment figures follow the shape of an Indonesian estate
operating under ISPO fire-control obligations:

  * Pos Induk (main post) at the estate office, ~15 crew, water tanker
  * Pos Divisi (satellite posts), ~6-8 crew, floating pumps and hose
  * Menara Api (watch towers), 1-2 spotters, no suppression capability
  * Embung (reservoirs) dug specifically as fire water, plus drainage canals

Every feature carries provenance "synthetic" and a `would_come_from` field
naming what would replace it in a real deployment. The map renders synthetic
assets in a different colour from real geometry, and the readiness panel keeps
the gap visible. This is the "instrument the gaps" rule from UC-15: inventing
the asset for a demo is fine, hiding that it was invented is not.
"""

import json
import logging
import math
import random
from pathlib import Path

log = logging.getLogger("estate-command.fire-assets")

OUT = Path(__file__).parent / "data" / "ec_fire_assets.geojson"

# Fixed seed: the demo must land on the same map every time it is run.
SEED = 20260910

# Indonesian estate fire-response norms, used to size the invented assets.
CREW_MAIN = 15          # Regu Pemadam Kebakaran at a Pos Induk
CREW_SATELLITE = (6, 8)
TANKER_LITRES = 5000
PUMP_LPM = 500          # pompa apung, litres per minute
EMBUNG_M3 = (1800, 4200)


def _centroid(ring):
    n = len(ring) - 1  # ring is closed
    return (sum(p[0] for p in ring[:n]) / n, sum(p[1] for p in ring[:n]) / n)


def _dist_km(a, b):
    lat0 = math.radians((a[1] + b[1]) / 2)
    return math.hypot((b[0] - a[0]) * 111.32 * math.cos(lat0), (b[1] - a[1]) * 111.32)


def _nearest_block_corner(blocks, target):
    """Snap an invented asset onto a real block vertex.

    Block corners are where estate roads meet, so a post placed on one is
    reachable by a tanker. Placing assets at arbitrary interior coordinates
    would put them in the middle of a palm stand.
    """
    best, best_d = None, 1e9
    for f in blocks:
        for pt in f["geometry"]["coordinates"][0]:
            d = _dist_km(pt, target)
            if d < best_d:
                best, best_d = pt, d
    return [round(best[0], 6), round(best[1], 6)]


def build(blocks_fc: dict, divisions_fc: dict) -> dict:
    rng = random.Random(SEED)
    blocks = blocks_fc["features"]
    divisions = divisions_fc["features"]
    feats = []

    est_centre = _centroid(
        _convex_ring([p for f in blocks for p in f["geometry"]["coordinates"][0]])
    )

    # -- Pos Induk: the main fire station, at the estate office ------------
    main_pt = _nearest_block_corner(blocks, est_centre)
    feats.append({
        "type": "Feature",
        "id": "firepost:EC:induk",
        "geometry": {"type": "Point", "coordinates": main_pt},
        "properties": {
            "entity": "fire_post",
            "estate_code": "EC",
            "kind": "pos_induk",
            "name": "Pos Induk EC",
            "label": "Main fire post",
            "crew": CREW_MAIN,
            "crew_on_shift": CREW_MAIN,
            "tanker_litres": TANKER_LITRES,
            "pumps": 3,
            "pump_lpm": PUMP_LPM,
            "response_kmh": 30,
            "provenance": "synthetic",
            "would_come_from": "Estate HSE register / ISPO fire-control plan",
        },
    })

    # -- Pos Divisi: one satellite post per division ------------------------
    for i, dv in enumerate(divisions):
        div = dv["properties"]["division_code"]
        c = _centroid(dv["geometry"]["coordinates"][0])
        # Offset off the division centroid so posts sit on the road edge
        # rather than dead centre of a stand, then snap to a real corner.
        jitter = (c[0] + rng.uniform(-0.004, 0.004), c[1] + rng.uniform(-0.004, 0.004))
        pt = _nearest_block_corner(blocks, jitter)
        crew = rng.randint(*CREW_SATELLITE)
        # Night shift runs thinner than day shift, as it does in practice.
        on_shift = max(2, int(crew * rng.uniform(0.45, 0.75)))
        feats.append({
            "type": "Feature",
            "id": f"firepost:EC:div{div}",
            "geometry": {"type": "Point", "coordinates": pt},
            "properties": {
                "entity": "fire_post",
                "estate_code": "EC",
                "division_code": div,
                "kind": "pos_divisi",
                "name": f"Pos Divisi {div}",
                "label": f"Division {div} fire post",
                "crew": crew,
                "crew_on_shift": on_shift,
                "tanker_litres": 0,
                "pumps": rng.randint(1, 2),
                "pump_lpm": PUMP_LPM,
                "response_kmh": 25,
                "provenance": "synthetic",
                "would_come_from": "Estate HSE register / ISPO fire-control plan",
            },
        })

    # -- Menara Api: watch towers, spotting only ---------------------------
    # Merauke is flat, so towers earn their placement by sightline over the
    # estate edge rather than by elevation.
    for i, dv in enumerate(divisions[::2]):
        div = dv["properties"]["division_code"]
        c = _centroid(dv["geometry"]["coordinates"][0])
        ang = rng.uniform(0, 2 * math.pi)
        edge = (c[0] + 0.010 * math.cos(ang), c[1] + 0.010 * math.sin(ang))
        feats.append({
            "type": "Feature",
            "id": f"firetower:EC:{div}",
            "geometry": {"type": "Point", "coordinates": _nearest_block_corner(blocks, edge)},
            "properties": {
                "entity": "fire_tower",
                "estate_code": "EC",
                "division_code": div,
                "kind": "menara_api",
                "name": f"Menara Api {div}",
                "label": f"Watch tower, division {div}",
                "height_m": rng.choice([12, 15, 18]),
                "spotters_on_shift": rng.choice([1, 1, 2]),
                "suppression": False,
                "provenance": "synthetic",
                "would_come_from": "Estate HSE register",
            },
        })

    # -- Embung: reservoirs dug as fire water ------------------------------
    for i, dv in enumerate(divisions):
        if i % 2:
            continue
        div = dv["properties"]["division_code"]
        c = _centroid(dv["geometry"]["coordinates"][0])
        ang = rng.uniform(0, 2 * math.pi)
        p = (c[0] + 0.006 * math.cos(ang), c[1] + 0.006 * math.sin(ang))
        cap = rng.randint(*EMBUNG_M3)
        feats.append({
            "type": "Feature",
            "id": f"water:EC:embung{div}",
            "geometry": {"type": "Point", "coordinates": _nearest_block_corner(blocks, p)},
            "properties": {
                "entity": "water_source",
                "estate_code": "EC",
                "division_code": div,
                "kind": "embung",
                "name": f"Embung Divisi {div}",
                "label": f"Reservoir, division {div}",
                "capacity_m3": cap,
                # Dry-season drawdown: an embung is not full in September.
                "usable_m3": int(cap * rng.uniform(0.45, 0.75)),
                "pump_access": True,
                "provenance": "synthetic",
                "would_come_from": "Estate civil / water management records",
            },
        })

    # -- Parit induk: the main drainage canal, always-wet water source -----
    # Drawn along the estate's long axis, which in a flat Merauke estate is
    # where the primary drain actually runs.
    pts = [p for f in blocks for p in f["geometry"]["coordinates"][0]]
    w = min(p[0] for p in pts); e = max(p[0] for p in pts)
    s = min(p[1] for p in pts); n = max(p[1] for p in pts)
    canal = [[round(w + (e - w) * t, 6),
              round(s + (n - s) * (0.42 + 0.10 * math.sin(t * 5.0)), 6)]
             for t in [i / 24 for i in range(25)]]
    feats.append({
        "type": "Feature",
        "id": "water:EC:parit-induk",
        "geometry": {"type": "LineString", "coordinates": canal},
        "properties": {
            "entity": "water_source",
            "estate_code": "EC",
            "kind": "parit_induk",
            "name": "Parit Induk",
            "label": "Main drainage canal",
            "length_km": round(sum(_dist_km(canal[i], canal[i + 1])
                                   for i in range(len(canal) - 1)), 2),
            "pump_access": True,
            # A canal is not a tank. It has no fixed capacity but it does have
            # a draw rate, and unlike an embung it does not run dry mid-shift.
            "continuous": True,
            "capacity_m3": None,
            "usable_m3": None,
            "draw_rate_lpm": PUMP_LPM * 2,
            "provenance": "synthetic",
            "would_come_from": "Estate drainage network survey",
        },
    })

    log.info("[fire-assets] generated %d synthetic assets", len(feats))
    return {"type": "FeatureCollection", "features": feats}


def _convex_ring(points):
    """Closed hull ring, reused here only to find the estate centre."""
    pts = sorted(set((round(x, 7), round(y, 7)) for x, y in points))

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

    lower, upper = half(pts), half(list(reversed(pts)))
    ring = [list(p) for p in lower[:-1] + upper[:-1]]
    ring.append(list(ring[0]))
    return ring


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from gis import ontology

    fc = build(ontology.blocks_geojson("EC"), ontology.divisions_geojson("EC"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc), encoding="utf-8")
    by_kind: dict[str, int] = {}
    for f in fc["features"]:
        k = f["properties"]["kind"]
        by_kind[k] = by_kind.get(k, 0) + 1
    for k, v in sorted(by_kind.items()):
        print(f"  {k:14s} {v}")
    print(f"wrote {OUT} ({len(fc['features'])} synthetic assets)")
