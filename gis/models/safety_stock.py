"""Reorder points and safety stock, and the replay that decides whether they are used.

The question a storekeeper asks is not "what is the average use". It is:
if I order today, how much could go out before the delivery arrives, and how
sure do I need to be? Both halves are uncertain, so both are drawn.

The method
----------
    1. Draw 2,000 lead times for an order placed today (leadtime.py): the
       supplier's usual time, its spread, and its chance of arriving very late.
    2. For each, add up the use over that many days (consumption.py), with the
       model's own past errors on top, or counted as jobs for occasional use.
       Fertiliser's use is the programme, so its spread is how far past
       rounds' totals strayed from theirs, not the weekly errors, which mostly
       measure when in the week a round happened to start.
    3. The reorder point is the service level's share of those totals: at 95%,
       19 draws in 20 are covered. Safety stock is the reorder point less the
       average total.
    4. When stock on hand plus stock on order falls to the reorder point, order
       up to it plus `order_cover_weeks` of use, rounded to the bag, can or
       tanker. Fertiliser is ordered in vessel lots against the programme.

Why the buffer is that size: rerun step 2 with the lead time fixed at its
middle. What remains is the part use explains; the rest is the supplier's.

The test that decides whether it is used: the replay
----------------------------------------------------
Twelve months, 2024-06-01 to 2025-05-23, replayed twice from the recorded
stock and open orders on the first day. Both replays meet the RECORDED issues.
A replayed order takes the receipts of the recorded order from the same
supplier placed nearest in date - what that lane actually did then, not a draw
from the model being judged. A day the store cannot meet is a rush buy at the
recorded premium.

    old way   SAP's settings: reorder at MINBE, fixed lot; fertiliser ordered
              PLIFZ + 28 days before each round, in vessel lots
    new way   the reorder point above, refitted at each month start on what
              was known before it

A material uses the learned reorder point only where its own replay costs less
and keeps the service level within 3 points; everywhere else SAP's settings
stay. The history was generated under the old way, so replaying the old way
must reproduce the recorded rush buys. If it does not, the replay is wrong and
neither result is trusted.
"""

import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from threading import Lock

import numpy as np

from gis import assumptions
from gis.build_operations import TOMORROW, WINDOW_END
from gis.models import consumption, leadtime, learn, mm

log = logging.getLogger("estate-command.models.safety_stock")

_CACHE: dict = {}
_LOCK = Lock()

N_DRAWS = 2000
N_PATHS = 1000
H_MAX = 200
PROJECT_DAYS = 90
PROJECT_DAYS_PROGRAMME = 150      # long enough to show the next fertiliser round arrive
SEARCH_DAYS = 150
REPLAY_START = date(2024, 6, 1)
SERVICE_TOLERANCE = 3.0
COST_LEVELS = (80, 85, 90, 95, 97, 99)
SL_KEY = {"FERT": "service_level_fertiliser", "AGCH": "service_level_agrochemical",
          "FUEL": "service_level_fuel", "SPARE": "service_level_parts"}
PD_BUFFER_DAYS = 28
PD_LOT_SPACING_DAYS = 3


def settings() -> dict:
    av = assumptions.values()
    return {"service": {g: float(av[k]) for g, k in SL_KEY.items()},
            "holding": float(av["holding_cost_pct_yr"]) / 100.0,
            "cover_days": int(round(float(av["order_cover_weeks"]) * 7)),
            "switch": int(av["use_stock_model"]) == 1,
            "prior_orders": float(av["leadtime_prior_orders"])}


def _key(s: dict) -> tuple:
    return (tuple(sorted(s["service"].items())), s["holding"], s["cover_days"], s["prior_orders"])


def _round_up(q: float, step: float) -> float:
    return math.ceil(q / step - 1e-9) * step if step > 0 else q


# ── demand during lead time ────────────────────────────────────────────────

def _eps(matnr: str, before: date, n: int, rng) -> np.ndarray:
    e = consumption.ratio_errors(matnr, before)
    if len(e) >= 8:
        return e[rng.integers(0, len(e), n)]
    return np.exp(rng.normal(0.0, 0.3, n))


def ddlt(matnr: str, d: date, fit: dict, known: date, rng, n: int = N_DRAWS, extra_days: int = 0,
         forecast: np.ndarray | None = None, offset: int = 0) -> dict:
    """Use during lead time for an order placed on d, as n draws, with what was known on `known`.

    `forecast` is a daily forecast made on `known`; d is `offset` days after it. Without
    one, the forecast is made on d itself, which is only right when d is `known`.

    Every material is ordered one reorder at a time against the whole lead-time draw,
    fertiliser included: it is ordered as the programme comes inside the lead time, so
    the draw sets how early each part of the round is bought, a safety lead time.
    """
    m = mm.state()["materials"][matnr]
    L = leadtime.draws(fit, m["primary_lifnr"], d, n, rng)
    hmax = int(min(H_MAX, L.max()))
    if forecast is None:
        daily = consumption.expected_daily(matnr, d, hmax + 1 + extra_days)
    else:
        daily = forecast[offset:offset + hmax + 1 + extra_days]
    cum = np.concatenate([[0.0], np.cumsum(daily)])
    Li = np.minimum(L.astype(int), len(cum) - 1)
    Lmed = int(np.median(Li))
    if consumption.kind(matnr) == "jobs":
        size = consumption.job_size(matnr, known)
        D = rng.poisson(cum[Li] / size) * size
        Dfix = rng.poisson(np.full(n, cum[Lmed] / size)) * size
    elif m["dismm"] == "PD":
        spread = consumption.round_spread(matnr, known)
        e = spread[rng.integers(0, len(spread), n)]
        Dfix = cum[Lmed] * e
        D = cum[Li] * e
    else:
        e = _eps(matnr, known, n, rng)
        Dfix = cum[Lmed] * e
        D = cum[Li] * e
    return {"L": L, "D": D, "Dfix": Dfix, "daily": daily, "cum": cum, "Lmed": Lmed}


def policy_at(matnr: str, d: date, fit: dict, known: date, s: dict, rng, n: int = N_DRAWS,
              forecast: np.ndarray | None = None, offset: int = 0) -> dict:
    m = mm.state()["materials"][matnr]
    sl = s["service"][m["matkl"]]
    pd = m["dismm"] == "PD"
    r = ddlt(matnr, d, fit, known, rng, n, extra_days=0 if pd else s["cover_days"], forecast=forecast, offset=offset)
    rop = float(np.percentile(r["D"], sl))
    mean = float(r["D"].mean())
    ss = max(0.0, rop - mean)
    rop_d = float(np.percentile(r["Dfix"], sl))
    ss_demand = min(ss, max(0.0, rop_d - float(r["Dfix"].mean())))
    cover = 0.0 if pd else float(r["daily"][r["Lmed"]:r["Lmed"] + s["cover_days"]].sum())
    return {"rop": rop, "mean": mean, "ss": ss, "ss_demand": ss_demand, "ss_supply": ss - ss_demand,
            "cover": cover, "order_up_to": rop + cover, "L": r["L"], "D": r["D"], "Lmed": r["Lmed"],
            "daily": r["daily"], "service": sl}


def order_qty(m: dict, pol: dict, position: float, on_hand: float) -> float:
    lot_min = m["bstfe"] if m["dismm"] == "PD" else m["bstrf"]
    q = max(pol["order_up_to"] - position, lot_min)
    q = _round_up(q, m["bstrf"])
    if m["max_stock"]:
        room = m["max_stock"] - position
        q = min(q, math.floor(room / m["bstrf"]) * m["bstrf"])
    return max(q, 0.0)


# ── the replay ─────────────────────────────────────────────────────────────

def _lanes() -> dict:
    """Recorded normal orders per supplier, with the receipt schedule each actually had."""
    with _LOCK:
        if "lanes" in _CACHE:
            return _CACHE["lanes"]
    lanes = defaultdict(list)
    for po in mm.state()["pos"]:
        if po["bsart"] != "NB" or po["done"] is None:
            continue
        sched = [((when - po["bedat"]).days, q / po["menge"]) for when, q in po["receipts"]]
        lanes[po["lifnr"]].append({"ord": po["bedat"].toordinal(), "matnr": po["matnr"], "sched": sched})
    for v in lanes.values():
        v.sort(key=lambda x: x["ord"])
    with _LOCK:
        _CACHE["lanes"] = dict(lanes)
    return dict(lanes)


def _nearest(lifnr: str, matnr: str, d: date) -> list:
    cands = _lanes()[lifnr]
    o = d.toordinal()
    best = min(cands, key=lambda x: (abs(x["ord"] - o), x["matnr"] != matnr))
    return best["sched"]


def _replay(matnr: str, way: str, s: dict, trace: list | None = None) -> dict:
    st = mm.state()
    m = st["materials"][matnr]
    use = st["use"][matnr]
    i0, i1 = mm.day(REPLAY_START), mm.N_DAYS - 1
    on_hand = float(st["on_hand"][matnr][i0 - 1])
    arrivals: dict = defaultdict(float)
    open_qty = 0.0
    for po in st["pos"]:
        if po["matnr"] != matnr or po["bsart"] != "NB" or po["bedat"] >= REPLAY_START:
            continue
        for when, q in po["receipts"]:
            if when >= REPLAY_START:
                arrivals[mm.day(when)] += q
                open_qty += q
    premium = mm.rush_premium(m["matkl"])
    loss_rate = mm.storage_loss_rate(matnr, REPLAY_START)["rate_per_month"] / 30.4375
    h_day = s["holding"] / 365.0
    rng = np.random.default_rng(sum(map(ord, matnr + way)) * 104729)

    triggers, planned = {}, defaultdict(list)
    if way == "old" and m["dismm"] == "PD":
        rounds = defaultdict(float)
        for r in st["reservations"]:
            if r["matnr"] == matnr:
                rounds[r["bdter"]] += r["bdmng"]
        for bdter, need in rounds.items():
            trig = bdter - timedelta(days=m["plifz"] + PD_BUFFER_DAYS)
            if REPLAY_START <= trig <= WINDOW_END:
                triggers[mm.day(trig)] = need

    rush = {"count": 0, "qty": 0.0, "cost": 0.0, "days": []}
    holding = loss_cost = value_days = 0.0
    orders, cycles, cycle_rush = 0, 0, set()
    fits: dict = {}
    for i in range(i0, i1 + 1):
        d = mm.date_of(i)
        q_in = arrivals.pop(i, 0.0)
        if q_in > 0:
            on_hand += q_in
            open_qty -= q_in
            cycles += 1
        need = float(use[i])
        if need > 0 and on_hand + 1e-9 < need:
            short = _round_up(need - on_hand, m["rush_lot"])
            rush["count"] += 1
            rush["qty"] += short
            rush["cost"] += short * m["verpr"] * premium
            rush["days"].append(i)
            cycle_rush.add(cycles)
            on_hand += short
        on_hand -= need
        lost = max(0.0, on_hand) * loss_rate
        on_hand -= lost
        loss_cost += lost * m["verpr"]
        holding += max(0.0, on_hand) * m["verpr"] * h_day
        value_days += max(0.0, on_hand) * m["verpr"]

        placed = []
        position = on_hand + open_qty
        if way == "old":
            if m["dismm"] == "PD":
                if i in triggers:
                    gap = triggers[i] - on_hand - open_qty + m["eisbe"]
                    if gap > 0:
                        k = max(1, math.ceil(gap / m["bstfe"]))
                        each = _round_up(gap / k, m["bstrf"])
                        for j in range(k):
                            planned[i + PD_LOT_SPACING_DAYS * j].append(each)
                placed = planned.pop(i, [])
            else:
                n = 0
                while position <= m["minbe"] + 1e-9 and n < 3:
                    if m["max_stock"] and position + m["bstfe"] > m["max_stock"]:
                        break
                    placed.append(m["bstfe"])
                    position += m["bstfe"]
                    n += 1
        else:
            month = date(d.year, d.month, 1)
            if month not in fits:
                fits[month] = leadtime.fit(month, s["prior_orders"])
            pol = policy_at(matnr, d, fits[month], month, s, rng, n=600)
            if position <= pol["rop"]:
                q = order_qty(m, pol, position, on_hand)
                if q > 0:
                    placed.append(q)
        for q in placed:
            orders += 1
            open_qty += q
            for offset, share in _nearest(m["primary_lifnr"], matnr, d):
                if i + offset <= i1:          # later receipts stay on order when the replay ends
                    arrivals[i + offset] += q * share
        if trace is not None:
            trace.append({"date": d.isoformat(), "on_hand": round(on_hand, 1), "position": round(on_hand + open_qty, 1),
                          "reorder_point": round(pol["rop"], 1) if way == "new" else m["minbe"],
                          "ordered": round(sum(placed), 1), "used": round(need, 1)})
    days = i1 - i0 + 1
    service = 100.0 * (1.0 - len(cycle_rush) / max(cycles, 1))
    total = holding + loss_cost + rush["cost"]
    return {"way": way, "rush_orders": rush["count"], "rush_qty": round(rush["qty"], 1),
            "rush_cost_idr": round(rush["cost"]), "holding_cost_idr": round(holding),
            "storage_loss_idr": round(loss_cost), "total_cost_idr": round(total),
            "avg_stock_value_idr": round(value_days / days), "orders": orders, "receipts": cycles,
            "cycles_with_rush": len(cycle_rush), "service_pct": round(service, 1),
            "rush_days": rush["days"], "closing_stock": round(on_hand, 1)}


def replay() -> dict:
    s = settings()
    key = ("replay", _key(s))
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    if not mm.available():
        return {"available": False, "reason": "No MM records."}
    st = mm.state()
    per = {}
    recorded_total = matched_total = 0
    for matnr, m in st["materials"].items():
        old = _replay(matnr, "old", s)
        new = _replay(matnr, "new", s)
        recorded = {mm.day(po["receipts"][0][0]) for po in st["pos"]
                    if po["matnr"] == matnr and po["bsart"] == "RUSH" and po["receipts"]
                    and po["receipts"][0][0] >= REPLAY_START}
        matched = len(recorded & set(old["rush_days"]))
        recorded_total += len(recorded)
        matched_total += matched
        target = s["service"][m["matkl"]]
        imp = learn.improvement_pct(new["total_cost_idr"], old["total_cost_idr"]) if old["total_cost_idr"] else None
        service_ok = new["service_pct"] >= target - SERVICE_TOLERANCE or new["service_pct"] >= old["service_pct"]
        better = bool(imp is not None and imp > 0 and service_ok)
        per[matnr] = {
            "matnr": matnr, "maktx": m["maktx"], "group": m["matkl"], "target_service_pct": target,
            "old": {k: v for k, v in old.items() if k != "rush_days"},
            "new": {k: v for k, v in new.items() if k != "rush_days"},
            "recorded_rush_orders": len(recorded), "old_replay_reproduces": matched,
            "saving_idr": old["total_cost_idr"] - new["total_cost_idr"], "improvement_pct": imp,
            "service_ok": service_ok, "better": better,
            "plain": _replay_words(m, old, new, target, better, service_ok),
        }
    old_total = sum(p["old"]["total_cost_idr"] for p in per.values())
    new_total = sum(p["new"]["total_cost_idr"] for p in per.values())
    # The switch is per material, so the estate's cost is the better of the two on each.
    used_total = sum(p["new"]["total_cost_idr"] if p["better"] else p["old"]["total_cost_idr"] for p in per.values())
    imp_all = learn.improvement_pct(new_total, old_total)
    imp_used = learn.improvement_pct(used_total, old_total)
    groups = {}
    for g in SL_KEY:
        ps = [p for p in per.values() if p["group"] == g]
        cyc_old = sum(p["old"]["receipts"] for p in ps)
        cyc_new = sum(p["new"]["receipts"] for p in ps)
        groups[g] = {
            "label": mm.GROUPS[g], "target_service_pct": s["service"][g],
            "old_service_pct": round(100 * (1 - sum(p["old"]["cycles_with_rush"] for p in ps) / max(cyc_old, 1)), 1),
            "new_service_pct": round(100 * (1 - sum(p["new"]["cycles_with_rush"] for p in ps) / max(cyc_new, 1)), 1),
            "old_cost_idr": sum(p["old"]["total_cost_idr"] for p in ps),
            "new_cost_idr": sum(p["new"]["total_cost_idr"] for p in ps),
            "old_rush_orders": sum(p["old"]["rush_orders"] for p in ps),
            "new_rush_orders": sum(p["new"]["rush_orders"] for p in ps),
            "materials_better": sum(1 for p in ps if p["better"]), "materials": len(ps),
        }
    reproduces = matched_total / recorded_total if recorded_total else 1.0
    trustworthy = reproduces >= 0.9
    grade = learn.grade(imp_used if trustworthy else None)
    out = {
        "available": True, "model": "safety_stock", "trained_on": "synthetic",
        "from": REPLAY_START.isoformat(), "to": WINDOW_END.isoformat(),
        "materials": per, "groups": groups,
        "old_cost_idr": old_total, "new_cost_idr": new_total, "used_cost_idr": used_total,
        "improvement_pct": imp_used, "improvement_all_new_pct": imp_all, "grade": grade,
        "materials_better": sum(1 for p in per.values() if p["better"]),
        "replay_check": {"recorded_rush_orders": recorded_total, "reproduced": matched_total,
                         "share": round(reproduces, 3), "bar": 0.9, "passes": trustworthy},
        "settings": {"service": s["service"], "holding_pct_yr": round(100 * s["holding"], 1),
                     "cover_weeks": s["cover_days"] / 7},
        "plain": {
            "headline": (f"Replayed from {REPLAY_START.strftime('%d %B %Y').lstrip('0')} to "
                         f"{WINDOW_END.strftime('%d %B %Y').lstrip('0')} against the recorded use: SAP's settings "
                         f"cost {_rp(old_total)} in stock held, storage loss and rush buys. Using the learned reorder "
                         f"point on the {sum(1 for p in per.values() if p['better'])} materials where it did better "
                         f"costs {_rp(used_total)}, {imp_used}% less."),
            "check": (f"The replay of SAP's settings reproduced {matched_total} of the {recorded_total} rush buys "
                      f"the store actually made ({reproduces:.0%}; the bar is 90%)"
                      + (", so the replay can be trusted." if trustworthy else ". The replay is not trusted.")),
        },
    }
    with _LOCK:
        _CACHE[key] = out
    return out


def _rp(v: float) -> str:
    if abs(v) >= 1e9:
        return f"Rp {v / 1e9:,.2f} bn"
    if abs(v) >= 1e6:
        return f"Rp {v / 1e6:,.0f} m"
    return f"Rp {v:,.0f}"


def _replay_words(m, old, new, target, better, service_ok) -> str:
    base = (f"{m['maktx']}: SAP's settings needed {old['rush_orders']} rush buys and cost {_rp(old['total_cost_idr'])}; "
            f"the learned reorder point needed {new['rush_orders']} and cost {_rp(new['total_cost_idr'])}")
    if better:
        return base + f", keeping {new['service_pct']:.0f}% of cycles free of a rush buy (target {target:.0f}%)."
    if not service_ok:
        return base + f", but only {new['service_pct']:.0f}% of cycles stayed free of a rush buy against {target:.0f}%, so SAP's settings stay."
    return base + ", which is not cheaper, so SAP's settings stay."


def in_use(matnr: str) -> bool:
    s = settings()
    if not s["switch"]:
        return False
    r = replay()
    return bool(r.get("available") and r["replay_check"]["passes"] and r["materials"][matnr]["better"])


# ── the material, today ────────────────────────────────────────────────────

def _open_orders(matnr: str, on: date) -> list[dict]:
    out = []
    for po in mm.state()["pos"]:
        if po["matnr"] != matnr or po["bsart"] != "NB" or po["done"] is not None:
            continue
        remaining = po["menge"] - po["received"]
        if remaining > 0:
            out.append({**po, "remaining": remaining})
    return out


def material(matnr: str, on: date | None = None) -> dict:
    """Stock outlook, reorder point, order-by date and the cost of the service level, for one material."""
    on = on or TOMORROW
    s = settings()
    key = ("material", matnr, on.toordinal(), _key(s), s["switch"])
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    st = mm.state()
    m = st["materials"][matnr]
    v = st["vendors"][m["primary_lifnr"]]
    fit = leadtime.fit(on, s["prior_orders"])
    rng = np.random.default_rng(int(sum(map(ord, matnr))) * 7919 + on.toordinal())
    on_hand = float(st["stock"][matnr])
    opens = _open_orders(matnr, on)
    on_order = sum(o["remaining"] for o in opens)

    horizon_days = SEARCH_DAYS + H_MAX + s["cover_days"] + 1
    daily = consumption.expected_daily(matnr, on, horizon_days)
    cum = np.concatenate([[0.0], np.cumsum(daily)])

    # Order-by: the first day the expected position meets that day's reorder point.
    order_by = None
    seed = int(sum(map(ord, matnr))) * 7919 + on.toordinal()
    for t in range(0, SEARCH_DAYS + 1, 1):
        position = on_hand + on_order - cum[t]
        # The reorder point for an order placed on day t, on today's forecast. The same
        # seeded draws are used again below, so a reorder point sitting on the edge of a
        # round's first-day spike cannot flip between the search and the answer.
        r = ddlt(matnr, on + timedelta(days=t), fit, on, np.random.default_rng(seed + t), N_DRAWS,
                 extra_days=0 if m["dismm"] == "PD" else s["cover_days"], forecast=daily, offset=t)
        if position <= float(np.percentile(r["D"], s["service"][m["matkl"]])):
            order_by = t
            break
        if t > 30 and cum[t] == cum[min(t + H_MAX, len(cum) - 1)]:
            break          # nothing more is expected; no order is needed in the window
    # The full policy on the order-by day (or today, when nothing is due).
    t_pol = order_by if order_by is not None else 0
    pol = policy_at(matnr, on + timedelta(days=t_pol), fit, on, s, np.random.default_rng(seed + t_pol),
                    forecast=daily, offset=t_pol)
    pos_then = on_hand + on_order - cum[t_pol]
    qty = order_qty(m, pol, pos_then, on_hand - cum[t_pol]) if order_by is not None else 0.0

    # Projection: if nothing more is ordered, where does stock go?
    n = N_PATHS
    days = PROJECT_DAYS_PROGRAMME if m["dismm"] == "PD" else PROJECT_DAYS
    arr = np.zeros((n, days + 1))
    for o in opens:
        rem = leadtime.remaining_draws(fit, o, on, n, rng)
        idx = np.minimum(rem.astype(int), days)
        mask = rem.astype(int) <= days
        np.add.at(arr, (np.arange(n)[mask], idx[mask]), o["remaining"])
    arr_cum = np.cumsum(arr, axis=1)
    if consumption.kind(matnr) == "jobs":
        size = consumption.job_size(matnr, on)
        dem = rng.poisson(np.tile(daily[:days] / size, (n, 1))) * size
    elif m["dismm"] == "PD":
        spread = consumption.round_spread(matnr, on)
        dem = np.outer(spread[rng.integers(0, len(spread), n)], daily[:days])
    else:
        dem = np.outer(_eps(matnr, on, n, rng), daily[:days])
    dem_cum = np.concatenate([np.zeros((n, 1)), np.cumsum(dem, axis=1)], axis=1)
    paths = on_hand + arr_cum - dem_cum
    band = np.percentile(paths, [10, 50, 90], axis=0)
    L0 = leadtime.draws(fit, m["primary_lifnr"], on, n, rng).astype(int)
    Lc = np.minimum(L0, days)
    run_min = np.minimum.accumulate(paths, axis=1)
    p_out = float((run_min[np.arange(n), Lc] < 0).mean())
    first_out = None
    below = (band[1] < 0).nonzero()[0]
    if len(below):
        first_out = int(below[0])

    use30 = float(daily[:30].sum())
    cover_days = on_hand / (use30 / 30) if use30 > 0 else None
    if order_by == 0:
        status = "order_now"
    elif order_by is not None and order_by <= 7:
        status = "this_week"
    elif m["dismm"] == "VB" and on_hand > 2.0 * (pol["rop"] + max(pol["cover"], m["bstfe"])) and on_hand > 0:
        status = "overstocked"
    else:
        status = "covered"

    learned = in_use(matnr)
    rep = replay()
    rp = (rep.get("materials") or {}).get(matnr) or {}
    unit = m["meins"].lower()
    sap = {"minbe": m["minbe"], "eisbe": m["eisbe"], "plifz": m["plifz"], "bstfe": m["bstfe"], "dismm": m["dismm"]}
    if m["dismm"] == "PD":
        nxt = sorted({r["bdter"] for r in st["reservations"] if r["matnr"] == matnr and r["bdter"] >= on})
        sap["next_round"] = nxt[0].isoformat() if nxt else None
        sap["order_date"] = (nxt[0] - timedelta(days=m["plifz"] + PD_BUFFER_DAYS)).isoformat() if nxt else None

    costs = _cost_curve(m, pol, daily, s)
    round_info = None
    if m["dismm"] == "PD":
        nxt = sorted({r["bdter"] for r in st["reservations"] if r["matnr"] == matnr and r["bdter"] >= on})
        if nxt:
            prog = sum(r["bdmng"] for r in st["reservations"] if r["matnr"] == matnr and r["bdter"] == nxt[0])
            round_info = {"date": nxt[0].isoformat(), "programme": round(prog, 1),
                          "expected": round(float(daily[:(nxt[0] - on).days + H_MAX].sum()), 1)}
    lt = leadtime.summary(fit, m["primary_lifnr"], on + timedelta(days=t_pol))
    out = {
        "available": True, "matnr": matnr, "maktx": m["maktx"], "group": m["matkl"], "group_label": m["group_label"],
        "unit": m["meins"], "date": on.isoformat(), "kind": consumption.kind(matnr),
        "supplier": {"lifnr": v["lifnr"], "name": v["name"], "route": v["route"], **lt},
        "on_hand": round(on_hand, 1), "on_order": round(on_order, 1), "value_idr": round(on_hand * m["verpr"]),
        "use_30d": round(use30, 1), "days_of_cover": round(cover_days) if cover_days is not None else None,
        "reorder_point": round(pol["rop"], 1), "safety_stock": round(pol["ss"], 1),
        "safety_from_supplier": round(pol["ss_supply"], 1), "safety_from_use": round(pol["ss_demand"], 1),
        "expected_use_in_lead_time": round(pol["mean"], 1), "service_level_pct": pol["service"],
        "order_by_days": order_by, "order_by": (on + timedelta(days=order_by)).isoformat() if order_by is not None else None,
        "order_qty": round(qty, 1), "status": status,
        "chance_out_before_delivery": round(p_out, 3), "median_runs_out_in_days": first_out,
        "open_orders": [{"ebeln": o["ebeln"], "bedat": o["bedat"].isoformat(), "eindt": o["eindt"].isoformat(),
                         "remaining": round(o["remaining"], 1)} for o in opens],
        "projection": {"days": days, "p10": np.round(band[0], 1).tolist(), "p50": np.round(band[1], 1).tolist(),
                       "p90": np.round(band[2], 1).tolist(), "reorder_point": round(pol["rop"], 1)},
        "sap": sap, "learned_in_use": learned, "policy_in_force": "learned" if learned else "sap",
        "round": round_info,
        "replay": rp, "cost_curve": costs,
    }
    out["plain"] = _material_words(out, m, unit)
    with _LOCK:
        _CACHE[key] = out
    return out


def _cost_curve(m: dict, pol: dict, daily: np.ndarray, s: dict) -> dict:
    """Approximate yearly cost of each service level: stock held against rush buys prevented."""
    D = pol["D"]
    annual = float(daily[:365].sum())
    lot = max(pol["cover"], m["bstfe"])
    cycles = annual / lot if lot > 0 else 0.0
    loss = mm.storage_loss_rate(m["matnr"])["rate_per_month"] * 12
    premium = mm.rush_premium(m["matkl"])
    rows = []
    for lvl in COST_LEVELS:
        rop = float(np.percentile(D, lvl))
        ss = max(0.0, rop - float(D.mean()))
        short = float(np.maximum(D - rop, 0).mean())
        hold = (ss + lot / 2) * m["verpr"] * (s["holding"] + loss)
        rush = short * cycles * m["verpr"] * premium
        rows.append({"service_pct": lvl, "safety_stock": round(ss, 1), "holding_idr": round(hold),
                     "rush_idr": round(rush), "total_idr": round(hold + rush)})
    best = min(rows, key=lambda r: r["total_idr"])
    chosen = min(rows, key=lambda r: abs(r["service_pct"] - pol["service"]))
    extra = chosen["total_idr"] - best["total_idr"]
    if chosen["service_pct"] == best["service_pct"] or extra < 1e6:
        words = (f"On these figures the register's {pol['service']:.0f}% costs about the same as the cheapest level "
                 f"({best['service_pct']}%).")
    else:
        words = (f"The cheapest service level on these figures is {best['service_pct']}%. The register's "
                 f"{pol['service']:.0f}% costs about {_rp(extra)} a year more, the price of fewer rush buys.")
    return {"rows": rows, "cheapest_pct": best["service_pct"], "chosen_pct": pol["service"],
            "extra_over_cheapest_idr": extra, "plain": words}


def _qty_words(q: float, unit: str) -> str:
    if q <= 0:
        return "none"
    if unit == "kg" and q >= 1000:
        return f"{q / 1000:,.1f} t" if q < 10_000 else f"{q / 1000:,.0f} t"
    return f"{q:,.0f} {unit}"


def _material_words(o: dict, m: dict, unit: str) -> dict:
    sup = o["supplier"]
    by = date.fromisoformat(o["order_by"]) if o["order_by"] else None
    if o["status"] == "order_now":
        head = f"Order now: {_qty_words(o['order_qty'], unit)}."
    elif o["status"] == "this_week":
        head = f"Order by {by.strftime('%a %d %b').replace(' 0', ' ')}: {_qty_words(o['order_qty'], unit)}."
    elif o["status"] == "overstocked":
        head = "More stock than it needs: hold off ordering."
    else:
        head = (f"Covered. Next order by {by.strftime('%d %B').lstrip('0')}." if by
                else "Covered for the next five months.")
    why = (f"{sup['name']} quotes {sup['quoted']} days; an order placed then most likely takes {sup['median']:.0f}, and "
           f"1 in 10 takes {sup['p90']:.0f} or more.")
    if m["dismm"] == "PD" and o.get("round"):
        rd = date.fromisoformat(o["round"]["date"])
        buffer = (f"On the order-by day the reorder point is {_qty_words(o['reorder_point'], unit)}: the programme due "
                  f"inside the supplier's lead time, with {_qty_words(o['safety_stock'], unit)} bought ahead of the "
                  f"usual time so a late sailing still lands before the {rd.strftime('%B').lstrip('0')} round is issued. "
                  f"For fertiliser the buffer is time, not stock carried all year.")
    else:
        buffer = (f"Reorder at {_qty_words(o['reorder_point'], unit)}, of which {_qty_words(o['safety_stock'], unit)} is "
                  f"safety stock: {_qty_words(o['safety_from_supplier'], unit)} because deliveries vary, "
                  f"{_qty_words(o['safety_from_use'], unit)} because use varies.")
    sap = (f"SAP reorders at {_qty_words(m['minbe'], unit)} with {_qty_words(m['eisbe'], unit)} safety stock on a "
           f"{m['plifz']}-day lead time." if m["dismm"] == "VB" else
           (f"SAP plans the {o['sap'].get('next_round', '')[:7]} round on a {m['plifz']}-day lead time, ordered around "
            f"{date.fromisoformat(o['sap']['order_date']).strftime('%d %b').lstrip('0')}." if o["sap"].get("order_date") else
            f"SAP plans against the programme on a {m['plifz']}-day lead time."))
    risk = (f"If nothing is ordered today, there is a {o['chance_out_before_delivery']:.0%} chance of running out before a "
            "delivery ordered today could arrive.")
    return {"headline": head, "supplier": why, "buffer": buffer, "sap": sap, "risk": risk}


def overview(group: str | None = None, on: date | None = None) -> dict:
    st = mm.state()
    if not st.get("available"):
        return st
    rows = []
    for matnr, m in st["materials"].items():
        if group and m["matkl"] != group and mm.GROUP_KEYS.get(m["matkl"]) != group:
            continue
        v = material(matnr, on)
        rows.append({k: v[k] for k in (
            "matnr", "maktx", "group", "group_label", "unit", "on_hand", "on_order", "value_idr", "use_30d",
            "days_of_cover", "reorder_point", "safety_stock", "order_by", "order_by_days", "order_qty", "status",
            "chance_out_before_delivery", "policy_in_force", "plain", "sap")} | {"supplier": v["supplier"]["name"]})
    order = {"order_now": 0, "this_week": 1, "overstocked": 3, "covered": 2}
    rows.sort(key=lambda r: (order[r["status"]], r["order_by_days"] if r["order_by_days"] is not None else 999))
    return {"available": True, "date": (on or TOMORROW).isoformat(), "materials": rows,
            "counts": {k: sum(1 for r in rows if r["status"] == k) for k in order},
            "stock_value_idr": sum(r["value_idr"] for r in rows)}


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
