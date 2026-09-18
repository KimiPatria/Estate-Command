"""Collection point coverage and scheduling.

The question: is every block's fruit collected the day it is cut, and where
does it wait? Answered by joining the harvest work orders (the cutting days,
gis/data/synthetic/ec_harvest_orders.csv) to the trip ledger (the trip days,
ec_weighbridge.csv), block by block and day by day.

What the join finds has to be said plainly. The synthetic feeds place a
block's cutting days with one deterministic function (build_synthetic._trip_days,
seeded per block and month) and the trip ledger, the worker feed and the work
orders all draw on it, so every cutting day has its trips the same day and a
day's bunches cut equal the bunches hauled exactly. Same-day collection reads
100% here BY CONSTRUCTION, and this module reports it as that rather than as a
finding. The real question - fruit cut today and still at the collection point
tomorrow - needs two timestamps the client records and did not export: the TPH
count time and the trip departure or weighbridge-in time.

What the ledger can say without those is where the day's fruit waits: how much
of it leaves the platform in the afternoon, how much longer the afternoon
queue at the mill is, and how full the loads are on each route and in each
vehicle class. Those are the scheduling levers, and they are what this
endpoint ranks for the map.

Computed once and cached; the endpoint answers from the cache.
"""

import logging
from collections import defaultdict
from datetime import date
from threading import Lock

from gis import layers, ops

log = logging.getLogger("estate-command.collection")

_CACHE: dict = {}
_LOCK = Lock()

# A load leaving at or after this hour has sat through the morning at the
# platform. Noon, because that is where the ledger's own mill queue steps up
# (0.9 h before it, 1.3 h from it) and because the morning's cut is meant to
# be at the mill by then.
AFTERNOON_H = 12
# A load under half the vehicle's capacity is the wrong vehicle for the
# block-day, or a block-day too small to send that vehicle for.
HALF_LOAD = 0.5
# Blocks need this many trips to be ranked on load factor.
MIN_TRIPS = 5


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _i(v, d=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _hour(hhmm) -> float | None:
    """'13:45' -> 13.75. None for anything else."""
    try:
        h, m = str(hhmm).split(":")
        return int(h) + int(m) / 60.0
    except (TypeError, ValueError, AttributeError):
        return None


def _hhmm(h) -> str | None:
    if h is None:
        return None
    return f"{int(h):02d}:{int(round((h % 1) * 60)):02d}"


def _mean(vals, nd=2):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), nd) if vals else None


def _pct(num, den, nd=1):
    return round(100.0 * num / den, nd) if den else None


def _r(v, nd=1):
    return round(v, nd) if v is not None else None


# ── the fold ────────────────────────────────────────────────────────────────

def _build(estate: str) -> dict:
    ls = layers._state()
    st = ops._state()
    blocks = st["blocks"]
    if not blocks:
        return {"available": False,
                "reason": f"No block polygons for {estate}."}

    harvest = st["orders"].get("harvest") or []
    if not harvest:
        return {"available": False,
                "reason": "No harvest work orders. Run python gis/build_operations.py."}

    veh_cap = {v["vehicle_id"]: (v["capacity_t"] or 0) * 1000.0 for v in st["vehicles"]}
    cls_cap: dict = {}
    cls_n: dict = defaultdict(int)
    for v in st["vehicles"]:
        cls_cap[v["vehicle_class"]] = v["capacity_t"]
        cls_n[v["vehicle_class"]] += 1

    # ── the cutting side: one order per block per day ──────────────────────
    # A plan-only row (rain-out, no-show) has no fruit on the ground, so it
    # is not a cutting day; it is counted separately so the reader can see
    # that "no trip" on such a day is correct rather than missed.
    cut: dict = defaultdict(dict)          # key -> {date: bunches}
    plan_only: dict = defaultdict(set)     # key -> {date}
    for r in harvest:
        k = r["block_key"]
        if r["actual_qty"] > 0:
            cut[k][r["date"]] = cut[k].get(r["date"], 0) + int(r["actual_qty"])
        else:
            plan_only[k].add(r["date"])

    # ── the trip side: one pass over the ledger ────────────────────────────
    def day_bucket():
        return {"trips": 0, "bunches": 0, "net_kg": 0.0, "cap_kg": 0.0,
                "pm_kg": 0.0, "last_h": None, "queue_h": 0.0}

    trip_day: dict = defaultdict(dict)     # key -> {date: bucket}

    def agg_bucket():
        return {"trips": 0, "net_kg": 0.0, "cap_kg": 0.0, "pm_kg": 0.0,
                "under_half": 0, "queue_h": 0.0, "turn_h": 0.0, "ffa": 0.0,
                "km": 0.0, "blocks": set(), "days": set()}

    by_route: dict = defaultdict(agg_bucket)
    by_class: dict = defaultdict(agg_bucket)
    by_hour: dict = defaultdict(agg_bucket)
    by_block: dict = defaultdict(agg_bucket)
    block_route: dict = defaultdict(lambda: defaultdict(int))

    for r in layers._read("ec_weighbridge.csv"):
        k = layers._key(r["division_code"], r["block_code"])
        d = r["date"]
        net = _f(r["net_kg"], 0.0)
        cap = veh_cap.get(r["vehicle_id"]) or (cls_cap.get(r["vehicle_class"]) or 0) * 1000.0
        h = _hour(r["depart_time"])
        pm = h is not None and h >= AFTERNOON_H
        q = _f(r["queue_h"], 0.0)

        b = trip_day[k].setdefault(d, day_bucket())
        b["trips"] += 1
        b["bunches"] += _i(r["bunches_epms"], 0)
        b["net_kg"] += net
        b["cap_kg"] += cap
        b["queue_h"] += q
        if pm:
            b["pm_kg"] += net
        if h is not None and (b["last_h"] is None or h > b["last_h"]):
            b["last_h"] = h

        block_route[k][r["route_code"]] += 1
        for a in (by_route[r["route_code"]], by_class[r["vehicle_class"]],
                  by_hour[int(h) if h is not None else -1], by_block[k]):
            a["trips"] += 1
            a["net_kg"] += net
            a["cap_kg"] += cap
            a["queue_h"] += q
            a["turn_h"] += _f(r["turnaround_h"], 0.0)
            a["ffa"] += _f(r["ffa_pct"], 0.0)
            a["km"] += _f(r["km_to_mill"], 0.0)
            a["blocks"].add(k)
            a["days"].add(d)
            if pm:
                a["pm_kg"] += net
            if cap and net < HALF_LOAD * cap:
                a["under_half"] += 1

    # ── the join, block by block ───────────────────────────────────────────
    rows = []
    for k, blk in blocks.items():
        cdays = cut.get(k, {})
        tdays = trip_day.get(k, {})
        cset, tset = set(cdays), set(tdays)
        same = cset & tset
        overnight = sorted(cset - tset)          # fruit on the ground, no trip
        orphan = sorted(tset - cset)             # a trip with no cut on record

        lags = []
        for d in overnight:
            dd = date.fromisoformat(d)
            later = [(date.fromisoformat(t) - dd).days for t in tset
                     if date.fromisoformat(t) > dd]
            if later:
                lags.append(min(later))
        matched = sum(1 for d in same if cdays[d] == tdays[d]["bunches"])
        cut_b = sum(cdays.values())
        haul_b = sum(b["bunches"] for b in tdays.values())

        a = by_block.get(k) or agg_bucket()
        trips = a["trips"]
        last_hs = [b["last_h"] for b in tdays.values() if b["last_h"] is not None]
        route = max(block_route[k].items(), key=lambda kv: kv[1])[0] if block_route[k] else None

        rows.append({
            "block_key": k, "block_id": blk["id"], "block_label": blk["label"],
            "division_code": blk["division"], "route_code": route,
            "cutting_days": len(cset), "trip_days": len(tset),
            "same_day": len(same),
            "same_day_pct": _pct(len(same), len(cset)),
            "days_left_overnight": len(overnight),
            "overnight_bunches": sum(cdays[d] for d in overnight),
            "lag_days": _mean(lags, 1),
            "trips_without_cut": len(orphan),
            "plan_only_days": len(plan_only.get(k, ())),
            "bunches_cut": cut_b, "bunches_hauled": haul_b,
            "bunch_days_matched_pct": _pct(matched, len(same)),
            "trips": trips,
            "trips_per_cutting_day": _r(trips / len(cset), 2) if cset else None,
            "hauled_t": _r(a["net_kg"] / 1000, 1),
            "afternoon_t": _r(a["pm_kg"] / 1000, 1),
            "afternoon_share_pct": _pct(a["pm_kg"], a["net_kg"]),
            "last_load": _hhmm(_mean(last_hs, 3)),
            "load_factor_pct": _pct(a["net_kg"], a["cap_kg"]),
            "mean_load_t": _r(a["net_kg"] / trips / 1000, 2) if trips else None,
            "loads_under_half_pct": _pct(a["under_half"], trips),
            "mean_queue_h": _r(a["queue_h"] / trips, 2) if trips else None,
            "km_to_mill": _r(a["km"] / trips, 1) if trips else None,
        })

    # ── estate totals ──────────────────────────────────────────────────────
    n_cut = sum(r["cutting_days"] for r in rows)
    n_trip = sum(r["trip_days"] for r in rows)
    n_same = sum(r["same_day"] for r in rows)
    n_over = sum(r["days_left_overnight"] for r in rows)
    all_lags = [r["lag_days"] for r in rows if r["lag_days"] is not None]
    tot_trips = sum(a["trips"] for a in by_route.values())
    tot_net = sum(a["net_kg"] for a in by_route.values())
    tot_cap = sum(a["cap_kg"] for a in by_route.values())
    tot_pm = sum(a["pm_kg"] for a in by_route.values())
    tot_under = sum(a["under_half"] for a in by_route.values())
    am = [a for h, a in by_hour.items() if 0 <= h < AFTERNOON_H]
    pm_ = [a for h, a in by_hour.items() if h >= AFTERNOON_H]
    q_am = sum(a["queue_h"] for a in am) / max(sum(a["trips"] for a in am), 1)
    q_pm = sum(a["queue_h"] for a in pm_) / max(sum(a["trips"] for a in pm_), 1)
    by_construction = (n_cut > 0 and n_same == n_cut
                       and all(r["bunch_days_matched_pct"] in (None, 100.0) for r in rows))

    # ── by month and by division, with the dispatch plan's adherence ───────
    adh = ops.adherence("dispatch")
    adh_month = {m["month"]: m for m in (adh.get("by_month") or [])} if adh.get("available") else {}
    adh_div = {d["division_code"]: d for d in (adh.get("by_division") or [])} if adh.get("available") else {}

    def period_rows(keyfn, adh_map, label):
        g: dict = defaultdict(lambda: {"cut": 0, "same": 0, "over": 0, "plan_only": 0,
                                       "trips": 0, "net": 0.0, "cap": 0.0, "pm": 0.0,
                                       "queue": 0.0, "blocks": set()})
        for k, blk in blocks.items():
            for d, b in trip_day.get(k, {}).items():
                a = g[keyfn(k, blk, d)]
                a["trips"] += b["trips"]
                a["net"] += b["net_kg"]
                a["cap"] += b["cap_kg"]
                a["pm"] += b["pm_kg"]
                a["queue"] += b["queue_h"]
                a["blocks"].add(k)
            for d in cut.get(k, {}):
                a = g[keyfn(k, blk, d)]
                a["cut"] += 1
                a["same"] += 1 if d in trip_day.get(k, {}) else 0
                a["over"] += 0 if d in trip_day.get(k, {}) else 1
            for d in plan_only.get(k, ()):
                g[keyfn(k, blk, d)]["plan_only"] += 1
        out = []
        for key in sorted(g, key=lambda x: (len(x), x)):
            a = g[key]
            ad = adh_map.get(key) or {}
            out.append({
                label: key,
                "blocks": len(a["blocks"]),
                "cutting_days": a["cut"],
                "same_day_pct": _pct(a["same"], a["cut"]),
                "days_left_overnight": a["over"],
                "plan_only_days": a["plan_only"],
                "trips": a["trips"],
                "trips_per_cutting_day": _r(a["trips"] / a["cut"], 2) if a["cut"] else None,
                "hauled_t": _r(a["net"] / 1000, 1),
                "afternoon_share_pct": _pct(a["pm"], a["net"]),
                "load_factor_pct": _pct(a["net"], a["cap"]),
                "mean_queue_h": _r(a["queue"] / a["trips"], 2) if a["trips"] else None,
                "dispatch_planned_t": ad.get("planned_qty"),
                "dispatch_actual_t": ad.get("actual_qty"),
                "dispatch_adherence_pct": ad.get("adherence_pct"),
            })
        return out

    by_month = period_rows(lambda k, blk, d: d[:7], adh_month, "month")
    by_division = period_rows(lambda k, blk, d: blk["division"], adh_div, "division_code")

    # ── routes, vehicle classes, departure hours ───────────────────────────
    def pack(a, trips):
        return {
            "trips": trips,
            "blocks": len(a["blocks"]),
            "days": len(a["days"]),
            "hauled_t": _r(a["net_kg"] / 1000, 1),
            "mean_km": _r(a["km"] / trips, 1) if trips else None,
            "mean_queue_h": _r(a["queue_h"] / trips, 2) if trips else None,
            "mean_turnaround_h": _r(a["turn_h"] / trips, 2) if trips else None,
            "mean_ffa_pct": _r(a["ffa"] / trips, 2) if trips else None,
            "load_factor_pct": _pct(a["net_kg"], a["cap_kg"]),
            "mean_load_t": _r(a["net_kg"] / trips / 1000, 2) if trips else None,
            "loads_under_half_pct": _pct(a["under_half"], trips),
            "afternoon_share_pct": _pct(a["pm_kg"], a["net_kg"]),
            "share_of_tonnes_pct": _pct(a["net_kg"], tot_net),
        }

    routes = []
    for rc, a in by_route.items():
        divs = defaultdict(int)
        for k in a["blocks"]:
            divs[blocks[k]["division"]] += 1
        routes.append({"route_code": rc,
                       "division_code": max(divs.items(), key=lambda kv: kv[1])[0] if divs else None,
                       **pack(a, a["trips"])})
    routes.sort(key=lambda r: (r["load_factor_pct"] if r["load_factor_pct"] is not None else 999))

    # The fleet's demonstrated day, from the dispatch ledger's own helpers.
    p80 = ops._vehicle_daily_loads(st)
    turn = ops._vehicle_turnaround(st)
    cls_p80: dict = defaultdict(list)
    cls_turn: dict = defaultdict(list)
    for v in st["vehicles"]:
        if v["vehicle_id"] in p80:
            cls_p80[v["vehicle_class"]].append(p80[v["vehicle_id"]])
        if v["vehicle_id"] in turn:
            cls_turn[v["vehicle_class"]].append(turn[v["vehicle_id"]])
    classes = []
    for cls, a in by_class.items():
        classes.append({"vehicle_class": cls, "capacity_t": cls_cap.get(cls),
                        "vehicles": cls_n.get(cls, 0),
                        "p80_loads_per_day": _mean(cls_p80.get(cls), 1),
                        "mean_loads_per_day": _r(a["trips"] / len(a["days"]) / max(cls_n.get(cls, 1), 1), 1)
                        if a["days"] else None,
                        "dispatch_turnaround_h": _mean(cls_turn.get(cls), 2),
                        **pack(a, a["trips"])})
    classes.sort(key=lambda r: (r["load_factor_pct"] if r["load_factor_pct"] is not None else 999))

    hours = []
    for h in sorted(x for x in by_hour if x >= 0):
        a = by_hour[h]
        hours.append({"hour": h, "label": f"{h:02d}:00",
                      "afternoon": h >= AFTERNOON_H, **pack(a, a["trips"])})

    # ── the lists for the map ──────────────────────────────────────────────
    waiting = sorted([r for r in rows if r["trips"]],
                     key=lambda r: (-(r["overnight_bunches"] or 0), -(r["afternoon_t"] or 0)))
    worst_load = sorted([r for r in rows if r["trips"] >= MIN_TRIPS and r["load_factor_pct"] is not None],
                        key=lambda r: r["load_factor_pct"])
    overnight_blocks = [r for r in rows if r["days_left_overnight"]]

    worst_route = routes[0] if routes else None
    return {
        "available": True,
        "estate": estate.upper(),
        "rows": rows,
        "coverage": {
            "blocks": len(rows),
            "blocks_cut": sum(1 for r in rows if r["cutting_days"]),
            "cutting_days": n_cut,
            "trip_days": n_trip,
            "same_day": n_same,
            "same_day_pct": _pct(n_same, n_cut),
            "days_left_overnight": n_over,
            "blocks_with_overnight": len(overnight_blocks),
            "overnight_bunches": sum(r["overnight_bunches"] for r in rows),
            "mean_lag_days": _mean(all_lags, 1),
            "trips_without_cut": sum(r["trips_without_cut"] for r in rows),
            "plan_only_days": sum(len(v) for v in plan_only.values()),
            "bunches_cut": sum(r["bunches_cut"] for r in rows),
            "bunches_hauled": sum(r["bunches_hauled"] for r in rows),
            "bunch_days_matched_pct": _pct(
                sum(1 for k in blocks for d in (cut.get(k, {}))
                    if d in trip_day.get(k, {}) and cut[k][d] == trip_day[k][d]["bunches"]),
                n_same),
            "by_construction": by_construction,
        },
        "totals": {
            "trips": tot_trips,
            "hauled_t": _r(tot_net / 1000, 1),
            "trips_per_cutting_day": _r(tot_trips / n_cut, 2) if n_cut else None,
            "load_factor_pct": _pct(tot_net, tot_cap),
            "loads_under_half_pct": _pct(tot_under, tot_trips),
            "afternoon_t": _r(tot_pm / 1000, 1),
            "afternoon_share_pct": _pct(tot_pm, tot_net),
            "queue_morning_h": _r(q_am, 2),
            "queue_afternoon_h": _r(q_pm, 2),
            "afternoon_from": f"{AFTERNOON_H:02d}:00",
            "dispatch_adherence_pct": (adh.get("totals") or {}).get("adherence_pct"),
            "dispatch_planned_t": (adh.get("totals") or {}).get("planned_qty"),
            "dispatch_actual_t": (adh.get("totals") or {}).get("actual_qty"),
        },
        "by_month": by_month,
        "by_division": by_division,
        "by_route": routes,
        "by_class": classes,
        "by_hour": hours,
        "waiting": waiting,
        "worst_load": worst_load,
        "worst_route": worst_route["route_code"] if worst_route else None,
        "worst_route_load_pct": worst_route["load_factor_pct"] if worst_route else None,
    }


# ── the position ────────────────────────────────────────────────────────────

def _cached(estate: str) -> dict:
    key = estate.upper()
    with _LOCK:
        if key not in _CACHE:
            _CACHE[key] = _build(key)
            if _CACHE[key].get("available"):
                c = _CACHE[key]["coverage"]
                log.info("[collection] %s: %d blocks, %d cutting days, %s%% same day%s",
                         key, c["blocks"], c["cutting_days"], c["same_day_pct"],
                         " (by construction)" if c["by_construction"] else "")
        return _CACHE[key]


def reload_collection() -> None:
    with _LOCK:
        _CACHE.clear()


def _after_label() -> str:
    return "noon" if AFTERNOON_H == 12 else f"{AFTERNOON_H:02d}:00"


def _summary(c: dict, t: dict, worst_route, worst_pct) -> str:
    q_bit = ""
    if t["queue_afternoon_h"] is not None and t["queue_morning_h"] is not None:
        gap = t["queue_afternoon_h"] - t["queue_morning_h"]
        q_bit = (f" into a mill queue {abs(gap):.1f} h {'longer' if gap >= 0 else 'shorter'} "
                 f"than the morning's")
    route_bit = (f", and route {worst_route} runs its loads only {worst_pct:.0f}% full"
                 if worst_route and worst_pct is not None else "")
    after = _after_label()
    if c["by_construction"]:
        return (f"Same-day collection reads {c['same_day_pct']:.0f}% here by construction, so this "
                f"ledger cannot show fruit left overnight; what it can show is that "
                f"{t['afternoon_share_pct']:.0f}% of the crop leaves the platform after "
                f"{after}{q_bit}{route_bit}.")
    return (f"{c['same_day_pct']:.0f}% of block-cutting-days were collected the same day; "
            f"{c['days_left_overnight']} block-days on {c['blocks_with_overnight']} blocks were "
            f"left overnight, waiting {c['mean_lag_days']} days on average; "
            f"{t['afternoon_share_pct']:.0f}% of the crop leaves the platform after "
            f"{after}{q_bit}{route_bit}.")


def position(estate: str = "EC", top: int = 12) -> dict:
    """The collection panel: coverage, where the fruit waits, and the lists for the map."""
    d = _cached(estate)
    if not d.get("available"):
        return d
    c, t = d["coverage"], d["totals"]
    top = max(1, min(int(top or 12), 50))

    def strip(r):
        return {k: v for k, v in r.items() if k not in ("block_key",)}

    caveat = (
        "The trip ledger, the worker feed and the work orders all place a block's cutting "
        "days with the same seeded function (gis/build_synthetic._trip_days), so every "
        "cutting day has its trips the same day and the bunches cut equal the bunches hauled "
        "to the unit. Same-day collection, days left overnight and the lag are therefore not "
        "findings here: they are the generator's reconciliation. The question this feature "
        "exists for - fruit cut today and still at the collection point tomorrow - needs two "
        "timestamps the client already records and did not export: the TPH count time from "
        "the harvesting record and the trip departure or weighbridge-in time from the "
        "ticket. With those two columns the same figures below become measurements. The "
        "afternoon queue is generated 1.4x the morning's, which is the shape a real mill "
        "shows, but its size is not the client's."
        if c["by_construction"] else
        "Cutting days come from the harvest work orders and trip days from the weighbridge "
        "ledger; a cutting day with no trip counts as fruit left overnight, and the lag is "
        "the days to the block's next trip. Departure times are the trip ledger's own."
    )

    return {
        "available": True,
        "estate": d["estate"],
        "summary": _summary(c, t, d["worst_route"], d["worst_route_load_pct"]),
        "by_construction": c["by_construction"],
        "coverage": c,
        "totals": t,
        "by_month": d["by_month"],
        "by_division": d["by_division"],
        "by_route": d["by_route"],
        "by_class": d["by_class"],
        "by_hour": d["by_hour"],
        "waiting": [strip(r) for r in d["waiting"][:top]],
        "worst_load": [strip(r) for r in d["worst_load"][:top]],
        "afternoon_from": t["afternoon_from"],
        "afternoon_label": _after_label(),
        "provenance": (
            "synthetic: the trip ledger, the dispatch plan and the work orders are generated. "
            "The number of cutting days per block-month and the bunch counts inside them are "
            "the client's REAL figures; which dates in the month, the departure times, the "
            "vehicles, the weights and the queue times are not. Vehicle capacity is from the "
            "generated fleet master."
        ),
        "note": (
            "Same-day share is block-cutting-days with at least one trip that date over all "
            "block-cutting-days with fruit on the ground; plan-only order rows (rain-outs and "
            f"no-shows, {c['plan_only_days']} of them) have no fruit and no trip and are "
            "counted separately. Load factor is net kg weighed over the capacity of the "
            "vehicle that carried it, summed, not a mean of ratios. The afternoon queue is "
            f"the mean wait at the weighbridge for loads departing from {t['afternoon_from']}, "
            "which is where the ledger's queue steps up. Dispatch adherence is weighed tonnes over planned "
            "tonnes from the dispatch orders (gis/ops.py), so its gap is harvest shortfall "
            "and shrinkage together."
        ),
        "caveat": caveat,
        "needs": [
            {"entity": "TPH count timestamp", "system": "EPMS",
             "note": "the time each collection point's bunches were counted, not just the day"},
            {"entity": "Trip departure or weighbridge-in timestamp", "system": "weighbridge / SAP PM",
             "note": "the time each load left the platform or crossed the bridge, with the TPH it came from"},
        ],
    }
