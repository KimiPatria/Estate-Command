"""Loose fruit recovery - the one harvesting loss the export already counts.

Every harvest record in the EC export carries a `loose_fruits` column: the
detached fruitlets picked up at the palm beside the bunches cut. Until this
module the app read the bunch columns and dropped the count, and the
readiness register said the export carried none. It carries 11.6 million.

Loose fruit matters out of proportion to its weight. The fruitlets that
detach are the ripest on the bunch, so they carry the highest oil content of
anything the estate sends to the mill, and they are the easiest crop to leave
behind: picked up by hand, after the bunch, at the end of a harvester's task.
Recovery is a discipline question, and the export measures it per block per
day.

A recovery rate needs a benchmark, and there is no literature figure a client
would accept for their own ground. The benchmark here is measured: the ratio
the estate's own upper quartile of blocks already reaches. A block below it
is leaving fruit behind, cutting less ripe, or not writing the count down.
The payload says so, and all three are worth the walk.

Counts, not kilograms. Turning the gap into tonnes and rupiah needs a fruit
weight nothing here measures, so it is a module constant labelled as a
placeholder, priced at the FFB rate the assumption register holds.

Computed once per estate and cached; the endpoint answers from the cache.
"""

import logging
from collections import defaultdict
from threading import Lock

from gis import assumptions, layers, ontology

log = logging.getLogger("estate-command.loose_fruit")

_CACHE: dict = {}
_LOCK = Lock()

# PLACEHOLDER. The export counts loose fruit and weighs nothing. A tenera
# fruitlet runs roughly 8-15 g; 12 g sits in the middle of that band and is
# not a measurement from this estate. Every kilogram and rupiah below is this
# constant times a real count, and the payload labels it so.
KG_PER_LOOSE_FRUIT = 0.012
KG_PER_LOOSE_FRUIT_PROVENANCE = ("placeholder: not measured on this estate. "
                                 "The export counts fruits and weighs nothing.")

# A block-month with fewer bunches than this gives a ratio too noisy to rank a
# fall on. The window minimum is ~16,000 bunches per block, so this only
# trims a partial month.
_MIN_MONTH_BUNCHES = 200

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _month_long(m: str) -> str:
    try:
        return _MONTHS[int(m[5:7]) - 1]
    except (ValueError, IndexError):
        return m


def _month_short(m: str) -> str:
    return _month_long(m)[:3]


def _quantile(vals: list, q: float):
    """Linear-interpolated quantile of an ascending list."""
    if not vals:
        return None
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _fruits(n: float) -> str:
    """A fruit count a sentence can carry."""
    if n >= 1e6:
        return f"{n / 1e6:.1f} million"
    if n >= 1e4:
        return f"{round(n, -3):,.0f}"
    return f"{n:,.0f}"


# ── the measurement, once ──────────────────────────────────────────────────

def _measure(estate: str) -> dict | None:
    geo = ontology.blocks_geojson(estate)
    if geo is None:
        return None
    idx = next((e for e in ontology.estate_index() if e["estate_code"] == estate), None)
    window = (idx or {}).get("harvest_window") or {}
    months = list(window.get("months") or [])

    blocks = []
    for f in geo["features"]:
        p = f["properties"]
        b = p.get("bunches_total") or 0
        if not b:
            continue
        lf = p.get("loose_fruits") or 0
        bm = p.get("bunches_by_month") or {}
        lm = p.get("loose_by_month") or {}
        recs = p.get("harvest_records") or 0
        blocks.append({
            "block_id": f["id"],
            "division_code": p["division_code"],
            "block_code": p["block_code"],
            "block_label": p.get("block_label"),
            "planted_ha": p.get("planted_ha"),
            "palm_age_years": p.get("palm_age_years"),
            "bunches": b,
            "loose_fruits": lf,
            "loose_per_bunch": round(lf / b, 3),
            "ratio_by_month": {
                m: (round(lm.get(m, 0) / bm[m], 3)
                    if (bm.get(m) or 0) >= _MIN_MONTH_BUNCHES else None)
                for m in months
            },
            "bunches_by_month": {m: bm.get(m, 0) for m in months},
            "loose_by_month": {m: lm.get(m, 0) for m in months},
            "records": recs,
            "records_with_count": p.get("loose_records") or 0,
            "records_with_count_pct": (round(100 * (p.get("loose_records") or 0) / recs, 1)
                                       if recs else None),
        })
    if not blocks:
        return None

    # The estate, and the distribution the benchmark is read from.
    tot_b = sum(r["bunches"] for r in blocks)
    tot_lf = sum(r["loose_fruits"] for r in blocks)
    # An export with the column absent or all zero has nothing to benchmark
    # against; say so rather than dividing by it.
    if not tot_lf:
        return None
    tot_rec = sum(r["records"] for r in blocks)
    tot_rec_lf = sum(r["records_with_count"] for r in blocks)
    ratios = sorted(r["loose_per_bunch"] for r in blocks)
    q1, med, q3 = (_quantile(ratios, 0.25), _quantile(ratios, 0.5), _quantile(ratios, 0.75))
    bench = round(q3, 3)

    gap_total = 0.0
    for r in blocks:
        r["recovery_pct"] = round(100 * r["loose_per_bunch"] / bench, 1) if bench else None
        short = max(0.0, bench - r["loose_per_bunch"]) * r["bunches"]
        r["gap_fruits"] = round(short)
        gap_total += short
    below = [r for r in blocks if r["loose_per_bunch"] < bench]

    # Recording, split by quartile: is the count missing where the ratio is
    # low? Weighted by records so a small block does not swing it.
    def rec_pct(rs):
        n = sum(r["records"] for r in rs)
        return round(100 * sum(r["records_with_count"] for r in rs) / n, 1) if n else None
    bottom_q = [r for r in blocks if r["loose_per_bunch"] <= q1]
    top_q = [r for r in blocks if r["loose_per_bunch"] >= q3]

    # Months, over the whole estate.
    by_month = []
    for m in months:
        mb = sum(r["bunches_by_month"][m] for r in blocks)
        ml = sum(r["loose_by_month"][m] for r in blocks)
        by_month.append({
            "month": m, "label": _month_short(m),
            "bunches": mb, "loose_fruits": ml,
            "loose_per_bunch": round(ml / mb, 3) if mb else None,
            "blocks": sum(1 for r in blocks if r["bunches_by_month"][m]),
        })

    # Divisions.
    div: dict = defaultdict(lambda: {"blocks": 0, "bunches": 0, "loose_fruits": 0, "below": 0})
    for r in blocks:
        a = div[r["division_code"]]
        a["blocks"] += 1
        a["bunches"] += r["bunches"]
        a["loose_fruits"] += r["loose_fruits"]
        a["below"] += 1 if r["loose_per_bunch"] < bench else 0
    by_division = []
    for code in sorted(div, key=lambda c: int(c) if str(c).isdigit() else 0):
        a = div[code]
        ratio = round(a["loose_fruits"] / a["bunches"], 3) if a["bunches"] else None
        by_division.append({
            "division_code": code, "label": f"Division {code}",
            "blocks": a["blocks"], "bunches": a["bunches"], "loose_fruits": a["loose_fruits"],
            "loose_per_bunch": ratio,
            "recovery_pct": round(100 * ratio / bench, 1) if (ratio and bench) else None,
            "blocks_below_benchmark": a["below"],
        })

    # The fall from the first month to the last, block by block.
    first, last = (months[0], months[-1]) if len(months) >= 2 else (None, None)
    falls = []
    if first and last:
        for r in blocks:
            a, z = r["ratio_by_month"].get(first), r["ratio_by_month"].get(last)
            if a is None or z is None:
                continue
            falls.append({**r, "ratio_first": a, "ratio_last": z, "delta": round(z - a, 3)})
        falls.sort(key=lambda r: r["delta"])

    return {
        "window": {"from": window.get("from"), "to": window.get("to"), "months": months,
                   "first_month": first, "last_month": last},
        "blocks": blocks,
        "totals": {
            "blocks": len(blocks),
            "bunches": tot_b,
            "loose_fruits": tot_lf,
            "loose_per_bunch": round(tot_lf / tot_b, 3) if tot_b else None,
            "records": tot_rec,
            "records_with_count": tot_rec_lf,
            "records_with_count_pct": round(100 * tot_rec_lf / tot_rec, 1) if tot_rec else None,
        },
        "distribution": {
            "min": ratios[0], "q1": round(q1, 3), "median": round(med, 3),
            "q3": round(q3, 3), "max": ratios[-1], "n": len(ratios),
            "basis": "loose fruits per bunch, one value per block over the window",
        },
        "benchmark": {
            "loose_per_bunch": bench,
            "basis": ("measured on this estate: the 75th percentile of block ratios "
                      "over the window - the rate a quarter of the blocks already "
                      "reach or exceed. Not a literature figure."),
            "recovery_pct": round(100 * (tot_lf / tot_b) / bench, 1) if (tot_b and bench) else None,
            "blocks_below": len(below),
            "blocks_at_or_above": len(blocks) - len(below),
            "gap_fruits": round(gap_total),
        },
        "recording": {
            "bottom_quarter_pct": rec_pct(bottom_q),
            "top_quarter_pct": rec_pct(top_q),
        },
        "by_month": by_month,
        "by_division": by_division,
        "falls": falls,
    }


def _measured(estate: str) -> dict | None:
    with _LOCK:
        if estate in _CACHE:
            return _CACHE[estate]
        m = _measure(estate)
        _CACHE[estate] = m
        if m:
            log.info("[loose_fruit] %s: %d blocks, %.3f loose per bunch, benchmark %.3f",
                     estate, m["totals"]["blocks"], m["totals"]["loose_per_bunch"],
                     m["benchmark"]["loose_per_bunch"])
        return m


def reload_loose_fruit() -> None:
    with _LOCK:
        _CACHE.clear()


# ── the synthetic feed, kept apart ─────────────────────────────────────────

def _synthetic_comparison() -> dict | None:
    """The generated per-worker loose_fruit_kg, for scale only.

    Never combined with the real counts: the two are different units from
    different worlds, and the panel shows this one with its own label.
    """
    try:
        wk = layers._state().get("worker") or {}
    except Exception:          # the real layer must not depend on the synthetic one
        return None
    if not wk:
        return None
    kg = sum(v["loose_kg"] for v in wk.values())
    b = sum(v["bunches"] for v in wk.values())
    return {
        "provenance": "synthetic",
        "source": "gis/data/synthetic/ec_harvester_day.csv, per-worker loose_fruit_kg",
        "loose_kg": round(kg),
        "loose_t": round(kg / 1000, 1),
        "bunches": b,
        "kg_per_bunch": round(kg / b, 3) if b else None,
        "blocks": len(wk),
        "note": ("Generated crew feed, in kilograms. Shown beside the real counts "
                 "for scale only and never combined with them."),
    }


# ── the position, per call ─────────────────────────────────────────────────

def _pack(r: dict, *fields) -> dict:
    out = {"block_label": r["block_label"], "division_code": r["division_code"],
           "block_code": r["block_code"], "block_id": r["block_id"]}
    for f in fields:
        out[f] = r.get(f)
    return out


def position(estate: str = "EC", top: int = 12) -> dict:
    """Loose fruit recovery per block, against the estate's own upper quartile."""
    estate = (estate or "EC").upper()
    m = _measured(estate)
    if not m:
        return {"available": False, "estate": estate,
                "reason": f"No harvest export with loose fruit counts for estate {estate}."}
    top = max(1, min(int(top or 12), 50))

    t, bench, dist, w = m["totals"], m["benchmark"], m["distribution"], m["window"]
    ratio = t["loose_per_bunch"]
    blocks = m["blocks"]

    # Priced per call, off the register, so an edited price reprices this.
    price = assumptions.values(["ffb_price_idr_kg"])["ffb_price_idr_kg"]
    gap_kg = bench["gap_fruits"] * KG_PER_LOOSE_FRUIT
    gap_idr = gap_kg * price

    worst = sorted(blocks, key=lambda r: r["loose_per_bunch"])[:top]
    best = sorted(blocks, key=lambda r: -r["loose_per_bunch"])[:top]
    falls = [r for r in m["falls"] if r["delta"] < 0][:top]

    first, last = w["first_month"], w["last_month"]
    bm = m["by_month"]
    trend = None
    if len(bm) >= 2 and bm[0]["loose_per_bunch"] and bm[-1]["loose_per_bunch"]:
        peak = max(bm, key=lambda x: x["loose_per_bunch"] or 0)
        trend = (f"{bm[0]['loose_per_bunch']:.2f} in {_month_long(bm[0]['month'])} to "
                 f"{bm[-1]['loose_per_bunch']:.2f} in {_month_long(bm[-1]['month'])}"
                 + (f", peaking at {peak['loose_per_bunch']:.2f} in {_month_long(peak['month'])}"
                    if peak["month"] not in (bm[0]["month"], bm[-1]["month"]) else ""))

    worst_div = min(m["by_division"], key=lambda r: r["loose_per_bunch"] or 9)
    span = (f"between {_month_long(w['months'][0])} and {_month_long(w['months'][-1])}"
            if w["months"] else "over the window")
    summary = (
        f"Loose fruit is being picked up at {ratio:.2f} per bunch, "
        f"{bench['recovery_pct']:.0f}% of the {bench['loose_per_bunch']:.2f} the estate's best "
        f"quarter of blocks already reach. Matching them would have recovered about "
        f"{_fruits(bench['gap_fruits'])} more fruits {span}; {worst_div['label']} is furthest "
        f"behind at {worst_div['loose_per_bunch']:.2f}, and the {len(worst)} lowest blocks "
        f"listed are the ones to walk first."
    )

    rec = m["recording"]
    caveat = (
        "A low ratio is not proof of fruit left on the ground. Detachment rises with "
        "ripeness, so a block cut on a tighter round sheds less; and a loose fruit count "
        f"is written on only {t['records_with_count_pct']:.0f}% of harvest records, with the "
        f"lowest-ratio blocks recording it least ({rec['bottom_quarter_pct']:.0f}% of their "
        f"records against {rec['top_quarter_pct']:.0f}% on the best quarter), so part of the "
        "gap is fruit collected and never written down. Either way the block is worth "
        "walking. Counts, not kilograms: the tonnes and rupiah rest on a placeholder fruit "
        "weight."
    )

    return {
        "available": True,
        "estate": estate,
        "summary": summary,
        "window": {"from": w["from"], "to": w["to"], "months": w["months"]},
        "totals": dict(t),
        "distribution": dict(dist),
        "benchmark": {**bench, "provenance": "real: measured on this estate"},
        "trend": trend,
        "by_month": [dict(x, provenance="real") for x in bm],
        "by_division": m["by_division"],
        "worst_blocks": [_pack(r, "loose_per_bunch", "recovery_pct", "bunches", "loose_fruits",
                               "gap_fruits", "records_with_count_pct", "palm_age_years")
                         for r in worst],
        "best_blocks": [_pack(r, "loose_per_bunch", "recovery_pct", "bunches", "loose_fruits",
                              "records_with_count_pct", "palm_age_years")
                        for r in best],
        "biggest_falls": [_pack(r, "ratio_first", "ratio_last", "delta", "loose_per_bunch",
                                "bunches")
                          for r in falls],
        "falls_window": {"first": first, "last": last,
                         "first_label": _month_long(first) if first else None,
                         "last_label": _month_long(last) if last else None,
                         "blocks_fell": sum(1 for r in m["falls"] if r["delta"] < 0),
                         "blocks_rose": sum(1 for r in m["falls"] if r["delta"] > 0)},
        "recording": {
            **rec,
            "estate_pct": t["records_with_count_pct"],
            "provenance": "real: share of t_oph records with a non-zero loose_fruits count",
        },
        "value": {
            "gap_fruits": bench["gap_fruits"],
            "gap_kg": round(gap_kg),
            "gap_t": round(gap_kg / 1000, 1),
            "gap_idr": round(gap_idr),
            "gap_idr_m": round(gap_idr / 1e6, 1),
            "kg_per_loose_fruit": KG_PER_LOOSE_FRUIT,
            "kg_per_loose_fruit_provenance": KG_PER_LOOSE_FRUIT_PROVENANCE,
            "price_idr_kg": price,
            "price_provenance": "assumed: ffb_price_idr_kg from the assumption register",
            "assumptions": assumptions.used(["ffb_price_idr_kg"]),
            "provenance": "derived: real fruit count x placeholder fruit weight x assumed FFB price",
            "note": ("Priced at the FFB farmgate rate. Loose fruit is the ripest fruit on "
                     "the bunch and carries the highest oil content of anything the estate "
                     "sends to the mill, so this is a floor on what the gap is worth."),
        },
        "synthetic_comparison": _synthetic_comparison(),
        "provenance": (f"real: t_oph loose_fruits, {t['records']:,} harvest records over "
                       f"{t['blocks']} blocks, {w['from']} to {w['to']}"),
        "note": ("The benchmark is measured on this estate, not taken from literature: "
                 "the ratio the best quarter of blocks already reach. Every count, ratio "
                 "and month here is the client's own; only the tonnes and rupiah lean on "
                 "a placeholder."),
        "caveat": caveat,
    }
