"""Tomorrow's assignment: which crew goes to which blocks, and why.

The question every panel in this app stops short of. Rotation says which
blocks are ripe, upkeep says which are overdue, pest says where the disease
is; none of them says what G1-03 should do at six tomorrow morning with the
twenty-one men who turn up. This module does, for six operations, from the
demand and capacity the ops layer computes.

The objective
-------------
Maximise value recovered per man-day, subject to capacity. Four terms, all
in rupiah, each traceable to a figure the app already computes:

    deferral     what leaving the block one more planning cycle would cost:
                 the ops layer's cost-per-day times the cycle length. For
                 harvest that is ripe crop losing value past the round; for
                 upkeep it is yield lost while a round is overdue.
    contiguity   a bonus for a block adjacent to one the crew already has
                 (or to its home block), as a share of the block's value.
    travel       the crew's time in transit from wherever it last was,
                 priced at the man-day cost.
    capacity     man-days the crew actually has tomorrow, from attendance.

Contiguity is not a nicety. A plan that sends a gang to blocks 12, 87 and
203 is arithmetically optimal and operationally void: nobody executes it.
The bonus is what produces "blocks 14 to 19" instead of a scattered list,
and the Why tab reports exactly what it cost by running the plan again with
the weight at zero.

The method
----------
Greedy by value density, round-robin across crews, then a swap-improvement
pass. Not an LP. The output has to be defended line by line to a mandor who
disagrees with it, and "this block scored higher than that one, here is the
arithmetic" survives that conversation where a simplex tableau does not.
The gap to a relaxed upper bound (fractional knapsack, no contiguity, no
travel) is reported so the greedy choice is accountable.

Nothing is fitted here. The model inputs are read, never trained, inside a
request: the productivity model's per-block target, and the four forecasts
of buildplan_ml.md, each behind a switch in the assumption register and each
used only where it beat the method it replaces on past days:

    rain         the chance of wash-off and of a stop, from last night's
                 forecast (gis/models/rain.py); spray-or-hold becomes a cost
                 decision
    headcount    each crew's likely turnout and range (headcount.py, read
                 through ops.capacity)
    work done    each block's expected share done; a block's deferral value
                 is discounted by it, so on a wet day gangs go to good roads
                 first (slippage.py)
    speeds       a crew's capacity and a harvest block's rate, adjusted by
                 what the ledger shows they actually do (rates.py)

Everything else is arithmetic over the ledger and the assumption register,
cached by (operation, date, overrides, assumptions).
"""

import json
import logging
import math
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from threading import Lock

from gis import assumptions, ops
from gis.build_operations import SPRAY_RAIN_MM, TOMORROW, WINDOW_END

log = logging.getLogger("estate-command.models.scheduler")

_CACHE: OrderedDict = OrderedDict()
_ADJ: dict = {}
_LOCK = Lock()
MAX_CACHE = 64

# A crew stops taking work below this many man-days left in its day.
MIN_REMAINING_MD = 0.35
# A block may be started partially if at least this share of it fits.
MIN_PARTIAL_SHARE = 0.3
# Swap-improvement budget.
MAX_SWAP_PASSES = 4
MAX_SWAP_EVALS = 40000


# ── geometry ───────────────────────────────────────────────────────────────

def _km(a, b) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 111.32 * math.cos(lat), (a[1] - b[1]) * 111.32)


def _adjacency() -> dict:
    """Blocks that share a boundary, from the real polygons.

    Two blocks are adjacent when any vertex of one sits within ~50 m of any
    vertex of the other. A grid hash keeps it linear; computed once.
    """
    with _LOCK:
        if "adj" in _ADJ:
            return _ADJ["adj"]
        st = ops._state()
        cell = 0.0005                       # about 55 m
        grid: dict = defaultdict(set)
        for k, b in st["blocks"].items():
            for x, y in b["ring"]:
                grid[(int(x // cell), int(y // cell))].add(k)
        adj: dict = defaultdict(set)
        for (cx, cy), ks in grid.items():
            near = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    near |= grid.get((cx + dx, cy + dy), set())
            for k in ks:
                adj[k] |= near - {k}
        _ADJ["adj"] = dict(adj)
        _ADJ["mill"] = (min(b["centroid"][0] for b in st["blocks"].values()) - 0.012,
                        min(b["centroid"][1] for b in st["blocks"].values()) - 0.010)
        return _ADJ["adj"]


def _mill() -> tuple:
    _adjacency()
    return _ADJ["mill"]


def _range_label(codes: list) -> str:
    """'14-19, 23, 31-33' from a list of block codes."""
    nums = sorted({int(c) for c in codes if str(c).strip().lstrip("-").isdigit()})
    if not nums:
        return ""
    runs, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append((start, prev))
        start = prev = n
    runs.append((start, prev))
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


# ── the plan ───────────────────────────────────────────────────────────────

def _crews_for(operation: str, on: date, overrides: dict, av: dict) -> list[dict]:
    from gis.models import rates
    m = ops.OPERATIONS[operation]
    cap = ops.capacity(on.isoformat(), m["crew_type"])
    if not cap.get("available"):
        return []
    st = ops._state()
    rows = cap["vehicles"] if operation == "dispatch" else cap["crews"]
    ov = (overrides.get("crews") or {})
    out = []
    for c in rows:
        o = ov.get(c["crew_code"]) or {}
        if o.get("exclude"):
            continue
        if overrides.get("division") and c.get("division_code") \
                and str(c["division_code"]) != str(overrides["division"]):
            continue
        present = c["present"]
        if isinstance(o.get("present"), (int, float)):
            present = max(0, min(int(o["present"]), c["on_roll"]))
        crew = st["crew_by"].get(c["crew_code"]) or {}
        if operation == "dispatch":
            man_days = c["man_days"] if present else 0
            home = _mill()
        else:
            if operation == "harvest" and crew.get("harvesters") and crew.get("establishment"):
                man_days = int(round(present * crew["harvesters"] / crew["establishment"]))
            else:
                man_days = present
            hb = next((b for b in st["blocks"].values() if b["label"] == c.get("home_block")), None)
            home = hb["centroid"] if hb else _mill()
        # A crew that works faster than the book covers more standard man-days.
        speed = rates.crew_factor(operation, c["crew_code"], on) if operation in rates.RATE_OPS else 1.0
        out.append({
            **c, "present": present, "edited": bool(o),
            "present_edited": isinstance(o.get("present"), (int, float)),
            "speed_factor": round(speed, 3), "base_md": float(man_days),
            "capacity_md": float(man_days) * speed, "remaining_md": float(man_days) * speed,
            "home": home, "home_key": next((k for k, b in st["blocks"].items()
                                            if b["label"] == c.get("home_block")), None),
            "position": home, "assigned": [],
        })
    return out


def _items_for(operation: str, on: date, overrides: dict, expect: dict | None = None,
               discount: bool = False) -> tuple[list, dict]:
    dem = ops.demand(operation, on.isoformat())
    if not dem.get("available"):
        return [], dem
    st = ops._state()
    excluded = {str(x) for x in (overrides.get("exclude_blocks") or [])}
    closed = {str(x) for x in (overrides.get("road_closed") or [])}
    items = []
    for i in dem["items"]:
        if str(i["block_label"]) in excluded or i["block_key"] in excluded:
            continue
        if str(i["block_label"]) in closed or i["block_key"] in closed:
            continue
        if overrides.get("division") and str(i["division_code"]) != str(overrides["division"]):
            continue
        if not i.get("man_days"):
            continue
        b = st["blocks"][i["block_key"]]
        nominal = i["deferral_cost_idr_per_day"] * i["horizon_days"]
        item = {**i, "centroid": b["centroid"], "deferral_idr": nominal, "deferral_idr_nominal": nominal}
        e = (expect or {}).get(i["block_key"])
        if e:
            item.update({"expected_share": e["expected_share"], "p_carried": e["p_carried"],
                         "risk_drivers": e["drivers"]})
            if discount:
                # Value recovered, not value scheduled: a block whose work is
                # likely to be rained off or carried is worth less tomorrow.
                item["deferral_idr"] = nominal * e["expected_share"]
        items.append(item)
    return items, dem


def _travel_idr(crew: dict, item: dict, av: dict, operation: str) -> tuple[float, float]:
    km = _km(crew["position"], item["centroid"])
    if operation == "dispatch":
        # A vehicle's hour, priced as the driver's day plus a little diesel.
        speed = 25.0
        hour_cost = av["man_day_cost_idr"] / av["work_day_hours"] * 2.5
        return km, km / speed * hour_cost
    hours = km / max(av["crew_transport_km_per_hour"], 1.0)
    return km, hours * crew["present"] * av["man_day_cost_idr"] / av["work_day_hours"]


def _contiguous(crew: dict, item: dict, adj: dict, operation: str) -> bool:
    if operation == "dispatch":
        return any(a["division_code"] == item["division_code"] for a in crew["assigned"])
    near = adj.get(item["block_key"], set())
    if any(a["block_key"] in near for a in crew["assigned"]):
        return True
    return not crew["assigned"] and crew.get("home_key") in near


def _score(crew: dict, item: dict, adj: dict, av: dict, operation: str, contig_w: float) -> dict:
    km, travel = _travel_idr(crew, item, av, operation)
    contig = _contiguous(crew, item, adj, operation)
    bonus = contig_w / 100.0 * item["value_idr"] if contig else 0.0
    score = item["deferral_idr"] + bonus - travel
    return {"deferral_idr": item["deferral_idr"], "contiguity_idr": round(bonus),
            "travel_idr": round(travel), "travel_km": round(km, 2),
            "score": score, "contiguous": contig,
            "density": score / item["man_days"] if item["man_days"] else 0.0}


def _greedy(crews: list, items: list, adj: dict, av: dict, operation: str, contig_w: float) -> None:
    """Round-robin greedy: each crew takes its best next block in turn."""
    pool_of = {}
    for c in crews:
        if operation == "dispatch":
            pool_of[c["crew_code"]] = items
        else:
            pool_of[c["crew_code"]] = [i for i in items if i["division_code"] == str(c["division_code"])]
    taken = {}
    progress = True
    while progress:
        progress = False
        for c in sorted(crews, key=lambda x: -x["remaining_md"]):
            if c["remaining_md"] < MIN_REMAINING_MD:
                continue
            best, best_s, best_share = None, None, 1.0
            for i in pool_of[c["crew_code"]]:
                if i["block_key"] in taken:
                    continue
                s = _score(c, i, adj, av, operation, contig_w)
                if s["score"] <= 0:
                    continue
                share = 1.0
                if i["man_days"] > c["remaining_md"]:
                    share = c["remaining_md"] / i["man_days"]
                    # A crew's first block can be a multi-day job it merely
                    # starts: pruning a 3,000-palm block is four days for one
                    # crew and the plan has to be able to say "begin it".
                    # A later block has to mostly fit, or the day fragments.
                    if share < MIN_PARTIAL_SHARE and c["assigned"]:
                        continue
                if best_s is None or s["density"] > best_s["density"]:
                    best, best_s, best_share = i, s, share
            if best is None:
                continue
            _assign(c, best, best_s, best_share, taken)
            progress = True


def _assign(crew: dict, item: dict, s: dict, share: float, taken: dict) -> None:
    md = item["man_days"] * share
    crew["assigned"].append({**item, "share": round(share, 3), "md": md, **s,
                             "seq": len(crew["assigned"]) + 1})
    crew["remaining_md"] -= md
    crew["position"] = item["centroid"]
    taken[item["block_key"]] = crew["crew_code"]


def _crew_objective(crew: dict, adj: dict, av: dict, operation: str, contig_w: float) -> float:
    """Recompute a crew's whole objective along its assignment order."""
    pos = crew["home"]
    total = 0.0
    seen = []
    for a in crew["assigned"]:
        km = _km(pos, a["centroid"])
        if operation == "dispatch":
            travel = km / 25.0 * av["man_day_cost_idr"] / av["work_day_hours"] * 2.5
            contig = any(x["division_code"] == a["division_code"] for x in seen)
        else:
            travel = (km / max(av["crew_transport_km_per_hour"], 1.0)
                      * crew["present"] * av["man_day_cost_idr"] / av["work_day_hours"])
            near = adj.get(a["block_key"], set())
            contig = any(x["block_key"] in near for x in seen) or \
                (not seen and crew.get("home_key") in near)
        bonus = contig_w / 100.0 * a["value_idr"] if contig else 0.0
        total += a["deferral_idr"] * a["share"] + bonus * a["share"] - travel
        pos = a["centroid"]
        seen.append(a)
    return total


def _swap_pass(crews: list, items: list, adj: dict, av: dict, operation: str,
               contig_w: float) -> int:
    """Try replacing each assigned block with an unassigned one that fits."""
    taken = {a["block_key"] for c in crews for a in c["assigned"]}
    accepted, evals = 0, 0
    for _ in range(MAX_SWAP_PASSES):
        improved = False
        for c in crews:
            pool = items if operation == "dispatch" else \
                [i for i in items if i["division_code"] == str(c["division_code"])]
            base = _crew_objective(c, adj, av, operation, contig_w)
            for idx, a in enumerate(list(c["assigned"])):
                if a["share"] < 1.0:
                    continue
                room = c["remaining_md"] + a["md"]
                for b in pool:
                    if b["block_key"] in taken or b["man_days"] > room:
                        continue
                    evals += 1
                    if evals > MAX_SWAP_EVALS:
                        return accepted
                    trial = {**b, "share": 1.0, "md": b["man_days"], "seq": a["seq"]}
                    old = c["assigned"][idx]
                    c["assigned"][idx] = trial
                    new = _crew_objective(c, adj, av, operation, contig_w)
                    if new > base + 1.0:
                        c["remaining_md"] = room - b["man_days"]
                        taken.discard(old["block_key"])
                        taken.add(b["block_key"])
                        base = new
                        accepted += 1
                        improved = True
                        a = trial
                    else:
                        c["assigned"][idx] = old
        if not improved:
            break
    return accepted


def _finalise(crews: list, adj: dict, av: dict, operation: str, contig_w: float) -> None:
    """Recompute each block's terms along the final order, for the Why tab."""
    for c in crews:
        pos = c["home"]
        seen = []
        for a in c["assigned"]:
            km = _km(pos, a["centroid"])
            if operation == "dispatch":
                travel = km / 25.0 * av["man_day_cost_idr"] / av["work_day_hours"] * 2.5
                contig = any(x["division_code"] == a["division_code"] for x in seen)
            else:
                travel = (km / max(av["crew_transport_km_per_hour"], 1.0)
                          * c["present"] * av["man_day_cost_idr"] / av["work_day_hours"])
                near = adj.get(a["block_key"], set())
                contig = any(x["block_key"] in near for x in seen) or \
                    (not seen and c.get("home_key") in near)
            bonus = contig_w / 100.0 * a["value_idr"] if contig else 0.0
            a.update({"travel_km": round(km, 2), "travel_idr": round(travel),
                      "contiguity_idr": round(bonus), "contiguous": contig,
                      "score": round(a["deferral_idr"] * a["share"] + bonus * a["share"] - travel)})
            pos = a["centroid"]
            seen.append(a)


def _upper_bound(crews: list, items: list, operation: str) -> float:
    """Fractional knapsack on deferral per man-day, per pool. No geography."""
    if operation == "dispatch":
        pools = {"all": (sum(c["capacity_md"] for c in crews), items)}
    else:
        pools = {}
        for c in crews:
            d = str(c["division_code"])
            pools.setdefault(d, [0.0, []])
            pools[d][0] += c["capacity_md"]
        for i in items:
            if i["division_code"] in pools:
                pools[i["division_code"]][1].append(i)
        pools = {k: (v[0], v[1]) for k, v in pools.items()}
    ub = 0.0
    for cap, pool in pools.values():
        left = cap
        for i in sorted(pool, key=lambda x: -(x["deferral_idr"] / x["man_days"])):
            if left <= 0:
                break
            take = min(1.0, left / i["man_days"])
            ub += i["deferral_idr"] * take
            left -= i["man_days"] * take
    return ub


def _run(operation: str, on: date, overrides: dict, av: dict, contig_w: float,
         expect: dict | None = None, discount: bool = False) -> dict:
    adj = _adjacency()
    crews = _crews_for(operation, on, overrides, av)
    items, dem = _items_for(operation, on, overrides, expect, discount)
    if not crews or not items:
        return {"crews": crews, "items": items, "demand": dem, "swaps": 0, "ub": 0.0}
    _greedy(crews, items, adj, av, operation, contig_w)
    swaps = _swap_pass(crews, items, adj, av, operation, contig_w)
    _finalise(crews, adj, av, operation, contig_w)
    return {"crews": crews, "items": items, "demand": dem, "swaps": swaps,
            "ub": _upper_bound(crews, items, operation)}


def _weather(operation: str, on: date, overrides: dict, av: dict) -> dict:
    """The rain the plan decides on.

    With the rain forecast on, every date, replays included, is planned on
    what was knowable the evening before: the chance of wash-off and of a
    stop. The rain that actually fell is carried alongside for a replay, never
    used to decide. Off, it falls back to the old behaviour: recorded rain on
    a replay, unknown for tomorrow.
    """
    st = ops._state()
    recorded = st["rain"].get(on.isoformat()) if on <= WINDOW_END else None
    fc = None
    if isinstance(overrides.get("rain_mm"), (int, float)):
        rain, source = float(overrides["rain_mm"]), "set on the plan"
    else:
        if int(av.get("use_rain_model", 0)) == 1:
            from gis.models import rain as rain_model
            f = rain_model.forecast(on, float(av["rain_cutoff_mm"]))
            if f.get("available"):
                fc = f
        if fc is not None:
            rain = None
            source = ("predicted: the forecast issued the evening before, as learned against what fell"
                      if fc["source"] == "model" else "predicted: the month's usual rain (no forecast archived)")
        elif on <= WINDOW_END:
            rain, source = recorded, "real: Open-Meteo, recorded on the day"
        else:
            rain, source = None, "unknown: tomorrow's rain is not in the archive yet"
    stops = False
    reason = None
    if rain is not None:
        if operation == "spray" and rain >= SPRAY_RAIN_MM:
            stops, reason = True, f"{rain:.0f} mm on the day: herbicide washes off above {SPRAY_RAIN_MM:.0f} mm"
        elif rain >= av["rain_cutoff_mm"]:
            stops, reason = True, f"{rain:.0f} mm on the day, above the {av['rain_cutoff_mm']:.0f} mm cutoff"
    elif fc is not None and fc["chances"]["stop"]["p"] >= 0.5 and operation != "dispatch":
        c = fc["chances"]["stop"]
        stops, reason = True, (f"rain of {av['rain_cutoff_mm']:.0f} mm or more is {c['words']} "
                               f"({c['pct']}%), so field work is called off")
    # Expected adherence from what the ledger did under the same rain; the
    # work-done forecast replaces this once crews are assigned.
    led = ops.ledger(operation, limit=1)
    buckets = (led.get("drivers") or {}).get("rain") or []
    exp, basis = led.get("totals", {}).get("adherence_pct"), "ledger mean, rain unknown"
    if rain is not None and buckets:
        lbl = ops._bucket(rain, ops._RAIN_BUCKETS)
        hit = next((b for b in buckets if b["bucket"] == lbl), None)
        if hit and hit["adherence_pct"] is not None:
            exp, basis = hit["adherence_pct"], f"ledger adherence on days with {lbl}"
    out = {"rain_mm": rain, "source": source, "stops_work": stops, "reason": reason,
           "expected_adherence_pct": exp, "basis": basis,
           "recorded_mm": recorded if (fc is not None or isinstance(overrides.get("rain_mm"), (int, float))) else None}
    if fc is not None:
        out["forecast"] = {
            "headline": fc["headline"], "source": fc["source"], "source_label": fc["source_label"],
            "chances": fc["chances"], "amount": fc["amount"], "how": fc["how"],
            "forecast_plain": fc["forecast_plain"], "recorded_plain": fc["recorded_plain"],
            "grade": fc["grade"], "month_average_pct": fc["month_average_pct"],
            "provenance": fc["provenance"],
        }
    return out


def plan(operation: str, on: str | None = None, overrides: dict | None = None) -> dict:
    """Tomorrow's assignment for one operation, every figure pre-computed."""
    m = ops.OPERATIONS.get(operation)
    if not m:
        return {"available": False, "reason": f"No operation {operation!r}.",
                "available_operations": list(ops.OPERATIONS)}
    overrides = overrides or {}
    try:
        d = date.fromisoformat(on) if on else TOMORROW
    except ValueError:
        return {"available": False, "reason": f"Bad date {on!r}; use YYYY-MM-DD."}
    av = assumptions.values()
    if isinstance(overrides.get("contiguity_bonus_pct"), (int, float)):
        av["contiguity_bonus_pct"] = float(overrides["contiguity_bonus_pct"])
    key = (operation, d.isoformat(), json.dumps(overrides, sort_keys=True, default=str),
           assumptions.fingerprint())
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]

    out = _build(operation, d, overrides, av, m)
    with _LOCK:
        _CACHE[key] = out
        while len(_CACHE) > MAX_CACHE:
            _CACHE.popitem(last=False)
    return out


def _use_work_done(operation: str, av: dict) -> bool:
    from gis.models import slippage
    if int(av.get("use_slippage_model", 0)) != 1 or operation not in slippage.OPS:
        return False
    bt = slippage.backtest()
    v = ((bt.get("modes") or {}).get("six_in_the_morning") or {}).get("by_operation", {}).get(operation) or {}
    return bool(v.get("order_improvement_pct") and v["order_improvement_pct"] > 0)


def _build(operation: str, d: date, overrides: dict, av: dict, m: dict,
           hold: dict | None = None) -> dict:
    from gis.models import slippage
    st = ops._state()
    weather = _weather(operation, d, overrides, av)
    if hold:
        weather.update({"stops_work": True, "reason": hold["plain"], "spray_call": hold})
    contig_w = av["contiguity_bonus_pct"]
    rain_set = float(overrides["rain_mm"]) if isinstance(overrides.get("rain_mm"), (int, float)) else None

    use_wd = _use_work_done(operation, av)
    expect = None
    if use_wd:
        cand, _ = _items_for(operation, d, overrides)
        expect = (slippage.item_expectations(operation, d, cand, rain_set) or {}).get("items")
    discount = bool(expect) and int(av.get("plan_on_expected_adherence", 0)) == 1

    if weather["stops_work"]:
        crews = _crews_for(operation, d, overrides, av)
        items, dem = _items_for(operation, d, overrides, expect, discount)
        res = {"crews": crews, "items": items, "demand": dem, "swaps": 0, "ub": 0.0}
        without = None
    else:
        res = _run(operation, d, overrides, av, contig_w, expect, discount)
        without = _run(operation, d, overrides, av, 0.0, expect, discount) if contig_w > 0 else None

    crews, items, dem = res["crews"], res["items"], res["demand"]
    taken = {a["block_key"]: c["crew_code"] for c in crews for a in c["assigned"]}
    abw = st["abw"]
    seq = 0
    letter = {"harvest": "H", "prune": "U", "weed": "U", "spray": "U", "pest": "P", "dispatch": "D"}[operation]
    order_refs = []
    crew_rows = []
    for c in sorted(crews, key=lambda x: (int(x.get("division_code") or 0) if str(x.get("division_code") or "").isdigit() else 0, x["crew_code"])):
        blocks = []
        for a in c["assigned"]:
            seq += 1
            ref = f"WO-{d:%Y-%m%d}-{letter}-{seq:03d}"
            order_refs.append(ref)
            qty = a["qty"] * a["share"]
            if operation == "harvest":
                tonnes = qty * (abw.get(a["block_key"]) or av["abw_kg"]) / 1000
            elif operation == "dispatch":
                tonnes = qty
            else:
                tonnes = a["tonnes_at_risk"] * a["share"]
            blocks.append({
                "order_ref": ref, "seq": a["seq"],
                "block_key": a["block_key"], "block_id": a["block_id"],
                "block_label": a["block_label"], "block_code": a["block_code"],
                "division_code": a["division_code"], "planted_ha": a["planted_ha"],
                "activity": a.get("activity") or operation, "pest": a.get("pest"),
                "qty": round(qty, 1 if m["unit"] in ("ha", "tonnes") else 0), "unit": a["unit"],
                "share": a["share"], "partial": a["share"] < 1.0,
                "in_progress": a.get("in_progress", False),
                "man_days": round(a["md"], 2),
                "rate_per_man_day": a["rate_per_man_day"], "rate_source": a["rate_source"],
                "tonnes": round(tonnes, 2), "value_idr": round(a["value_idr"] * a["share"]),
                "urgency": a["urgency"], "days_since": a["days_since"],
                "days_over_round": a["days_over_round"], "target_days": a["target_days"],
                "deferral_idr": round(a["deferral_idr"] * a["share"]),
                "deferral_cost_idr_per_day": a["deferral_cost_idr_per_day"],
                "contiguity_idr": a["contiguity_idr"], "contiguous": a["contiguous"],
                "travel_km": a["travel_km"], "travel_idr": a["travel_idr"],
                "score": a["score"], "road_condition": a.get("road_condition"),
                "harvest_crew": a.get("harvest_crew"), "loads": a.get("loads"),
                "expected_share": a.get("expected_share"), "p_carried": a.get("p_carried"),
                "risk_drivers": a.get("risk_drivers") or [],
                "deferral_idr_nominal": round((a.get("deferral_idr_nominal") or a["deferral_idr"]) * a["share"]),
            })
        used = c["capacity_md"] - c["remaining_md"]
        exp_adh = weather["expected_adherence_pct"]
        tot_qty = sum(b["qty"] for b in blocks)
        crew_rows.append({
            "crew_code": c["crew_code"], "name": c.get("name") or c["crew_code"],
            "crew_type": c["crew_type"], "division_code": c.get("division_code"),
            "vehicle_class": c.get("vehicle_class"), "capacity_t": c.get("capacity_t"),
            "on_roll": c["on_roll"], "present": c["present"], "cutters": c.get("cutters"),
            "attendance_basis": c.get("basis"), "edited": c["edited"],
            "present_low": None if c.get("present_edited") else c.get("present_low"),
            "present_high": None if c.get("present_edited") else c.get("present_high"),
            "model_present": c.get("model_present"), "present_source": (
                "set on the plan" if c.get("present_edited") else c.get("present_source")),
            "recorded_present": c.get("recorded_present"),
            "turnout_drivers": c.get("drivers") or [],
            "speed_factor": c.get("speed_factor", 1.0), "base_md": round(c.get("base_md", c["capacity_md"]), 1),
            "home_block": c.get("home_block"),
            "capacity_md": round(c["capacity_md"], 1), "used_md": round(used, 2),
            "utilisation_pct": round(100 * used / c["capacity_md"], 1) if c["capacity_md"] else None,
            "blocks": blocks,
            "block_labels": [b["block_label"] for b in blocks],
            "range_label": _range_label([b["block_code"] for b in blocks]),
            "totals": {
                "blocks": len(blocks),
                "qty": round(tot_qty, 1), "unit": m["unit"],
                "ha": round(sum(b["planted_ha"] or 0 for b in blocks), 1),
                "tonnes": round(sum(b["tonnes"] for b in blocks), 2),
                "value_idr": sum(b["value_idr"] for b in blocks),
                "deferral_idr": sum(b["deferral_idr"] for b in blocks),
                "contiguity_idr": sum(b["contiguity_idr"] for b in blocks),
                "travel_idr": sum(b["travel_idr"] for b in blocks),
                "travel_km": round(sum(b["travel_km"] for b in blocks), 1),
                "contiguous_pairs": sum(1 for b in blocks if b["contiguous"]),
                "max_days_over_round": max((b["days_over_round"] or 0 for b in blocks), default=0),
                "expected_qty": round(tot_qty * exp_adh / 100, 1) if exp_adh is not None else None,
            },
        })

    work_done = None
    if use_wd and not weather["stops_work"] and any(c["blocks"] for c in crew_rows):
        work_done = slippage.plan_expectations(operation, d, crew_rows, rain_set)
        if work_done:
            by_crew = {x["crew_code"]: x for x in work_done["crews"]}
            for c in crew_rows:
                x = by_crew.get(c["crew_code"])
                if x:
                    c["totals"]["expected_qty"] = x["expected_qty"]
                    c["totals"]["expected_qty_low"] = x["low_qty"]
                    c["totals"]["expected_qty_high"] = x["high_qty"]
                    c["totals"]["expected_tonnes"] = round(sum(
                        b["tonnes"] * (b.get("expected_share") or 0) for b in c["blocks"]), 2)
            weather["expected_adherence_pct"] = round(100 * work_done["expected_share"], 1)
            weather["basis"] = ("the work-done forecast for these blocks and crews, over the rain "
                                "the forecast allows and the turnout expected")

    # Spray or hold, as a cost decision, when the plan is deciding on a forecast.
    fc = weather.get("forecast")
    if (operation == "spray" and fc and not hold and not weather["stops_work"]
            and any(c["blocks"] for c in crew_rows)):
        from gis.models import rain as rain_model
        blocks = [b for c in crew_rows for b in c["blocks"]]
        defer = sum(b["deferral_cost_idr_per_day"] * b["share"] for b in blocks)
        ha = sum((b["planted_ha"] or 0) * b["share"] for b in blocks)
        call = rain_model.spray_call(float(fc["chances"]["washoff"]["p"]), defer,
                                     ha * float(av["herbicide_idr_per_ha"]),
                                     fc["month_average_pct"]["washoff"] / 100.0, d.strftime("%B"))
        call["hectares"] = round(ha, 1)
        call["blocks"] = len(blocks)
        if not call["go"]:
            return _build(operation, d, overrides, av, m, hold=call)
        weather["spray_call"] = call

    not_reached = [i for i in items if i["block_key"] not in taken]
    nr_cost = sum(i["deferral_cost_idr_per_day"] for i in not_reached)
    achieved = sum(b["deferral_idr"] for c in crew_rows for b in c["blocks"])
    ub = res["ub"]
    gap = round(100 * (ub - achieved) / ub, 1) if ub else None

    contiguity = {"weight_pct": contig_w,
                  "pairs_with": sum(c["totals"]["contiguous_pairs"] for c in crew_rows)}
    if without:
        w_ach = sum(a["deferral_idr"] * a["share"] for c in without["crews"] for a in c["assigned"])
        contiguity.update({
            "deferral_without_idr": round(w_ach),
            "deferral_with_idr": round(achieved),
            "cost_idr": round(max(0.0, w_ach - achieved)),
            "cost_pct": round(100 * max(0.0, w_ach - achieved) / w_ach, 1) if w_ach else None,
            "pairs_without": sum(1 for c in without["crews"] for a in c["assigned"] if a["contiguous"]),
            "reading": ("What keeping crews on adjacent blocks gives up in deferral value, "
                        "against the scattered plan a pure ranking would produce."),
        })

    need_md = sum(i["man_days"] for i in items)
    cap_md = sum(c["capacity_md"] for c in crews)
    if weather["stops_work"]:
        binding = {"kind": "weather", "text": f"Weather: {weather['reason']} Nothing is assigned."
                   if str(weather["reason"]).endswith(".") else
                   f"Weather: {weather['reason']}. Nothing is assigned."}
    elif need_md > cap_md:
        binding = {"kind": "capacity",
                   "text": (f"Crew capacity: {cap_md:,.0f} man-days available against "
                            f"{need_md:,.0f} needed for everything due. "
                            f"{len(not_reached)} blocks are not reached.")}
    else:
        binding = {"kind": "demand",
                   "text": (f"Demand: everything due is reached with {cap_md - need_md:,.0f} "
                            f"man-days to spare across {len(crews)} {m['crew_label']}s.")}

    date_label = d.strftime("%A %d %B %Y").replace(" 0", " ")
    ranking = sorted(items, key=lambda i: -(i["deferral_idr"] / i["man_days"]))
    why_rows = [{
        "rank": n + 1, "block_label": i["block_label"], "block_id": i["block_id"],
        "division_code": i["division_code"], "activity": i.get("activity") or operation,
        "urgency": i["urgency"], "days_over_round": i["days_over_round"],
        "qty": i["qty"], "unit": i["unit"], "man_days": i["man_days"],
        "tonnes_at_risk": i["tonnes_at_risk"], "value_idr": i["value_idr"],
        "deferral_cost_idr_per_day": i["deferral_cost_idr_per_day"],
        "horizon_days": i["horizon_days"], "deferral_idr": round(i["deferral_idr"]),
        "value_per_man_day_idr": round(i["deferral_idr"] / i["man_days"]),
        "assigned_to": taken.get(i["block_key"]), "road_condition": i.get("road_condition"),
        "rate_per_man_day": i["rate_per_man_day"], "rate_source": i["rate_source"],
        "expected_share": i.get("expected_share"), "p_carried": i.get("p_carried"),
        "deferral_idr_nominal": round(i.get("deferral_idr_nominal") or i["deferral_idr"]),
    } for n, i in enumerate(ranking[:40])]

    total_qty = sum(c["totals"]["qty"] for c in crew_rows)
    total_t = sum(c["totals"]["tonnes"] for c in crew_rows)
    exp_adh = weather["expected_adherence_pct"]
    used_keys = ["ffb_price_idr_kg", "abw_kg", "man_day_cost_idr", "contiguity_bonus_pct",
                 "crew_transport_km_per_hour", "work_day_hours", "rain_cutoff_mm",
                 "attendance_lookback_days", "use_rain_model", "use_headcount_model",
                 "use_slippage_model", "use_learned_rates", "plan_on_expected_adherence"] + \
        (["herbicide_idr_per_ha"] if operation == "spray" else []) + \
        [a["key"] for a in dem.get("assumptions_used") or []]
    return {
        "available": True,
        "operation": operation, "label": m["label"], "unit": m["unit"],
        "crew_label": m["crew_label"],
        "date": d.isoformat(), "date_label": date_label,
        "is_tomorrow": d == TOMORROW, "inside_ledger": d <= WINDOW_END,
        "window": ops.window(),
        "headline": _headline(m, d, crew_rows, total_qty, total_t, not_reached, nr_cost, weather),
        "crews": crew_rows,
        "totals": {
            "crews": len(crew_rows), "crews_with_work": sum(1 for c in crew_rows if c["blocks"]),
            "present": sum(c["present"] for c in crew_rows),
            "on_roll": sum(c["on_roll"] for c in crew_rows),
            "capacity_md": round(cap_md, 1),
            "used_md": round(sum(c["used_md"] for c in crew_rows), 1),
            "blocks": sum(c["totals"]["blocks"] for c in crew_rows),
            "ha": round(sum(c["totals"]["ha"] for c in crew_rows), 1),
            "qty": round(total_qty, 1), "unit": m["unit"],
            "tonnes": round(total_t, 1),
            "expected_qty": (work_done["expected_qty"] if work_done else
                             round(total_qty * exp_adh / 100, 1) if exp_adh is not None else None),
            "expected_qty_low": work_done["low_qty"] if work_done else None,
            "expected_qty_high": work_done["high_qty"] if work_done else None,
            "expected_tonnes": (round(sum(c["totals"].get("expected_tonnes") or 0 for c in crew_rows), 1)
                                if work_done else
                                round(total_t * exp_adh / 100, 1) if exp_adh is not None else None),
            "value_idr": sum(c["totals"]["value_idr"] for c in crew_rows),
            "deferral_idr": round(achieved),
            "blocks_due": len(items), "man_days_due": round(need_md, 1),
        },
        "not_reached": {
            "blocks": len(not_reached),
            "ha": round(sum(i["planted_ha"] or 0 for i in not_reached), 1),
            "qty": round(sum(i["qty"] for i in not_reached), 1), "unit": m["unit"],
            "tonnes_at_risk": round(sum(i["tonnes_at_risk"] for i in not_reached), 1),
            "deferral_cost_idr_per_day": round(nr_cost),
            "deferral_cost_idr_per_week": round(nr_cost * 7),
            "tonnes_lost_per_week": round(sum(
                i["tonnes_at_risk"] * (i["deferral_cost_idr_per_day"] / i["value_idr"] if i["value_idr"] else 0)
                for i in not_reached) * 7, 2),
            "overdue": sum(1 for i in not_reached if i["urgency"] >= 1.0),
            "block_labels": [i["block_label"] for i in not_reached],
            "top": [{"block_label": i["block_label"], "block_id": i["block_id"],
                     "division_code": i["division_code"], "urgency": i["urgency"],
                     "days_over_round": i["days_over_round"], "qty": i["qty"],
                     "man_days": i["man_days"],
                     "deferral_cost_idr_per_day": i["deferral_cost_idr_per_day"]}
                    for i in sorted(not_reached, key=lambda x: -x["deferral_cost_idr_per_day"])[:12]],
        },
        "weather": weather,
        "why": {
            "demand_signal": m["demand"], "rate": m["rate"],
            "ranking": why_rows,
            "binding_constraint": binding,
            "objective": {
                "method": ("greedy by deferral value per man-day, round-robin across "
                           f"{m['crew_label']}s, then a swap-improvement pass"
                           f" ({res['swaps']} swaps accepted)"),
                "terms": {
                    "deferral_idr": round(achieved),
                    "contiguity_idr": sum(c["totals"]["contiguity_idr"] for c in crew_rows),
                    "travel_idr": sum(c["totals"]["travel_idr"] for c in crew_rows),
                },
                "upper_bound_idr": round(ub), "achieved_idr": round(achieved),
                "gap_pct": gap,
                "gap_reading": (("within a few percent of the relaxed bound; the greedy "
                                 "choice is as good as the arithmetic allows.")
                                if gap is not None and gap <= 5 else
                                ("the gap is the price of contiguity, travel and whole "
                                 "blocks: the bound ignores all three.") if gap is not None else None),
                "contiguity": contiguity,
                "formula": (("score = deferral cost per day x horizon x expected share done + contiguity "
                             "bonus - travel; a block is taken by the crew for which score per man-day is highest")
                            if discount else
                            ("score = deferral cost per day x horizon + contiguity bonus - travel; "
                             "a block is taken by the crew for which score per man-day is highest")),
                "discounted_by_work_done": discount,
            },
        },
        "order_refs": order_refs,
        "work_done": work_done,
        "forecast": _forecast_block(operation, d, weather, crew_rows, work_done, av, use_wd, discount),
        "assumptions_used": assumptions.used(sorted(set(used_keys))),
        "overrides": overrides,
        "provenance": ("scheduled: every figure on this plan is computed from the generated "
                       "ledger, the client's real block geometry and areas, and the assumption "
                       "register. Nothing here is a record; a scheduled figure never wears a "
                       "real badge."),
        "note": (f"Tomorrow is {TOMORROW.isoformat()}, the first day beyond the client's own "
                 "data. Present figures are forecasts; edit them and re-run before this "
                 "becomes an assignment.") if d == TOMORROW else
                (f"{d.isoformat()} is inside the ledger. The plan decides on what was knowable "
                 "the evening before, forecasts where they are switched on, and shows what "
                 "actually happened beside them; the Did-it-work panel reads it back."),
    }


def _forecast_block(operation: str, d: date, weather: dict, crew_rows: list, work_done: dict | None,
                    av: dict, use_wd: bool, discount: bool) -> dict:
    """The four forecasts behind this plan, in words a manager can act on."""
    from gis.models import learn, rates
    m = ops.OPERATIONS[operation]
    unit = m["unit"]
    tonnes = operation in ("harvest", "dispatch")
    out: dict = {"inputs": []}
    fc = weather.get("forecast")

    # Rain.
    if fc:
        out["rain"] = {**fc, "in_use": True, "recorded_mm": weather.get("recorded_mm")}
        out["inputs"].append({"input": "Rain", "used": "forecast",
                              "plain": fc["headline"], "grade": fc["grade"]["label"]})
    else:
        used = ("set on the plan" if weather["source"] == "set on the plan" else
                "recorded rain" if weather.get("rain_mm") is not None else "not known")
        out["rain"] = {"in_use": False, "source": weather["source"]}
        out["inputs"].append({"input": "Rain", "used": used, "plain": weather["source"]})

    # Headcount.
    staffed = [c for c in crew_rows if operation != "dispatch"]
    ranged = [c for c in staffed if c.get("present_low") is not None]
    if ranged:
        from gis.models import headcount
        # A total range is not the sum of the crews' ranges: crews miss
        # independently, except in rain, which falls on all of them at once.
        tot = headcount.estate(d, codes=[c["crew_code"] for c in ranged])
        lo, hi = (tot["low"], tot["high"]) if tot.get("available") else (
            sum(c["present_low"] for c in ranged), sum(c["present_high"] for c in ranged))
        ml = sum(c["present"] for c in ranged)
        roll = sum(c["on_roll"] for c in ranged)
        rec = [c["recorded_present"] for c in ranged if c.get("recorded_present") is not None]
        out["headcount"] = {
            "in_use": True, "low": lo, "high": hi, "most_likely": ml, "on_roll": roll,
            "crews": len(ranged),
            "recorded": sum(rec) if len(rec) == len(ranged) and d <= WINDOW_END else None,
            "plain": (f"Expect {lo:,} to {hi:,} of the {roll:,} people on these {len(ranged)} "
                      f"{m['crew_label']}s' rolls to turn up (most likely {ml:,})."),
        }
        out["inputs"].append({"input": "Headcount", "used": "forecast", "plain": out["headcount"]["plain"]})
    elif operation != "dispatch":
        out["headcount"] = {"in_use": False}
        out["inputs"].append({"input": "Headcount", "used": "average",
                              "plain": f"Mean attendance over the last {int(av['attendance_lookback_days'])} days."})

    # Work done.
    if work_done:
        es, lo_s, hi_s = work_done["expected_share"], work_done["low_share"], work_done["high_share"]
        planned = work_done["planned_qty"]
        if tonnes:
            t_plan = sum(c["totals"]["tonnes"] for c in crew_rows)
            t_exp = sum(c["totals"].get("expected_tonnes") or 0 for c in crew_rows)
            amount = (f"about {t_exp:,.0f} t of the {t_plan:,.0f} t planned "
                      f"({lo_s * t_plan:,.0f} to {hi_s * t_plan:,.0f} t)")
        else:
            amount = (f"about {work_done['expected_qty']:,.0f} of the {planned:,.0f} {unit} planned "
                      f"({work_done['low_qty']:,.0f} to {work_done['high_qty']:,.0f})")
        risk = work_done["at_risk"]
        out["work_done"] = {
            "in_use": True, "expected_pct": round(100 * es), "low_pct": round(100 * lo_s),
            "high_pct": round(100 * hi_s), "amount_plain": amount,
            "at_risk": risk[:8], "at_risk_count": work_done["at_risk_count"],
            "discounted": discount,
            "plain": (f"Expect about {round(100 * es)}% of the plan to get done "
                      f"(between {round(100 * lo_s)}% and {round(100 * hi_s)}% in 8 days out of 10): {amount}."),
            "risk_plain": ((f"{work_done['at_risk_count']} of the planned blocks have a 7 in 10 chance or more "
                            "of needing another day to finish.") if work_done["at_risk_count"] else
                           "No planned block has a 7 in 10 chance of needing another day."),
        }
        out["inputs"].append({"input": "Work done", "used": "forecast", "plain": out["work_done"]["plain"]})
    elif operation != "dispatch":
        out["work_done"] = {"in_use": False,
                            "reason": ("switched off in the register" if int(av.get("use_slippage_model", 0)) != 1
                                       else "nothing is assigned" if not any(c["blocks"] for c in crew_rows)
                                       else "the forecast did not beat the ledger average for this work")}
        out["inputs"].append({"input": "Work done", "used": "ledger average", "plain": weather["basis"]})

    # Speeds.
    if operation in rates.RATE_OPS:
        if operation == "harvest":
            moved = [b for c in crew_rows for b in c["blocks"] if "block's record" in (b.get("rate_source") or "")]
            in_use = bool(moved)
            out["speeds"] = {"in_use": in_use, "unit": "block",
                             "plain": (f"{len(moved)} of the planned blocks have a harvest pace learned from their "
                                       "own records, so their man-days are sized to it.") if in_use else
                                      "Harvest rates come from the productivity model's targets alone."}
        else:
            fast = [c for c in crew_rows if c.get("speed_factor", 1) >= 1.03]
            slow = [c for c in crew_rows if c.get("speed_factor", 1) <= 0.97]
            in_use = any(abs(c.get("speed_factor", 1) - 1) > 1e-9 for c in crew_rows)
            out["speeds"] = {
                "in_use": in_use, "unit": m["crew_label"],
                "faster": [{"crew_code": c["crew_code"], "factor": c["speed_factor"]} for c in fast],
                "slower": [{"crew_code": c["crew_code"], "factor": c["speed_factor"]} for c in slow],
                "plain": ((f"{len(fast)} {m['crew_label']}s on this plan work faster than the book and "
                           f"{len(slow)} slower, so each is given the ground it can actually cover.")
                          if in_use else "Every crew is planned at the textbook rate."),
            }
        out["inputs"].append({"input": "Crew speed", "used": "learned" if out["speeds"]["in_use"] else "textbook",
                              "plain": out["speeds"]["plain"]})

    if weather.get("spray_call"):
        out["spray_call"] = weather["spray_call"]

    # What this means, in two or three sentences.
    parts = []
    if fc:
        w = fc["chances"]["washoff" if operation == "spray" else "heavy"]
        parts.append(f"Rain: {w['label'].lower()} is {w['words']} ({w['pct']}%).")
    if out.get("spray_call"):
        parts.append(out["spray_call"]["plain"])
    if out.get("headcount", {}).get("in_use"):
        parts.append(out["headcount"]["plain"])
    if out.get("work_done", {}).get("in_use"):
        parts.append(out["work_done"]["plain"])
    out["summary"] = " ".join(parts)
    out["badge"] = "predicted"
    out["note"] = ("Rain is learned from real forecasts and real rainfall. Headcount, work done and crew "
                   "speeds are learned from the generated ledger, so they show how the method works on "
                   "this estate's shape of data, not yet what this estate's crews really do.")
    del learn
    return out


def _headline(m, d, crew_rows, total_qty, total_t, not_reached, nr_cost, weather=None) -> str:
    working = [c for c in crew_rows if c["blocks"]]
    present = sum(c["present"] for c in crew_rows)
    unit = m["unit"]
    q = f"{total_t:,.1f} t" if unit in ("bunches", "tonnes") else f"{total_qty:,.0f} {unit}"
    lead = (f"{d.strftime('%A %d %B').replace(' 0', ' ')}: {len(working)} {m['crew_label']}s "
            f"on shift, {present:,} present, {sum(c['totals']['blocks'] for c in working)} blocks, "
            f"{sum(c['totals']['ha'] for c in working):,.0f} ha, {q}.")
    if not_reached:
        lead += (f" Not reached: {len(not_reached)} blocks, deferring costs "
                 f"{nr_cost * 7 / 1e6:,.1f}M IDR this week.")
    if weather and weather.get("stops_work"):
        what = "Spraying held" if weather.get("spray_call") else "Field work called off"
        why = str(weather["reason"]).removeprefix("Hold spraying. ")
        lead = (f"{d.strftime('%A %d %B').replace(' 0', ' ')}: {what}. {why[:1].upper()}{why[1:]}"
                + ("" if why.endswith(".") else "."))
    return lead


def cost_of_deferral(operation: str, block: str, days: int = 1, on: str | None = None) -> dict:
    """What waiting costs on one block, from the same arithmetic as the plan."""
    m = ops.OPERATIONS.get(operation)
    if not m:
        return {"available": False, "reason": f"No operation {operation!r}."}
    dem = ops.demand(operation, on)
    if not dem.get("available"):
        return dem
    days = max(1, min(int(days or 1), 60))
    hit = next((i for i in dem["items"] + dem["not_due"]
                if str(i["block_label"]) == str(block) or i["block_key"] == block), None)
    if hit is None:
        return {"available": False,
                "reason": f"Block {block!r} carries no {operation} demand on {dem['date']}.",
                "hint": "Block labels look like '32-51'."}
    per_day = hit["deferral_cost_idr_per_day"]
    return {
        "available": True, "operation": operation, "date": dem["date"],
        "block_label": hit["block_label"], "block_id": hit["block_id"],
        "division_code": hit["division_code"], "planted_ha": hit["planted_ha"],
        "activity": hit.get("activity") or operation,
        "urgency": hit["urgency"], "days_since": hit["days_since"],
        "target_days": hit["target_days"], "days_over_round": hit["days_over_round"],
        "qty": hit["qty"], "unit": hit["unit"], "man_days": hit["man_days"],
        "tonnes_at_risk": hit["tonnes_at_risk"], "value_idr": hit["value_idr"],
        "deferral_cost_idr_per_day": per_day,
        "days": days, "deferral_cost_idr": round(per_day * days),
        "deferral_cost_million_idr": round(per_day * days / 1e6, 2),
        "is_due": hit in dem["items"],
        "assumptions_used": dem["assumptions_used"],
        "provenance": ("scheduled: computed from the ledger, the block's real area and "
                       "the assumption register."),
        "note": ("Cost per day is the block's value at risk times the loss rate for the "
                 "operation, weighted by how far past its round it is."),
    }


def clear_cache() -> int:
    with _LOCK:
        n = len(_CACHE)
        _CACHE.clear()
        _ADJ.clear()
    return n
