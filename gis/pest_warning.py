"""Early warning: canopy anomaly against census.

The satellite reads every block on the estate; the census that would confirm
what it sees does not exist. This module lays the two over each other and
sorts the blocks into four groups:

    early warning   canopy weak or patchy, census quiet or absent
                    -> the targeted-census list, the thing to act on
    corroborated    weak on both
    census only     census high, canopy fine: over-reporting or recent treatment
    clear           neither

Two provenances meet here and must not be blurred. The canopy is a real
Sentinel-2 measurement of the client's own polygons (gis/vegetation.py). The
census is invented (gis/data/synthetic/ec_pest_census.csv), and the generator
that invented it never read the satellite: its Ganoderma foci sit on blocks
drawn at random, and its only anchor to reality is recorded yield, which the
operations plan found ranks the estate almost independently of vigour. So any
agreement found below is coincidence, and the module says so in the payload
rather than manufacturing a signal. That is not a weakness of the feature: it
is exactly what a real census would be tested against.

Computed once per estate and cached. Everything downstream slices the cached
structure, so the endpoint answers in microseconds after the first call.
"""

import logging
from threading import Lock

from . import layers, vegetation

log = logging.getLogger("estate-command.pest_warning")

_CACHE: dict = {}
_LOCK = Lock()

# Thresholds. Fixed and printed on the screen, so a reader argues with the
# line rather than with the ranking.
GANODERMA_HIGH_PCT = 5.0   # the census panel's own "blocks over 5%" line
COVERAGE_LOW_PCT = 70.0    # fewer than seven palms in ten inspected
FIFTH = 5                  # weak / patchy = the estate's bottom / top fifth,
                           # the same rule layers.compare_metrics uses
NEIGHBOURS = 6             # the k the spread-risk model uses


def reload_pest_warning() -> None:
    with _LOCK:
        _CACHE.clear()


def position(estate: str = "EC", top: int = 12) -> dict:
    """The panel payload. `top` trims the lists; the counts are always whole."""
    est = (estate or "EC").upper()
    with _LOCK:
        full = _CACHE.get(est)
        if full is None:
            full = _compute(est)
            _CACHE[est] = full
    if not full.get("available"):
        return full
    return _trim(full, max(1, int(top or 12)))


def _trim(full: dict, top: int) -> dict:
    out = dict(full)
    for key in ("early_warning", "strongest_ask", "corroborated", "census_only"):
        out[key] = full[key][:top]
    out["top"] = top
    return out


# ── the computation ────────────────────────────────────────────────────────

def _planted_foci() -> int | None:
    """How many foci the generator planted, read from the generator itself."""
    try:
        from .build_synthetic import GANODERMA_FOCI
        return int(GANODERMA_FOCI)
    except Exception:  # noqa: BLE001 - the number is a courtesy, never a dependency
        return None


def _compute(est: str) -> dict:
    rows = layers.block_rows(est)
    if rows is None:
        return {"available": False, "estate": est,
                "reason": f"No block polygons for estate {est}."}
    veg = vegetation.by_block(est)
    scene = vegetation.scene(est)
    if not veg or not scene:
        return {"available": False, "estate": est,
                "reason": ("No Sentinel-2 scene has been pulled for this estate. "
                           f"Run: python -m gis.build_ndre --estate {est}")}

    recs, unmeasured = [], []
    for r in rows:
        v = veg.get(layers._key(r["division_code"], r["block_code"])) or {}
        anomaly = r.get("ndre_anomaly")
        # Only the real reading counts. Under cloud, block_rows falls back to
        # the synthetic vegetation feed; that value must not be scored here.
        if anomaly is None or r.get("ndre_source") != "real:sentinel-2":
            unmeasured.append(r.get("block_label"))
            continue
        p10, p90 = v.get("ndre_p10"), v.get("ndre_p90")
        spread = (round(p90 - p10, 4)
                  if p10 is not None and p90 is not None else None)
        rounds = r.get("pest_rounds") or []
        latest = rounds[-1] if rounds else None
        palms = r.get("palms") or 0
        insp = latest["palms_inspected"] if latest else None
        recs.append({
            "block_id": r["block_id"],
            "block_label": r.get("block_label"),
            "division_code": r.get("division_code"),
            "planted_year": r.get("planted_year"),
            "palm_age_years": r.get("palm_age_years"),
            "planted_ha": r.get("planted_ha"),
            "palms": palms or None,
            # real
            "ndre": r.get("ndre"),
            "ndre_anomaly": anomaly,
            "ndre_spread": spread,
            "ndre_valid_pct": r.get("ndre_valid_pct"),
            "peer_index": r.get("peer_index"),
            # synthetic
            "ganoderma_pct": r.get("ganoderma_pct"),
            "ganoderma_confirmed": r.get("ganoderma_confirmed"),
            "ganoderma_suspect": r.get("ganoderma_suspect"),
            "ganoderma_trend_pct": r.get("ganoderma_trend_pct"),
            "palms_inspected": insp,
            "coverage_pct": latest["coverage_pct"] if latest else None,
            "palms_uninspected": (palms - insp if palms and insp is not None else None),
            "palms_felled": latest["palms_felled"] if latest else None,
            "census_round": latest["round"] if latest else None,
            "census_date": latest["date"] if latest else None,
            "treated": bool(r.get("treatments")),
            "treatment_overdue": r.get("treatment_overdue"),
        })

    if len(recs) < 8:
        return {"available": False, "estate": est,
                "reason": f"Only {len(recs)} blocks carry a real canopy reading."}

    n = len(recs)
    cut = max(1, n // FIFTH)

    # Weak: the bottom fifth of anomaly against age-matched peers.
    by_anom = sorted(recs, key=lambda x: x["ndre_anomaly"])
    weak_ids = {x["block_id"] for x in by_anom[:cut]}
    anomaly_cut = by_anom[cut - 1]["ndre_anomaly"]
    for i, x in enumerate(by_anom):
        x["weakness"] = round(1 - i / (n - 1), 3) if n > 1 else 1.0

    # Patchy: the top fifth of within-block spread. A disease focus is a hole
    # in the canopy, and a hole widens the block's own pixel distribution
    # before it moves the block mean.
    with_spread = [x for x in recs if x["ndre_spread"] is not None]
    by_spread = sorted(with_spread, key=lambda x: x["ndre_spread"])
    m = len(with_spread)
    cut_s = max(1, m // FIFTH) if m else 0
    patchy_ids = {x["block_id"] for x in by_spread[m - cut_s:]} if m else set()
    spread_cut = by_spread[m - cut_s]["ndre_spread"] if m else None
    for i, x in enumerate(by_spread):
        x["patchiness"] = round(i / (m - 1), 3) if m > 1 else 1.0
    for x in recs:
        x.setdefault("patchiness", 0.0)

    for x in recs:
        weak = x["block_id"] in weak_ids
        patchy = x["block_id"] in patchy_ids
        missing = x["ganoderma_pct"] is None
        high = (x["ganoderma_pct"] or 0) > GANODERMA_HIGH_PCT
        cov = x["coverage_pct"]
        cov_low = missing or cov is None or cov < COVERAGE_LOW_PCT
        x["canopy_weak"] = weak
        x["canopy_patchy"] = patchy
        x["canopy_flag"] = weak or patchy
        x["census_missing"] = missing
        x["census_high"] = high
        x["coverage_low"] = cov_low
        # Short, because it is a table cell in a 480 px dock; the booleans
        # beside it carry the same facts for anything that reads the JSON.
        why = []
        if weak:
            why.append("weak")
        if patchy:
            why.append("patchy")
        if missing:
            why.append("not inspected")
        elif cov_low:
            why.append(f"{cov:.0f}% inspected")
        x["why"] = ", ".join(why)
        gap = 1.0 if missing or cov is None else (100 - cov) / 100
        # Priority: how weak, plus how patchy, plus the share of palms nobody
        # looked at. Canopy leads; coverage lifts the blocks where a real
        # census would have the least to say against the satellite.
        x["priority"] = round(x["weakness"] + x["patchiness"] + gap, 3)
        x["group"] = ("early_warning" if (weak or patchy) and not high else
                      "corroborated" if (weak or patchy) else
                      "census_only" if high else "clear")

    early = sorted((x for x in recs if x["group"] == "early_warning"),
                   key=lambda x: -x["priority"])
    corro = sorted((x for x in recs if x["group"] == "corroborated"),
                   key=lambda x: -(x["ganoderma_pct"] or 0))
    census_only = sorted((x for x in recs if x["group"] == "census_only"),
                         key=lambda x: -(x["ganoderma_pct"] or 0))
    clear = [x for x in recs if x["group"] == "clear"]
    strongest = [x for x in early if x["coverage_low"]]

    flagged = len(early) + len(corro)
    high_n = len(corro) + len(census_only)
    flag_rate = flagged / n if n else 0.0

    # ── rank agreement ───────────────────────────────────────────────────
    # compare_metrics gives the canonical rho and reading. Its weak-on-both
    # set is not used: it takes the bottom fifth of both metrics, and the
    # bottom fifth of incidence is the healthiest blocks, not the sickest.
    cmp = layers.compare_metrics(est, "ndre_anomaly", "ganoderma_pct")
    rho = cmp.get("spearman_rho")
    rho_spread, n_spread = _rho(recs, "ndre_spread", "ganoderma_pct")
    rho_trend, n_trend = _rho(recs, "ndre_anomaly", "ganoderma_trend_pct")
    rho_yield, n_yield = _rho(recs, "ndre_anomaly", "peer_index")
    rho_gano_yield, n_gy = _rho(recs, "ganoderma_pct", "peer_index")

    # ── recovered against planted ────────────────────────────────────────
    foci = _foci(recs, layers._centroids(est))
    planted = _planted_foci()
    recovered_foci = [f for f in foci if f["canopy_flag"]]
    expected_foci = round(len(foci) * flag_rate, 1)
    expected_blocks = round(high_n * flag_rate, 1)
    hit_rate = round(100 * len(corro) / high_n, 1) if high_n else None
    chance_rate = round(100 * flag_rate, 1)

    palms_unseen = sum(x["palms_uninspected"] or 0 for x in early)
    palms_unseen_ask = sum(x["palms_uninspected"] or 0 for x in strongest)
    felled = sum(x["palms_felled"] or 0 for x in recs)

    top3 = ", ".join(x["block_label"] for x in early[:3])
    summary = (
        f"The satellite flags {len(early)} blocks that the census does not: send "
        f"the next census round there first, starting with {top3}. "
        f"{len(strongest)} of them had under {COVERAGE_LOW_PCT:.0f}% of palms "
        f"inspected last round, {palms_unseen_ask:,} palms nobody looked at."
    )

    sign = _sign_reading(rho, rho_spread)
    caveat = (
        "The canopy is a real measurement; the census is invented, and the "
        "generator never read the satellite. Its Ganoderma foci were placed on "
        f"{planted if planted is not None else 'a few'} blocks drawn at random, "
        "and its only anchor to reality is recorded yield, which ranks this "
        f"estate almost independently of vigour (rho {_fmt_rho(rho_yield)}). "
        f"Any agreement here is coincidence. Measured: rho {_fmt_rho(rho)} between "
        f"vigour anomaly and incidence, {sign} That is what two spatially "
        "clustered fields laid over each other by chance look like. Nothing on "
        "this screen is a finding about disease on this estate; it is the "
        "test a real census would be put to."
    )

    scene_line = (f"Sentinel-2 L2A scene {scene.get('id')}, {scene.get('date')}, "
                  f"{scene.get('cloud_cover_pct')}% scene cloud, {n} of "
                  f"{len(rows)} blocks measured")
    return {
        "available": True,
        "estate": est,
        "summary": summary,
        "scene": {"id": scene.get("id"), "date": scene.get("date"),
                  "cloud_cover_pct": scene.get("cloud_cover_pct"),
                  "platform": scene.get("platform")},
        "measured": {"blocks": len(rows), "measured": n,
                     "unmeasured": len(unmeasured),
                     "unmeasured_blocks": unmeasured[:20],
                     "census_round": recs[0]["census_round"],
                     "census_date": recs[0]["census_date"]},
        "groups": {
            "early_warning": len(early),
            "corroborated": len(corro),
            "census_only": len(census_only),
            "clear": len(clear),
            "canopy_flagged": flagged,
            "canopy_weak": len(weak_ids),
            "canopy_patchy": len(patchy_ids),
            "census_high": high_n,
            "census_missing": sum(1 for x in recs if x["census_missing"]),
            "strongest_ask": len(strongest),
            "palms_uninspected_in_warning": palms_unseen,
            "palms_uninspected_in_ask": palms_unseen_ask,
            "palms_felled_latest_round": felled,
        },
        "thresholds": {
            "anomaly_weak_at": anomaly_cut,
            "spread_patchy_at": spread_cut,
            "fifth": cut,
            "ganoderma_high_pct": GANODERMA_HIGH_PCT,
            "coverage_low_pct": COVERAGE_LOW_PCT,
        },
        "early_warning": [_pack(x) for x in early],
        "strongest_ask": [_pack(x) for x in strongest],
        "corroborated": [_pack(x) for x in corro],
        "census_only": [_pack(x) for x in census_only],
        # Every measured block, for the quadrant scatter.
        "points": [{"block_label": x["block_label"],
                    "ganoderma_pct": x["ganoderma_pct"],
                    "ndre_anomaly": x["ndre_anomaly"],
                    "ndre_spread": x["ndre_spread"],
                    "coverage_pct": x["coverage_pct"],
                    "group": x["group"]} for x in recs],
        "agreement": {
            "spearman_rho": rho,
            "blocks_with_both": cmp.get("blocks_with_both"),
            "reading": cmp.get("reading"),
            "expected_sign": "negative: a diseased canopy is a thinner canopy",
            "sign_as_expected": (rho is not None and rho < 0),
            "verdict": sign,
            "others": [
                {"a": "canopy patchiness (real)", "b": "Ganoderma incidence (synthetic)",
                 "rho": rho_spread, "n": n_spread,
                 "expected": "positive: a focus is a hole in the canopy"},
                {"a": "vigour anomaly (real)", "b": "incidence rise R1 to R3 (synthetic)",
                 "rho": rho_trend, "n": n_trend,
                 "expected": "negative: stress precedes spread"},
                {"a": "vigour anomaly (real)", "b": "yield vs peers (real)",
                 "rho": rho_yield, "n": n_yield,
                 "expected": "positive, and the one channel the generator used"},
                {"a": "Ganoderma incidence (synthetic)", "b": "yield vs peers (real)",
                 "rho": rho_gano_yield, "n": n_gy,
                 "expected": "negative: the generator's designed link"},
            ],
        },
        "recovered": {
            "planted_foci": planted,
            "foci_found": len(foci),
            "foci": [_pack(f) for f in foci],
            "foci_recovered": len(recovered_foci),
            "foci_expected_by_chance": expected_foci,
            "census_high_blocks": high_n,
            "census_high_recovered": len(corro),
            "census_high_expected_by_chance": expected_blocks,
            "hit_rate_pct": hit_rate,
            "chance_rate_pct": chance_rate,
            "statement": _recovered_statement(planted, foci, recovered_foci,
                                              expected_foci, high_n, len(corro),
                                              expected_blocks, hit_rate, chance_rate),
        },
        "provenance": (
            f"canopy real ({scene_line}); anomaly real, peer-adjusted (NDRE minus "
            "the median of blocks planted the same year); census synthetic (no "
            "table in the 138-table EPMS schema can hold an infected palm)"),
        "sources": {
            "canopy": {"status": "real", "what": "Sentinel-2 NDRE per block, "
                       "with the block's own 10th and 90th percentile pixels",
                       "detail": scene_line},
            "anomaly": {"status": "real", "what": "NDRE minus the median NDRE of "
                        "blocks planted the same year; patchiness is the 90th "
                        "minus the 10th percentile pixel within the block"},
            "census": {"status": "synthetic", "what": "Palm census, latest round "
                       "per block, from gis/data/synthetic/ec_pest_census.csv",
                       "detail": "No table in the 138-table EPMS schema can hold "
                                 "an infected palm. Foci are generated, spatially "
                                 "clustered, and unrelated to the satellite."},
        },
        "note": (
            f"Weak means the bottom fifth of vigour anomaly against age-matched "
            f"peers (at or below {anomaly_cut:+.4f}); patchy means the top fifth "
            f"of within-block spread, the 90th minus the 10th percentile pixel "
            f"NDRE (at or above {spread_cut:.3f}); census-high means over "
            f"{GANODERMA_HIGH_PCT:.0f}% confirmed Ganoderma in the latest round, "
            "the same line the census panel draws. Priority is the weakness "
            "percentile plus the patchiness percentile plus the share of palms "
            "not inspected, so a flagged block nobody has walked ranks first."),
        "caveat": caveat,
        "with_real_census": (
            "With the client's real census - per-palm health status, which no "
            "table in the 138-table EPMS schema can hold - this screen stops "
            "being a demonstration and becomes a calibrated detector: the rank "
            "agreement becomes a measured hit rate, the early-warning list "
            "becomes the next round's route, and each round's findings score "
            "the satellite for the round after. The satellite half is already "
            "running and costs the client nothing."),
        "asks": ["palm_census", "scouting", "treatment"],
    }


# ── helpers ────────────────────────────────────────────────────────────────

_PACK_FIELDS = (
    "block_id", "block_label", "division_code", "planted_year", "palm_age_years",
    "planted_ha", "palms", "ndre", "ndre_anomaly", "ndre_spread", "ndre_valid_pct",
    "ganoderma_pct", "ganoderma_confirmed", "ganoderma_suspect", "ganoderma_trend_pct",
    "palms_inspected", "coverage_pct", "palms_uninspected", "palms_felled",
    "census_round", "treated", "treatment_overdue", "weakness", "patchiness",
    "canopy_weak", "canopy_patchy", "census_high", "census_missing",
    "coverage_low", "why", "priority", "group",
)


def _pack(x: dict) -> dict:
    return {k: x.get(k) for k in _PACK_FIELDS}


def _rho(recs, a: str, b: str):
    pairs = [(x[a], x[b]) for x in recs
             if isinstance(x.get(a), (int, float)) and isinstance(x.get(b), (int, float))]
    return layers._spearman(pairs), len(pairs)


def _fmt_rho(r) -> str:
    return "n/a" if r is None else f"{r:+.2f}"


def _sign_reading(rho, rho_spread) -> str:
    """One clause on what the sign of the agreement says, computed not written."""
    if rho is None:
        return "too few blocks carry both values to read a sign."
    if rho > 0.2:
        return ("the wrong way round: the blocks the census calls sickest are, on "
                "average, greener than their age-matched peers"
                + (", and the patchiest blocks carry less disease, not more."
                   if rho_spread is not None and rho_spread < -0.1 else "."))
    if rho < -0.2:
        return ("the right way round, which on an invented census is luck: the "
                "generator had no way to put disease where the canopy is thin.")
    return ("no relationship in either direction: the two rank the estate "
            "independently.")


def _recovered_statement(planted, foci, recovered, exp_foci, high_n, corro_n,
                         exp_blocks, hit_rate, chance_rate) -> str:
    head = (f"The generator planted {planted} Ganoderma foci"
            if planted is not None else "The generator planted its Ganoderma foci")
    head += (f"; the census reads back {len(foci)} peaks and calls {high_n} blocks "
             f"infected above {GANODERMA_HIGH_PCT:.0f}%. ")
    body = (f"The canopy flags {corro_n} of those {high_n} blocks, where flagging "
            f"the same fifth of the estate at random would hit {exp_blocks:g}, and "
            f"{len(recovered)} of the {len(foci)} peaks against {exp_foci:g} by chance. ")
    if hit_rate is None:
        tail = "There is nothing planted to recover."
    elif hit_rate <= chance_rate * 1.25:
        tail = (f"A hit rate of {hit_rate:g}% against a chance rate of {chance_rate:g}% "
                "is no recovery at all, which is the honest result when the two "
                "were generated independently.")
    else:
        tail = (f"A hit rate of {hit_rate:g}% against a chance rate of {chance_rate:g}% "
                "is above chance, and on an invented census that is coincidence "
                "between two clustered fields, not detection.")
    return head + body + tail


def _foci(recs, centroids, k: int = NEIGHBOURS) -> list[dict]:
    """The census's own peaks: blocks over the line that beat every neighbour.

    The generator planted its foci on block centroids; reading them back from
    the census as local maxima is the only way to count them without asking
    the generator, which a real census could not be asked either.
    """
    pts = [(x, centroids[x["block_id"]]) for x in recs
           if x["block_id"] in centroids and x["ganoderma_pct"] is not None]
    out = []
    for x, (px, py) in pts:
        own = x["ganoderma_pct"] or 0
        if own <= GANODERMA_HIGH_PCT:
            continue
        d = sorted(((px - bx) ** 2 + (py - by) ** 2, i)
                   for i, (o, (bx, by)) in enumerate(pts) if o is not x)
        near = [pts[i][0] for _, i in d[:k]]
        if all((o["ganoderma_pct"] or 0) < own for o in near):
            out.append(x)
    out.sort(key=lambda x: -(x["ganoderma_pct"] or 0))
    return out
