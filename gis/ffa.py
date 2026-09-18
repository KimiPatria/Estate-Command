"""Free fatty acid against dispatch delay: feature ffa_decay, panel `ffa`.

Answers "how much quality is lost between cut and mill?" from the trip ledger
(gis/data/synthetic/<estate>_weighbridge.csv). Free fatty acid climbs the
moment a bunch is cut, so the hours between the knife and the weighbridge are
the one quality loss the estate controls directly.

Standing rule - recovery is not discovery. The generator sets

    ffa_pct = FFA_BASE + FFA_PER_HOUR * turnaround_h + U(-0.15, 0.15)

so a least-squares slope near FFA_PER_HOUR on turnaround is the planted rule
read back, and the payload says so beside every fitted figure. The generator
planted nothing on the field-side wait (07:00 to departure), so the derived
cut-to-mill hours carry almost no slope here; on real tickets that wait is the
largest term and the one the fit would be for.

Provenance: ffa_pct, depart_time, turnaround_h and queue_h are synthetic;
net_kg is derived (real bunches x a synthetic ABW); the bunch counts behind
each trip are the client's. is_planted_anomaly is the shrinkage detector's
answer key and is never read here.

Computed once per estate and cached, the way layers._state() folds the same
ledger; the endpoint answers from the cache in milliseconds.
"""

import logging
from collections import defaultdict
from threading import Lock

import numpy as np

from gis import layers
from gis.build_synthetic import FFA_BASE, FFA_PER_HOUR

log = logging.getLogger("estate-command.ffa")

# ASSUMPTION. Cutting starts about 07:00, so a load that leaves at 11:00
# carries bunches that lay at the roadside for up to four hours.
HARVEST_START_H = 7.0

# PLACEHOLDER. Mills commonly start penalising FFB above about 3-3.5% FFA.
# The line and the shape of the penalty are to be agreed with the mill.
FFA_PENALTY_PCT = 3.0

# PLACEHOLDER. IDR per kg of FFB per FFA point, for putting a price on the
# decay. Not a contract figure; replace with the mill's tariff.
FFA_IDR_PER_KG_POINT = 25.0

# Bucket edges in hours; None is open-ended. A bucket nobody reaches is dropped
# from the payload rather than shown empty.
CUT_TO_MILL_EDGES = (0, 4, 8, 12, 24, None)
TURNAROUND_EDGES = (0, 1, 1.5, 2, 2.5, 3, None)

NOON_H = 12.0            # the generator lengthens the mill queue after noon
SCATTER_POINTS = 600     # dots drawn on the panel; every trip is in the fit
MIN_TRIPS_PER_BLOCK = 5  # a block ranked on fewer trips is noise

_CACHE: dict = {}
_LOCK = Lock()


# -- helpers ----------------------------------------------------------------

def _hours(hhmm) -> float:
    """Clock text such as 11:35 -> 11.58. A malformed time is treated as the
    start of cutting."""
    try:
        hh, mm = str(hhmm).strip().split(":")[:2]
        return int(hh) + int(mm) / 60.0
    except (ValueError, AttributeError):
        return HARVEST_START_H


def _fit(x: np.ndarray, y: np.ndarray) -> dict:
    """Plain least squares, y = slope * x + intercept, with R^2."""
    n = int(x.size)
    if n < 3 or float(x.std()) == 0.0:
        return {"slope_per_h": None, "intercept": None, "r2": None, "n": n}
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    ss = float(((y - y.mean()) ** 2).sum())
    r2 = (1.0 - float((resid ** 2).sum()) / ss) if ss else None
    return {"slope_per_h": round(float(slope), 4),
            "intercept": round(float(intercept), 3),
            "r2": round(r2, 3) if r2 is not None else None,
            "n": n}


def _label(lo, hi) -> str:
    if lo == 0:
        return f"< {hi:g} h"
    if hi is None:
        return f"> {lo:g} h"
    return f"{lo:g}-{hi:g} h"


def _buckets(x, edges, ffa, net, queue) -> list[dict]:
    out = []
    total = int(x.size)
    over = ffa > FFA_PENALTY_PCT
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (x >= lo) if hi is None else ((x >= lo) & (x < hi))
        n = int(m.sum())
        if not n:
            continue
        kg = float(net[m].sum())
        over_kg = float(net[m & over].sum())
        out.append({
            "label": _label(lo, hi),
            "lo_h": lo, "hi_h": hi,
            "trips": n,
            "share_pct": round(100.0 * n / total, 1),
            "tonnes": round(kg / 1000.0, 1),
            "mean_ffa_pct": round(float(ffa[m].mean()), 3),
            "p90_ffa_pct": round(float(np.percentile(ffa[m], 90)), 2),
            "mean_queue_h": round(float(queue[m].mean()), 2),
            "over_threshold_pct": round(100.0 * over_kg / kg, 2) if kg else 0.0,
        })
    return out


def _mean(a: np.ndarray, d: int = 2):
    return round(float(a.mean()), d) if a.size else None


# -- the computation --------------------------------------------------------

def _compute(estate: str) -> dict:
    est = estate.upper()
    rows = layers._read(f"{est.lower()}_weighbridge.csv")
    if not rows:
        return {"available": False, "estate": est,
                "reason": "No trip ledger. Run python gis/build_synthetic.py."}

    n = len(rows)
    depart = np.empty(n)
    turn = np.empty(n)
    queue = np.empty(n)
    ffa = np.empty(n)
    net = np.empty(n)
    km = np.empty(n)
    keys, routes, divs = [], [], []
    for i, r in enumerate(rows):
        depart[i] = _hours(r.get("depart_time"))
        turn[i] = layers._f(r.get("turnaround_h")) or 0.0
        queue[i] = layers._f(r.get("queue_h")) or 0.0
        ffa[i] = layers._f(r.get("ffa_pct")) or 0.0
        net[i] = layers._f(r.get("net_kg")) or 0.0
        km[i] = layers._f(r.get("km_to_mill")) or 0.0
        keys.append(layers._key(r["division_code"], r["block_code"]))
        routes.append(r.get("route_code") or "")
        divs.append(str(r["division_code"]).strip())

    # The three legs of cut-to-mill. Queue is on the ticket; road is what is
    # left of the turnaround once the queue is taken out (run in, load, tip).
    wait = np.clip(depart - HARVEST_START_H, 0.0, None)
    road = np.clip(turn - queue, 0.0, None)
    ctm = wait + turn
    over = ffa > FFA_PENALTY_PCT
    tot_kg = float(net.sum())
    am = depart < NOON_H
    pm = ~am

    # Fits. Turnaround is the regressor the generator used; cut-to-mill is
    # the one the question asks for; queue and wait separate the two legs.
    fit_turn = _fit(turn, ffa)
    fit_ctm = _fit(ctm, ffa)
    fit_queue = _fit(queue, ffa)
    fit_wait = _fit(wait, ffa)
    slope = fit_turn["slope_per_h"] or 0.0
    intercept = fit_turn["intercept"] or 0.0

    recovered_pct = (round(100.0 * slope / FFA_PER_HOUR, 1)
                     if FFA_PER_HOUR else None)
    if recovered_pct is not None and 90.0 <= recovered_pct <= 110.0:
        verdict = (f"The fit reads the planted {FFA_PER_HOUR:g}/h back at "
                   f"{slope:.4f}/h ({recovered_pct:g}%): recovery, not discovery.")
    else:
        verdict = (f"The fit lands at {recovered_pct}% of the planted slope; "
                   f"something in the derivation is off - check it before "
                   f"trusting any figure on this panel.")

    # Where the hours go.
    mean_wait, mean_road, mean_queue = float(wait.mean()), float(road.mean()), float(queue.mean())
    mean_turn, mean_ctm = float(turn.mean()), float(ctm.mean())
    q_am = float(queue[am].mean()) if am.any() else 0.0
    q_pm = float(queue[pm].mean()) if pm.any() else 0.0
    split = {
        "field_wait_h": round(mean_wait, 2),
        "road_h": round(mean_road, 2),
        "queue_h": round(mean_queue, 2),
        "turnaround_h": round(mean_turn, 2),
        "cut_to_mill_h": round(mean_ctm, 2),
        "field_wait_share_pct": round(100.0 * mean_wait / mean_ctm, 1) if mean_ctm else None,
        "road_share_pct": round(100.0 * mean_road / mean_ctm, 1) if mean_ctm else None,
        "queue_share_pct": round(100.0 * mean_queue / mean_ctm, 1) if mean_ctm else None,
        # Of the ticket itself, the share that is standing at the gate: the
        # part the estate can change without buying a vehicle.
        "queue_share_of_turnaround_pct": (round(100.0 * float(queue.sum()) / float(turn.sum()), 1)
                                          if turn.sum() else None),
        "queue_h_before_noon": round(q_am, 2),
        "queue_h_after_noon": round(q_pm, 2),
        "ffa_before_noon": _mean(ffa[am], 3),
        "ffa_after_noon": _mean(ffa[pm], 3),
        "trips_after_noon_pct": round(100.0 * int(pm.sum()) / n, 1),
    }

    by_hour = []
    for hr in range(int(depart.min()), int(depart.max()) + 1):
        m = (depart >= hr) & (depart < hr + 1)
        if not m.any():
            continue
        by_hour.append({
            "hour": f"{hr:02d}:00",
            "trips": int(m.sum()),
            "queue_h": _mean(queue[m]),
            "turnaround_h": _mean(turn[m]),
            "cut_to_mill_h": _mean(ctm[m]),
            "mean_ffa_pct": _mean(ffa[m], 3),
        })

    # Blocks and routes, folded once.
    def _acc():
        return {"trips": 0, "kg": 0.0, "ffa": 0.0, "ctm": 0.0, "turn": 0.0,
                "queue": 0.0, "over_kg": 0.0, "km": 0.0}
    by_block: dict = defaultdict(_acc)
    by_route: dict = defaultdict(_acc)
    route_blocks: dict = defaultdict(set)
    route_divs: dict = defaultdict(set)
    for i, k in enumerate(keys):
        for a in (by_block[k], by_route[routes[i]]):
            a["trips"] += 1
            a["kg"] += net[i]
            a["ffa"] += ffa[i]
            a["ctm"] += ctm[i]
            a["turn"] += turn[i]
            a["queue"] += queue[i]
            a["km"] += km[i]
            if over[i]:
                a["over_kg"] += net[i]
        route_blocks[routes[i]].add(k)
        route_divs[routes[i]].add(divs[i])

    def _pack(a: dict) -> dict:
        t = a["trips"] or 1
        return {
            "trips": int(a["trips"]),
            "hauled_t": round(float(a["kg"]) / 1000.0, 1),
            "mean_ffa_pct": round(float(a["ffa"]) / t, 3),
            "cut_to_mill_h": round(float(a["ctm"]) / t, 2),
            "turnaround_h": round(float(a["turn"]) / t, 2),
            "queue_h": round(float(a["queue"]) / t, 2),
            "km_to_mill": round(float(a["km"]) / t, 2),
            "over_threshold_pct": (round(100.0 * float(a["over_kg"]) / float(a["kg"]), 2)
                                   if a["kg"] else 0.0),
        }

    meta = {layers._key(r["division_code"], r["block_code"]): r
            for r in (layers.block_rows(est) or [])}
    blocks = []
    for k, a in by_block.items():
        if a["trips"] < MIN_TRIPS_PER_BLOCK:
            continue
        m = meta.get(k)
        div, code = k.split("|")
        blocks.append({
            "block_id": m.get("block_id") if m else None,
            "block_label": m.get("block_label") if m else None,
            "block_code": m.get("block_code") if m else code,
            "division_code": m.get("division_code") if m else div,
            **_pack(a),
        })
    worst_ffa = sorted(blocks, key=lambda b: (-b["mean_ffa_pct"], -b["cut_to_mill_h"]))
    longest = sorted(blocks, key=lambda b: (-b["cut_to_mill_h"], -b["mean_ffa_pct"]))

    route_rows = []
    for rc, a in by_route.items():
        dset = route_divs[rc]
        route_rows.append({
            "route_code": rc,
            "division_code": next(iter(dset)) if len(dset) == 1 else None,
            "blocks": len(route_blocks[rc]),
            **_pack(a),
        })
    route_rows.sort(key=lambda r: (-r["mean_ffa_pct"], -r["cut_to_mill_h"]))

    # A thinned scatter for the panel. Every trip is in the fit; the dots are
    # an even sample through the file so every month and block is drawn.
    idx = np.linspace(0, n - 1, min(SCATTER_POINTS, n)).astype(int)
    x0, x1 = float(turn.min()), float(turn.max())
    scatter = {
        "x": "turnaround_h", "y": "ffa_pct",
        "drawn": int(idx.size), "of": n,
        "points": [[round(float(turn[i]), 2), round(float(ffa[i]), 2)] for i in idx],
        "fitted": [[round(x0, 2), round(slope * x0 + intercept, 3)],
                   [round(x1, 2), round(slope * x1 + intercept, 3)]],
        "planted": [[round(x0, 2), round(FFA_BASE + FFA_PER_HOUR * x0, 3)],
                    [round(x1, 2), round(FFA_BASE + FFA_PER_HOUR * x1, 3)]],
        "threshold_pct": FFA_PENALTY_PCT,
    }

    # Pricing, on the placeholder tariff. The queue's share of the slope is the
    # figure to act on; the over-threshold penalty is what the mill would bill.
    queue_points = round(slope * mean_queue, 3)
    pm_extra_points = round(slope * (q_pm - q_am), 3)
    penalty_kg_points = float((np.clip(ffa - FFA_PENALTY_PCT, 0.0, None) * net).sum())
    pricing = {
        "idr_per_kg_point": FFA_IDR_PER_KG_POINT,
        "placeholder": True,
        "queue_ffa_points": queue_points,
        "queue_cost_idr": round(tot_kg * queue_points * FFA_IDR_PER_KG_POINT),
        "afternoon_extra_points": pm_extra_points,
        "afternoon_cost_idr": round(float(net[pm].sum()) * pm_extra_points * FFA_IDR_PER_KG_POINT),
        "over_threshold_penalty_idr": round(penalty_kg_points * FFA_IDR_PER_KG_POINT),
        "note": (f"IDR {FFA_IDR_PER_KG_POINT:g}/kg per FFA point is a placeholder, "
                 "not the mill's tariff. The queue cost prices the FFA the mill "
                 "gate adds to every tonne; the penalty is what the mill would "
                 f"bill for tonnes over {FFA_PENALTY_PCT:.1f}%."),
    }

    over_trips = int(over.sum())
    over_kg = float(net[over].sum())
    over_pct = round(100.0 * over_kg / tot_kg, 2) if tot_kg else 0.0
    totals = {
        "trips": n,
        "hauled_t": round(tot_kg / 1000.0, 1),
        "blocks": len(blocks),
        "routes": len(route_rows),
        "mean_ffa_pct": round(float(ffa.mean()), 3),
        "p50_ffa_pct": round(float(np.percentile(ffa, 50)), 2),
        "p90_ffa_pct": round(float(np.percentile(ffa, 90)), 2),
        "max_ffa_pct": round(float(ffa.max()), 2),
        "mean_cut_to_mill_h": round(mean_ctm, 2),
        "max_cut_to_mill_h": round(float(ctm.max()), 2),
        "mean_field_wait_h": round(mean_wait, 2),
        "mean_turnaround_h": round(mean_turn, 2),
        "mean_queue_h": round(mean_queue, 2),
        "over_threshold_trips": over_trips,
        "over_threshold_t": round(over_kg / 1000.0, 1),
        "over_threshold_pct": over_pct,
    }

    crossed = ("No load" if over_trips == 0
               else f"{over_pct:g}% of tonnes ({over_trips:,} loads)")
    qshare = split["queue_share_of_turnaround_pct"]
    summary = (
        f"Dispatch before noon. Every hour a load spends on the ticket adds "
        f"{slope:.2f} FFA points, {qshare:g}% of that time is the mill queue, and "
        f"loads leaving after 12:00 queue {q_pm:.1f} h against {q_am:.1f} h before. "
        f"{crossed} crossed the {FFA_PENALTY_PCT:.1f}% penalty line across "
        f"{n:,} trips; the {slope:.2f}/h reads back the generator's planted "
        f"{FFA_PER_HOUR:g}/h, it does not discover it."
    )

    return {
        "available": True,
        "estate": est,
        "summary": summary,
        "assumption": (
            f"Hours from cut to mill = (departure time - "
            f"{int(HARVEST_START_H):02d}:00) + the ticket's turnaround. Cutting "
            f"is assumed to start at {int(HARVEST_START_H):02d}:00, so a load "
            "that leaves at 11:00 carries bunches that lay at the roadside up to "
            "four hours; turnaround is taken as loading, the run in and the mill "
            "queue, ending at the weighbridge."),
        "threshold": {
            "ffa_pct": FFA_PENALTY_PCT,
            "placeholder": True,
            "note": ("Mills commonly penalise FFB above about 3-3.5% FFA. "
                     f"{FFA_PENALTY_PCT:.1f}% is a placeholder to agree with the "
                     "mill, as is the shape of the penalty."),
        },
        "totals": totals,
        "delay_split": split,
        "by_depart_hour": by_hour,
        "buckets": _buckets(ctm, CUT_TO_MILL_EDGES, ffa, net, queue),
        "turnaround_buckets": _buckets(turn, TURNAROUND_EDGES, ffa, net, queue),
        "fit": {
            "turnaround": fit_turn,
            "cut_to_mill": fit_ctm,
            "queue": fit_queue,
            "field_wait": fit_wait,
            "method": "ordinary least squares, ffa_pct on hours, every trip weighted equally",
        },
        "planted": {
            "rule": f"ffa_pct = {FFA_BASE:g} + {FFA_PER_HOUR:g} x turnaround_h + U(-0.15, 0.15)",
            "base_pct": FFA_BASE,
            "per_hour": FFA_PER_HOUR,
            "regressor": "turnaround_h",
            "field_wait_per_hour": 0.0,
            "source": "gis/build_synthetic.py (FFA_BASE, FFA_PER_HOUR)",
        },
        "recovery": {
            "regressor": "turnaround_h",
            "slope_planted": FFA_PER_HOUR,
            "slope_recovered": fit_turn["slope_per_h"],
            "recovered_pct": recovered_pct,
            "intercept_planted": FFA_BASE,
            "intercept_recovered": fit_turn["intercept"],
            "r2": fit_turn["r2"],
            "cut_to_mill_slope_recovered": fit_ctm["slope_per_h"],
            "cut_to_mill_slope_planted": None,
            "verdict": verdict,
        },
        "scatter": scatter,
        "worst_ffa_blocks": worst_ffa,
        "longest_delay_blocks": longest,
        "routes": route_rows,
        "pricing": pricing,
        "badges": {
            "ffa_pct": "synthetic",
            "hours": "synthetic",
            "tonnes": "derived",
            "bunches": "real",
            "trips": "synthetic split of the real harvest-day counts",
        },
        "provenance": ("synthetic: ffa_pct, depart_time, turnaround_h and queue_h "
                       "are generated. net_kg is derived (real bunches x a "
                       "synthetic ABW). The bunch counts and harvest-day counts "
                       "behind each trip are the client's real figures. No lab "
                       "FFA feed exists in the export."),
        "note": ("The mill lab holds FFA per delivery, EPMS holds the cut date "
                 "and the weighbridge holds the ticket time. None of the three "
                 "carries the others' key today, so this join is the extract "
                 "to ask for."),
        "caveat": (
            f"Recovery, not discovery. The generator sets FFA = {FFA_BASE:g} + "
            f"{FFA_PER_HOUR:g} x turnaround_h (+/- 0.15), so the {slope:.2f}/h "
            "on turnaround is the planted rule read back. It planted nothing "
            f"on the field-side wait between {int(HARVEST_START_H):02d}:00 and "
            "departure, which is why the derived cut-to-mill hours carry only "
            f"{fit_ctm['slope_per_h']}/h (R2 {fit_ctm['r2']}); on real tickets "
            "that wait is the largest term and the one this fit is for. The "
            f"{FFA_PENALTY_PCT:.1f}% line and the IDR/kg per point are "
            "placeholders to agree with the mill; nothing in this ledger "
            "reaches the line."),
    }


# -- the endpoint -----------------------------------------------------------

def position(estate: str = "EC", top: int = 12) -> dict:
    """GET /gis/ffa?estate=EC&top=12 - the whole position, block lists cut to `top`."""
    est = (estate or "EC").upper()
    with _LOCK:
        d = _CACHE.get(est)
        if d is None:
            d = _compute(est)
            _CACHE[est] = d
    if not d.get("available"):
        return d
    try:
        top = max(1, min(int(top), 50))
    except (TypeError, ValueError):
        top = 12
    out = dict(d)
    out["worst_ffa_blocks"] = d["worst_ffa_blocks"][:top]
    out["longest_delay_blocks"] = d["longest_delay_blocks"][:top]
    return out
