"""Cause and action for one leak: the server checks, Nova weighs (buildplan_finance.md §5).

The engine (gis/finance.py) says where money leaks and how much, and names the
driver: "fuel gap". A driver is a symptom. A fuel gap can be a leaking tank,
siphoning on one driver's shifts or runs nobody logged, and each needs a
different fix. Telling them apart means weighing several facts against each
other, which is what the model is for. The split is the one gis/briefings.py
keeps:

    the server computes and checks; Nova weighs the evidence and writes.

So this module does three things, in order:

  1. Runs a fixed set of deterministic checks for the driver, chosen so the
     candidate causes leave different traces (finance_playbook.py lists the
     causes and what each looks like). Each check returns a finding sentence
     and the figures inside it.
  2. Hands Nova the unit, the checks, the causes and the vetted actions, and
     asks it to choose. It may only choose: a cause by id (or within_norm,
     or other), check ids as evidence, actions by id.
  3. Validates the answer: every id must exist, and reasoning.audit_figures
     must find no figure the server did not give it. One retry at
     temperature 0; after that the rule-based Why and Do stand.

The answer key (ec_fi_leak_key.csv) is never read here. The diagnosis eval
(evals/run_leak_diagnosis.py) reads it, to score what this module returns.
"""

import hashlib
import json
import logging
import math
import threading
from collections import defaultdict
from datetime import date, timedelta

from gis import assumptions, finance, reasoning
from gis import finance_playbook as pb
from gis.build_operations import _read
from prompts import load_prompt

log = logging.getLogger("estate-command.diagnosis")

_CACHE = reasoning.TTLCache(ttl_seconds=24 * 3600, maxsize=400)
WITHIN_NORM = {"id": "within_norm", "label": "Nothing unusual",
               "looks_like": "every check reads about the same as the unit's peers"}
TANK_L = {"tractor-trailer": 200, "dump-truck": 250, "mini-truck": 80}


# ── small helpers ──────────────────────────────────────────────────────────

def _f(v, d=0.0):
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _r(v, n=0):
    if v is None:
        return None
    return round(v, n) if n else int(round(v))


def _pct(a, b):
    return _r(100 * a / b) if b else None


def _med(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def _check(cid, label, finding, **figures):
    return {"id": cid, "label": label, "finding": finding,
            "figures": {k: v for k, v in figures.items() if v is not None}}


def _in_month(d: str, mo: str) -> bool:
    return d[:7] == mo


def _km(lon1, lat1, lon2, lat2):
    if None in (lon1, lat1, lon2, lat2):
        return None
    x = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    y = (lat2 - lat1) * 111.32
    return math.hypot(x, y)


_DISPATCH = {}


def _dispatch():
    if "rows" not in _DISPATCH:
        _DISPATCH["rows"] = _read("ec_dispatch_orders.csv")
    return _DISPATCH["rows"]


# ── checks, one function per check id ──────────────────────────────────────
# Each takes ctx = {b, st, uid, mo, meta, row, av} and returns one check dict.

def _vehicle_trips(ctx):
    v = ctx["meta"]["object"]
    return [t for t in ctx["b"]["trips"] if t["vehicle"] == v and t["month"] == ctx["mo"]]


def _card_fills(ctx, vehicle=None, month=None):
    v = vehicle or ctx["meta"]["object"]
    mo = month or ctx["mo"]
    return [f for f in ctx["b"]["fuel_log"] if f["vehicle_id"] == v and f["source"] == "card"
            and _in_month(f["date"], mo)]


def c_fuel_by_driver(ctx):
    v = ctx["meta"]["object"]
    fills = _card_fills(ctx)
    lit = defaultdict(float)
    n = defaultdict(int)
    for f in fills:
        lit[f["driver_id"]] += _f(f["litres"])
        n[f["driver_id"]] += 1
    days = defaultdict(set)
    for t in _vehicle_trips(ctx):
        days[t["driver"]].add(t["date"])
    all_days = {t["date"] for t in _vehicle_trips(ctx)}
    if not fills:
        return _check("fuel_by_driver", "Card fills by driver",
                      f"No fuel card fills on {v} this month.", card_fills=0)
    top = max(lit, key=lit.get)
    tot = sum(lit.values())
    return _check("fuel_by_driver", "Card fills by driver",
                  f"{len(lit)} drivers made card fills on {v}. The most by one driver: {top}, {n[top]} of the "
                  f"{len(fills)} fills and {_pct(lit[top], tot)}% of the {_r(tot)} card litres; {top} drove {v} on "
                  f"{len(days[top])} of its {len(all_days)} working days.",
                  top_share_pct=_pct(lit[top], tot),
                  top_driver=top, top_fills=n[top], fills=len(fills), top_litres=_r(lit[top]),
                  card_litres=_r(tot), top_driver_days=len(days[top]), vehicle_days=len(all_days),
                  drivers_with_fills=len(lit))


def c_fuel_trip_days(ctx):
    v = ctx["meta"]["object"]
    mo = ctx["mo"]
    burn = defaultdict(float)
    for t in _vehicle_trips(ctx):
        burn[t["date"]] += t["diesel"]
    filled = defaultdict(float)
    for f in ctx["b"]["fuel_log"]:
        if f["vehicle_id"] == v and _in_month(f["date"], mo):
            filled[f["date"]] += _f(f["litres"])
    no_trip = [d for d in filled if d not in burn]
    over_days = sum(1 for d, l in burn.items() if filled.get(d, 0.0) > 1.1 * l)
    tb = sum(burn.values())
    tf = sum(filled.values())
    return _check("fuel_trip_days", "Fills against trip burn, day by day",
                  f"All fills {_r(tf)} L against {_r(tb)} L burned on trips ({_pct(tf, tb)}%); fills ran more than "
                  f"10% over the day's burn on {over_days} of {len(burn)} trip days; {len(no_trip)} fills on days "
                  f"without trips.",
                  filled_litres=_r(tf), burned_litres=_r(tb), filled_vs_burn_pct=_pct(tf, tb),
                  days_over=over_days, trip_days=len(burn), fills_on_no_trip_days=len(no_trip))


def c_fuel_fill_size(ctx):
    v = ctx["meta"]["object"]
    cls = ctx["b"]["vehicles"][v]["vehicle_class"]
    tank = TANK_L.get(cls, 150)
    fills = _card_fills(ctx)
    burn = defaultdict(float)
    for t in _vehicle_trips(ctx):
        burn[t["date"]] += t["diesel"]
    if not fills:
        return _check("fuel_fill_size", "Card fill size", "No card fills to size.", card_fills=0)
    sizes = [_f(f["litres"]) for f in fills]
    big = [f for f in fills if _f(f["litres"]) >= 0.6 * tank]
    day_burn = _med([burn.get(f["date"], 0.0) for f in big]) if big else None
    return _check("fuel_fill_size", "Card fill size",
                  f"{len(big)} of {len(fills)} card fills were over 60% of the {tank} L tank, averaging "
                  f"{_r(sum(_f(f['litres']) for f in big) / len(big)) if big else 0} L"
                  + (f", on days the truck burned a median {_r(day_burn)} L on its trips." if big else "."),
                  tank_l=tank, large_fills=len(big), fills=len(fills),
                  large_fill_avg_l=_r(sum(_f(f['litres']) for f in big) / len(big)) if big else 0,
                  median_burn_on_those_days_l=_r(day_burn) if day_burn is not None else None,
                  median_fill_l=_r(_med(sizes)))


def c_fuel_after_repair(ctx):
    v = ctx["meta"]["object"]
    jobs = [(p["raised_date"], p["fault"]) for p in ctx["b"]["pm_orders"].values() if p["vehicle_id"] == v
            and finance._in_window(p["raised_date"])]
    jobs += [(j["date"], j["fault"] or j["kind"]) for j in ctx["b"]["services"].values() if j["vehicle_id"] == v
             and finance._in_window(j["date"])]
    gap = {}
    for m in finance.MONTHS:
        r = ctx["st"]["rows"].get((ctx["uid"], m))
        if r:
            gap[m] = r["facts"].get("gap_pct")
    jobs.sort()
    before = [j for j in jobs if j[0][:7] < ctx["mo"]]
    last = before[-1] if before else None
    prev_gaps = [g for m, g in gap.items() if m < ctx["mo"] and g is not None]
    return _check("fuel_after_repair", "Fuel gap around repairs",
                  (f"Last workshop job before this month: {last[1]} on {last[0]}. " if last else
                   "No workshop job before this month in the window. ")
                  + f"Fuel gap {_r(gap.get(ctx['mo']) or 0)}% this month"
                  + (f" against {_r(_med(prev_gaps))}% in earlier months." if prev_gaps else "."),
                  last_job=last[0] if last else None, gap_pct=_r(gap.get(ctx["mo"]) or 0),
                  earlier_gap_pct=_r(_med(prev_gaps)) if prev_gaps else None)


def c_fuel_burn_vs_spec(ctx):
    f = ctx["row"]["facts"]
    return _check("fuel_burn_vs_spec", "Trip burn against spec",
                  f"Its trips burned {_r(f.get('over_pct', 0))}% more diesel per km than its spec.",
                  over_spec_pct=_r(f.get("over_pct", 0)))


def c_fuel_class_peers(ctx):
    v = ctx["meta"]["object"]
    cls = ctx["b"]["vehicles"][v]["vehicle_class"]
    gaps = []
    for u, m in ctx["st"]["meta"].items():
        if m["function"] == "transport" and m["unit_kind"] == "vehicle" and m.get("vehicle_class") == cls and u != ctx["uid"]:
            r = ctx["st"]["rows"].get((u, ctx["mo"]))
            if r:
                gaps.append(r["facts"].get("gap_pct", 0.0))
    mine = ctx["row"]["facts"].get("gap_pct", 0.0)
    return _check("fuel_class_peers", "Other vehicles of its class",
                  f"Other {cls.replace('-', ' ')}s ran a median fuel gap of {_r(_med(gaps) or 0)}% this month; "
                  f"{v} ran {_r(mine)}%.",
                  class_median_gap_pct=_r(_med(gaps) or 0), gap_pct=_r(mine), class_vehicles=len(gaps))


def c_burn_by_route(ctx):
    by = defaultdict(lambda: [0.0, 0.0])
    for t in _vehicle_trips(ctx):
        by[t["route"]][0] += t["diesel"]
        by[t["route"]][1] += 2 * t["km"] * t["spec"] / 100
    rows = sorted(((r, _pct(a - s, s)) for r, (a, s) in by.items() if s), key=lambda x: -(x[1] or 0))
    if not rows:
        return _check("burn_by_route", "Burn by route", "No trips.")
    return _check("burn_by_route", "Burn by route",
                  f"Highest burn over spec on route {rows[0][0]} ({rows[0][1]}%), lowest on {rows[-1][0]} ({rows[-1][1]}%).",
                  worst_route=rows[0][0], worst_pct=rows[0][1], best_route=rows[-1][0], best_pct=rows[-1][1])


def c_burn_by_driver(ctx):
    by = defaultdict(lambda: [0.0, 0.0])
    for t in _vehicle_trips(ctx):
        by[t["driver"]][0] += t["diesel"]
        by[t["driver"]][1] += 2 * t["km"] * t["spec"] / 100
    rows = sorted(((d, _pct(a - s, s)) for d, (a, s) in by.items() if s), key=lambda x: -(x[1] or 0))
    if not rows:
        return _check("burn_by_driver", "Burn by driver", "No trips.")
    return _check("burn_by_driver", "Burn by driver",
                  f"{len(rows)} drivers; burn over spec ranges from {rows[-1][1]}% ({rows[-1][0]}) to {rows[0][1]}% ({rows[0][0]}).",
                  drivers=len(rows), worst_driver=rows[0][0], worst_pct=rows[0][1])


def c_load_by_hour(ctx):
    thr = ctx["av"]["part_load_threshold_pct"] / 100
    am = [t for t in _vehicle_trips(ctx) if t["depart_h"] < 12]
    pm_ = [t for t in _vehicle_trips(ctx) if t["depart_h"] >= 12]

    def share(ts):
        return _pct(sum(1 for t in ts if t["net_kg"] / 1000 / t["cap_t"] < thr), len(ts)) if ts else None
    return _check("load_by_hour", "Part loads by departure",
                  f"Part loads on {share(am)}% of morning trips and {share(pm_)}% of afternoon trips.",
                  morning_part_pct=share(am), afternoon_part_pct=share(pm_), morning_trips=len(am),
                  afternoon_trips=len(pm_))


def c_load_by_route(ctx):
    thr = ctx["av"]["part_load_threshold_pct"] / 100
    by = defaultdict(list)
    for t in _vehicle_trips(ctx):
        by[t["route"]].append(t["net_kg"] / 1000 / t["cap_t"] < thr)
    rows = sorted(((r, _pct(sum(v), len(v))) for r, v in by.items()), key=lambda x: -x[1])
    if not rows:
        return _check("load_by_route", "Part loads by route", "No trips.")
    return _check("load_by_route", "Part loads by route",
                  f"Part loads on {rows[0][1]}% of trips on route {rows[0][0]}, {rows[-1][1]}% on {rows[-1][0]}.",
                  worst_route=rows[0][0], worst_pct=rows[0][1], best_route=rows[-1][0], best_pct=rows[-1][1])


def c_queue_by_hour(ctx):
    ts = _vehicle_trips(ctx)
    am = [t["queue_h"] for t in ts if t["depart_h"] < 12]
    pm_ = [t["queue_h"] for t in ts if t["depart_h"] >= 12]
    a = _r(sum(am) / len(am), 2) if am else None
    p = _r(sum(pm_) / len(pm_), 2) if pm_ else None
    return _check("queue_by_hour", "Mill queue by departure",
                  f"Mean mill queue {a} h on morning trips and {p} h on afternoon trips.",
                  morning_queue_h=a, afternoon_queue_h=p)


def c_queue_vs_class(ctx):
    v = ctx["meta"]["object"]
    cls = ctx["b"]["vehicles"][v]["vehicle_class"]
    qs = defaultdict(list)
    for t in ctx["b"]["trips"]:
        if t["month"] == ctx["mo"] and t["cls"] == cls:
            qs[t["vehicle"]].append(t["queue_h"])
    mean = {k: sum(x) / len(x) for k, x in qs.items() if x}
    return _check("queue_vs_class", "Queue against its class",
                  f"{v} queued {_r(mean.get(v, 0), 2)} h a trip; its class median is {_r(_med(mean.values()) or 0, 2)} h.",
                  queue_h=_r(mean.get(v, 0), 2), class_median_h=_r(_med(mean.values()) or 0, 2))


def _driver_trips(ctx, driver=None):
    d = driver or ctx["meta"]["object"]
    return [t for t in ctx["b"]["trips"] if t["driver"] == d]


def _shrink(ts):
    exp = sum(t["bunches"] * t["abw"] for t in ts)
    return _pct(exp - sum(t["net_kg"] for t in ts), exp) if exp else None


def c_shrink_concentration(ctx):
    drv = ctx["meta"]["object"]
    ts = [t for t in _driver_trips(ctx) if t["month"] == ctx["mo"]]
    light = [t for t in ts if t["bunches"] * t["abw"] and (t["bunches"] * t["abw"] - t["net_kg"]) / (t["bunches"] * t["abw"]) > 0.08]
    return _check("shrink_concentration", "Where the light loads fall",
                  f"{len(light)} of {drv}'s {len(ts)} trips this month weighed more than 8% light, across "
                  f"{len({t['vehicle'] for t in light})} vehicles, {len({t['route'] for t in light})} routes "
                  f"and {len({t['key'] for t in light})} blocks.",
                  light_trips=len(light), trips=len(ts), vehicles=len({t['vehicle'] for t in light}),
                  routes=len({t['route'] for t in light}), blocks=len({t['key'] for t in light}))


def c_shrink_when(ctx):
    drv = ctx["meta"]["object"]
    ts = [t for t in _driver_trips(ctx) if t["month"] == ctx["mo"]]
    light = sorted(t["date"] for t in ts if t["bunches"] * t["abw"] and
                   (t["bunches"] * t["abw"] - t["net_kg"]) / (t["bunches"] * t["abw"]) > 0.08)
    other = [m for m in finance.MONTHS if m != ctx["mo"]]
    other_s = _shrink([t for t in _driver_trips(ctx) if t["month"] in other])
    return _check("shrink_when", "When",
                  (f"Light trips run from {light[0]} to {light[-1]}. " if light else "No light trips. ")
                  + f"{drv}'s shrinkage in the other months: {other_s}%.",
                  first=light[0] if light else None, last=light[-1] if light else None,
                  other_months_pct=other_s)


def c_shrink_same_vehicle(ctx):
    drv = ctx["meta"]["object"]
    mine = [t for t in _driver_trips(ctx) if t["month"] == ctx["mo"]]
    vs = {t["vehicle"] for t in mine}
    others = [t for t in ctx["b"]["trips"] if t["month"] == ctx["mo"] and t["vehicle"] in vs and t["driver"] != drv]
    return _check("shrink_same_vehicle", "Same vehicles, other drivers",
                  f"On the same vehicles this month, other drivers' loads were {_shrink(others)}% light; {drv}'s were {_shrink(mine)}%.",
                  others_pct=_shrink(others), driver_pct=_shrink(mine))


def c_shrink_blocks(ctx):
    drv = ctx["meta"]["object"]
    mine = [t for t in _driver_trips(ctx) if t["month"] == ctx["mo"]]
    ks = {t["key"] for t in mine}
    others = [t for t in ctx["b"]["trips"] if t["month"] == ctx["mo"] and t["key"] in ks and t["driver"] != drv]
    return _check("shrink_blocks", "Same blocks, other drivers",
                  f"Fruit from the same blocks weighed {_shrink(others)}% light with other drivers.",
                  others_pct=_shrink(others))


def _crew_days(ctx, crew=None):
    c = crew or ctx["meta"]["object"]
    out = {}
    for a in ctx["b"]["attendance"]:
        if a["crew_code"] == c and _in_month(a["date"], ctx["mo"]):
            out[a["date"]] = a
    return out


def _ot_days(ctx, crew=None, window=False):
    c = crew or ctx["meta"]["object"]
    out = defaultdict(float)
    for p in ctx["b"]["postings"]:
        if p["racct"] == "6101200" and p["crew_code"] == c and (window or p["month"] == ctx["mo"]):
            out[p["budat"]] += p["quantity"]
    return out


def _crew_orders(ctx, crew=None, window=False):
    c = crew or ctx["meta"]["object"]
    out = defaultdict(list)
    for o in ctx["b"]["orders"]:
        if o["crew_code"] == c and (window or _in_month(o["date"], ctx["mo"])) and o["md"] > 0:
            out[o["date"]].append(o)
    return out


def _heavy_share(ctx, crew):
    """Of a gang's overtime days in the window, the share with output per cutter above its median."""
    ot = _ot_days(ctx, crew, window=True)
    orders = _crew_orders(ctx, crew, window=True)
    per = {d: sum(o["qty"] for o in os_) / sum(o["md"] for o in os_) for d, os_ in orders.items()}
    med = _med(per.values())
    days = [d for d in ot if d in per]
    if not days or med is None:
        return None, 0, 0
    heavy = sum(1 for d in days if per[d] > med)
    return 100 * heavy / len(days), heavy, len(days)


def c_ot_vs_output(ctx):
    crew = ctx["meta"]["object"]
    pct, heavy, n = _heavy_share(ctx, crew)
    others = []
    for c, cr in ctx["b"]["crews"].items():
        if cr["crew_type"] == "harvest" and c != crew:
            p, _, k = _heavy_share(ctx, c)
            if p is not None and k >= 3:
                others.append(p)
    return _check("ot_vs_output", "Overtime against crop",
                  f"Over the window, {heavy} of {crew}'s {n} overtime days were heavy-crop days (output per cutter above "
                  f"its median), {_r(pct or 0)}%; for the other gangs a median {_r(_med(others) or 0)}% of overtime days were.",
                  heavy_ot_days=heavy, ot_days=n, heavy_pct=_r(pct or 0), others_heavy_pct=_r(_med(others) or 0))


def c_ot_dry_wet(ctx):
    ot = _ot_days(ctx, window=True)
    days = _crew_orders(ctx, window=True)
    rain = ctx["b"]["rain"]
    wet_ot = sum(1 for d in ot if rain.get(d, 0.0) >= 10)
    wet_all = sum(1 for d in days if rain.get(d, 0.0) >= 10)
    return _check("ot_dry_wet", "Overtime and rain",
                  f"Over the window, {wet_ot} of {len(ot)} overtime days had 10 mm of rain or more, against {wet_all} of "
                  f"{len(days)} working days ({_pct(wet_ot, len(ot))}% and {_pct(wet_all, len(days))}%).",
                  wet_ot_days=wet_ot, ot_days=len(ot), wet_days=wet_all, work_days=len(days),
                  wet_ot_pct=_pct(wet_ot, len(ot)), wet_work_pct=_pct(wet_all, len(days)))


def c_ot_headcount(ctx):
    ot = _ot_days(ctx, window=True)
    work = _crew_orders(ctx, window=True)
    crew = ctx["meta"]["object"]
    att = {a["date"]: a for a in ctx["b"]["attendance"] if a["crew_code"] == crew}
    est = _f(ctx["b"]["crews"][crew]["establishment"])
    on = [int(_f(att[d]["present"])) for d in ot if d in att]
    off = [int(_f(att[d]["present"])) for d in work if d not in ot and d in att]
    return _check("ot_headcount", "Overtime and headcount",
                  f"Over the window, median present {_r(_med(on) or 0)} on overtime days and {_r(_med(off) or 0)} on other "
                  f"working days, of an establishment of {_r(est)}.",
                  present_ot=_r(_med(on) or 0), present_other=_r(_med(off) or 0), establishment=_r(est))


def c_ot_start(ctx):
    ot = _ot_days(ctx, window=True)
    orders = _crew_orders(ctx, window=True)

    def start(os_):
        hs = []
        for o in os_:
            try:
                h, m = o["started"].split(":")
                hs.append(int(h) + int(m) / 60)
            except (ValueError, AttributeError):
                pass
        return min(hs) if hs else None
    on = [start(orders[d]) for d in ot if d in orders]
    off = [start(os_) for d, os_ in orders.items() if d not in ot]

    def hhmm(x):
        return f"{int(x):02d}:{int(round((x % 1) * 60)):02d}" if x is not None else None
    return _check("ot_start", "Start times",
                  f"Median first start {hhmm(_med(on))} on overtime days and {hhmm(_med(off))} on others.",
                  start_ot=hhmm(_med(on)), start_other=hhmm(_med(off)))


def c_ot_peers(ctx):
    crew = ctx["meta"]["object"]
    ctype = ctx["b"]["crews"][crew]["crew_type"]
    shares = []
    for c, cr in ctx["b"]["crews"].items():
        if cr["crew_type"] != ctype or c == crew:
            continue
        ot = _ot_days(ctx, c)
        days = _crew_orders(ctx, c)
        if days:
            shares.append(100 * len(ot) / len(days))
    ot, days = _ot_days(ctx), _crew_orders(ctx)
    return _check("ot_peers", "Other gangs",
                  f"This month other gangs worked overtime on a median {_r(_med(shares) or 0)}% of their working days; {crew} on {_pct(len(ot), len(days))}%.",
                  peers_median_pct=_r(_med(shares) or 0), crew_pct=_pct(len(ot), len(days)))


def _pad_daily(ctx):
    crew = ctx["meta"]["object"]
    att = _crew_days(ctx)
    orders = _crew_orders(ctx)
    un = defaultdict(float)
    for p in ctx["b"]["postings"]:
        if p["racct"] == "6105000" and p["crew_code"] == crew and p["month"] == ctx["mo"]:
            un[p["budat"]] += p["quantity"]
    gaps = {}
    for d, a in att.items():
        present = int(_f(a["present"]))
        md = sum(o["md"] for o in orders.get(d, []))
        gaps[d] = (un.get(d, 0.0) - max(0.0, present - md), a)
    return gaps


def c_pad_constant(ctx):
    g = _pad_daily(ctx)
    vals = [round(x) for x, _ in g.values()]
    days = sum(1 for v in vals if v > 0)
    common = max(set(vals), key=vals.count) if vals else 0
    lo, hi = (min(vals), max(vals)) if vals else (0, 0)
    return _check("pad_constant", "Paid above muster, day by day",
                  f"Payroll paid above the muster on {days} of {len(vals)} muster days. The gap ranged from {lo} to "
                  f"{hi} a day, {len(set(vals))} different sizes; the most common was {common}, on "
                  f"{vals.count(common)} days.",
                  days_with_gap=days, muster_days=len(vals), gap_min=lo, gap_max=hi, gap_sizes=len(set(vals)),
                  common_gap=common, days_at_common_gap=vals.count(common))


def c_pad_absence(ctx):
    g = _pad_daily(ctx)
    bands = [("8 or more absent", lambda n: n >= 8), ("4 to 7 absent", lambda n: 4 <= n <= 7),
             ("3 or fewer absent", lambda n: n <= 3)]
    parts, figs = [], {}
    for label, test in bands:
        xs = [x for x, a in g.values() if test(int(_f(a["absent"])))]
        if xs:
            parts.append(f"{_r(_med(xs), 1)} on days with {label} ({len(xs)} days)")
            figs[label.split()[0] + "_band_gap"] = _r(_med(xs), 1)
    return _check("pad_absence", "Gap against absence",
                  "Median gap " + "; ".join(parts) + ".", **figs)


def c_pad_workload(ctx):
    g = _pad_daily(ctx)
    orders = _crew_orders(ctx)
    loads = {d: sum(o["md"] for o in orders.get(d, [])) for d in g}
    med = _med(loads.values()) or 0
    hv = [g[d][0] for d in g if loads[d] > med]
    lt = [g[d][0] for d in g if loads[d] <= med]
    return _check("pad_workload", "Gap against workload",
                  f"Median gap {_r(_med(hv) or 0, 1)} on heavy work days and {_r(_med(lt) or 0, 1)} on light ones.",
                  gap_heavy=_r(_med(hv) or 0, 1), gap_light=_r(_med(lt) or 0, 1))


def c_idle_dry_days(ctx):
    f = ctx["row"]["facts"]
    return _check("idle_dry_days", "Idle on ordinary days",
                  f"{_r(f.get('idle_md', 0))} man-days present with no work order on dry working days, "
                  f"{_r(f.get('idle_share', 0))}% of the {_r(f.get('present_md', 0))} present; the benchmark allows {_r(f.get('bench_share', 0))}%.",
                  idle_md=_r(f.get("idle_md", 0)), idle_share=_r(f.get("idle_share", 0)),
                  bench_share=_r(f.get("bench_share", 0)))


def c_idle_vs_roles(ctx):
    c = ctx["b"]["crews"][ctx["meta"]["object"]]
    est = _f(c["establishment"])
    roles = (est - _f(c["harvesters"])) if c["crew_type"] == "harvest" and c["harvesters"] else _f(c["mandor"]) + _f(c["kerani"])
    return _check("idle_vs_roles", "Support roles",
                  f"{_r(roles)} of the crew's {_r(est)} are support roles ({_pct(roles, est)}%).",
                  roles=_r(roles), establishment=_r(est), roles_pct=_pct(roles, est))


def _keys(ctx):
    """The block keys a check reads: the block itself, or every member of a division group."""
    if ctx["meta"].get("unit_kind") == "division":
        st = ctx["st"]
        return [st["meta"][u]["object"] for u in ctx["meta"].get("members", []) if u in st["meta"]]
    return [ctx["meta"]["object"]]


def _who(ctx):
    return "the division's blocks" if ctx["meta"].get("unit_kind") == "division" else "the block"


def _gangs(ctx):
    return sorted({(ctx["b"]["rot"].get(k) or {}).get("gang_code") for k in _keys(ctx)} - {None})


def _harvest_rate(ctx, keys):
    """Bunches a hectare per harvest day, median over the keys."""
    out = []
    for k in keys:
        bl = ctx["b"]["blocks"].get(k)
        if not bl or not bl["ha"]:
            continue
        os_ = [o for o in ctx["b"]["orders"] if o["key"] == k and o["operation"] == "harvest"
               and _in_month(o["date"], ctx["mo"]) and o["qty"] > 0]
        days = {o["date"] for o in os_}
        if days:
            out.append(sum(o["qty"] for o in os_) / len(days) / bl["ha"])
    return out


def c_bunches_per_day(ctx):
    keys = _keys(ctx)
    mine = _med(_harvest_rate(ctx, keys))
    dens = {ctx["b"]["blocks"][k]["peer"]["density"] for k in keys if k in ctx["b"]["blocks"]}
    peers = [k for k, bl in ctx["b"]["blocks"].items() if bl["peer"]["density"] in dens and k not in keys]
    theirs = _med(_harvest_rate(ctx, peers))
    return _check("bunches_per_day", "Crop per harvest day",
                  f"{_who(ctx).capitalize()} cut a median {_r(mine or 0, 1)} bunches a hectare per harvest day; "
                  f"other blocks of similar density cut {_r(theirs or 0, 1)}.",
                  per_ha=_r(mine or 0, 1), peers_per_ha=_r(theirs or 0, 1))


def c_home_distance(ctx):
    dists, others = [], []
    for k in _keys(ctx):
        gang = (ctx["b"]["rot"].get(k) or {}).get("gang_code")
        c = ctx["b"]["crews"].get(gang or "")
        bl = ctx["b"]["blocks"].get(k)
        if c and bl:
            dists.append(_km(_f(c["home_lon"]), _f(c["home_lat"]), bl.get("lon"), bl.get("lat")))
    keys = set(_keys(ctx))
    for k, r in ctx["b"]["rot"].items():
        c = ctx["b"]["crews"].get(r.get("gang_code") or "")
        bl = ctx["b"]["blocks"].get(k)
        if c and bl and k not in keys:
            others.append(_km(_f(c["home_lon"]), _f(c["home_lat"]), bl.get("lon"), bl.get("lat")))
    return _check("home_distance", "Distance from the gang's base",
                  f"{_who(ctx).capitalize()} sit a median {_r(_med(dists) or 0, 1)} km from their gang's base; "
                  f"the estate's other blocks {_r(_med(others) or 0, 1)} km.",
                  km=_r(_med(dists) or 0, 1), others_km=_r(_med(others) or 0, 1))


def c_path_overdue(ctx):
    end = min(date.fromisoformat(f"{ctx['mo']}-28"), finance.WINDOW_END)
    late, n, since_all = 0, 0, []
    state = {(finance._k(r["division_code"], r["block_code"])): r for r in ctx["b"]["upkeep_state"]
             if r["activity"] == "path_upkeep"}
    for k in _keys(ctx):
        s = state.get(k)
        done = sorted(o["date"] for o in ctx["b"]["orders"] if o["key"] == k and o["activity"] == "path_upkeep"
                      and o["status"] == "completed" and o["date"][:7] <= ctx["mo"])
        last = done[-1] if done else (s or {}).get("last_done")
        if not last:
            continue
        interval = int(_f((s or {}).get("interval_days"), 110))
        since = (end - date.fromisoformat(last)).days
        since_all.append(since)
        n += 1
        late += 1 if since > interval else 0
    return _check("path_overdue", "Harvest paths",
                  f"Path upkeep overdue on {late} of {n} blocks at the month's end; a median {_r(_med(since_all) or 0)} days since it was last done.",
                  overdue_blocks=late, blocks=n, median_days_since=_r(_med(since_all) or 0))


def c_rain_days(ctx):
    rain = ctx["b"]["rain"]
    keys = set(_keys(ctx))
    ops = ("harvest",) if ctx["meta"]["function"] == "harvesting" else ("prune", "weed", "spray")
    days = {o["date"] for o in ctx["b"]["orders"] if o["key"] in keys and o["operation"] in ops
            and _in_month(o["date"], ctx["mo"])}
    wet = sum(1 for d in days if rain.get(d, 0.0) >= 10)
    est_days = [d for d in rain if _in_month(d, ctx["mo"])]
    est_wet = sum(1 for d in est_days if rain.get(d, 0.0) >= 10)
    return _check("rain_days", "Rain on its working days",
                  f"{wet} of {len(days)} working days had 10 mm or more; {est_wet} of the month's {len(est_days)} days did.",
                  wet_days=wet, work_days=len(days), month_wet_days=est_wet, month_days=len(est_days))


def c_crew_speed(ctx):
    gangs = _gangs(ctx)
    per = defaultdict(lambda: [0.0, 0.0])
    for o in ctx["b"]["orders"]:
        if o["operation"] == "harvest" and _in_month(o["date"], ctx["mo"]) and o["md"]:
            per[o["crew_code"]][0] += o["qty"]
            per[o["crew_code"]][1] += o["md"]
    rates = {c: q / m for c, (q, m) in per.items() if m}
    mine = _med([rates[g] for g in gangs if g in rates])
    return _check("crew_speed", "The gangs' pace",
                  f"{', '.join(gangs[:4])}{' and others' if len(gangs) > 4 else ''} cut a median {_r(mine or 0)} bunches a man-day across all their blocks; "
                  f"the median gang cut {_r(_med(rates.values()) or 0)}.",
                  gangs=", ".join(gangs), gang_rate=_r(mine or 0), median_rate=_r(_med(rates.values()) or 0))


def c_crew_days(ctx):
    gangs = _gangs(ctx)
    shares = []
    for g in gangs:
        att = _crew_days(ctx, g)
        est = _f((ctx["b"]["crews"].get(g) or {}).get("establishment"))
        pres = [int(_f(a["present"])) for a in att.values() if int(_f(a["present"])) > 0]
        if est and pres:
            shares.append(100 * _med(pres) / est)
    late = blocks = 0
    for k, r in ctx["b"]["rot"].items():
        if r.get("gang_code") in gangs:
            blocks += 1
            x = ctx["st"]["rows"].get((f"harvesting:block:{k}", ctx["mo"]))
            if x and x["drivers"].get("late_rounds", 0) > 0:
                late += 1
    others = []
    for c, cr in ctx["b"]["crews"].items():
        if cr["crew_type"] == "harvest" and c not in gangs:
            att = _crew_days(ctx, c)
            est = _f(cr["establishment"])
            pres = [int(_f(a["present"])) for a in att.values() if int(_f(a["present"])) > 0]
            if est and pres:
                others.append(100 * _med(pres) / est)
    return _check("crew_days", "The gangs' capacity",
                  f"{', '.join(gangs[:4])}{' and others' if len(gangs) > 4 else ''} mustered a median {_r(_med(shares) or 0)}% of establishment "
                  f"(other gangs {_r(_med(others) or 0)}%); {late} of their {blocks} blocks were cut late this month.",
                  gangs=", ".join(gangs), present_pct=_r(_med(shares) or 0), others_present_pct=_r(_med(others) or 0),
                  late_blocks=late, blocks=blocks)


def c_rain_outs(ctx):
    rain = ctx["b"]["rain"]
    days = [d for d in rain if _in_month(d, ctx["mo"])]
    heavy = sum(1 for d in days if rain[d] >= ctx["av"]["rain_cutoff_mm"])
    wet = sum(1 for d in days if rain[d] >= 25)
    return _check("rain_outs", "Rain-outs",
                  f"{heavy} days this month with rain over {_r(ctx['av']['rain_cutoff_mm'])} mm, when field work stops, and {wet} over 25 mm.",
                  rain_out_days=heavy, heavy_rain_days=wet, cutoff_mm=_r(ctx["av"]["rain_cutoff_mm"]))


def c_dispatch_adherence(ctx):
    keys = set(_keys(ctx))
    rows = [o for o in _dispatch() if finance._k(o["division_code"], o["block_code"]) in keys and _in_month(o["date"], ctx["mo"])]
    pl = sum(_f(o["planned_qty"]) for o in rows)
    ac = sum(_f(o["actual_qty"]) for o in rows)
    est = [o for o in _dispatch() if _in_month(o["date"], ctx["mo"])]
    epl = sum(_f(o["planned_qty"]) for o in est)
    eac = sum(_f(o["actual_qty"]) for o in est)
    return _check("dispatch_adherence", "Dispatch",
                  f"Dispatch moved {_pct(ac, pl)}% of the planned tonnes off {_who(ctx)}' platforms; {_pct(eac, epl)}% estate-wide.",
                  dispatch_pct=_pct(ac, pl), estate_pct=_pct(eac, epl), dispatch_orders=len(rows))


def c_lf_recording(ctx):
    recs = sum(ctx["b"]["blocks"][k]["records"] for k in _keys(ctx) if k in ctx["b"]["blocks"])
    lrec = sum(ctx["b"]["blocks"][k]["loose_records"] for k in _keys(ctx) if k in ctx["b"]["blocks"])
    allr = sum(b["records"] for b in ctx["b"]["blocks"].values())
    alll = sum(b["loose_records"] for b in ctx["b"]["blocks"].values())
    return _check("lf_recording", "Loose fruit recorded",
                  f"{_pct(lrec, recs)}% of {_who(ctx)}' harvest records carry a loose fruit count; {_pct(alll, allr)}% estate-wide.",
                  recorded_pct=_pct(lrec, recs), estate_pct=_pct(alll, allr))


def c_lf_collectors(ctx):
    gangs = _gangs(ctx)
    cut = oth = 0.0
    for g in gangs:
        c = ctx["b"]["crews"].get(g) or {}
        cut += _f(c.get("harvesters"))
        oth += _f(c.get("establishment")) - _f(c.get("harvesters"))
    return _check("lf_collectors", "Hands behind the cutters",
                  f"{', '.join(gangs[:4])}: {_r(cut)} cutters and {_r(oth)} other hands, collectors included.",
                  gangs=", ".join(gangs), cutters=_r(cut), others=_r(oth))


def c_lf_interval(ctx):
    overs = []
    for k in _keys(ctx):
        x = ctx["st"]["rows"].get((f"harvesting:block:{k}", ctx["mo"]))
        if x and x["facts"].get("cuts"):
            overs.append(x["facts"].get("days_over", 0.0))
    return _check("lf_interval", "Round",
                  f"Cut a median {_r(_med(overs) or 0, 1)} days past round this month.",
                  days_over=_r(_med(overs) or 0, 1))


def _vehicle_jobs(ctx):
    v = ctx["meta"]["object"]
    jobs = [(p["raised_date"], p["fault"], "breakdown order", "V402", _f(p["cost_idr"]))
            for p in ctx["b"]["pm_orders"].values() if p["vehicle_id"] == v and finance._in_window(p["raised_date"])]
    jobs += [(j["date"], j["fault"] or "service", j["kind"], j["lifnr"], _f(j["cost_idr"]))
             for j in ctx["b"]["services"].values() if j["vehicle_id"] == v and finance._in_window(j["date"])]
    return sorted(jobs)


def c_repeat_faults(ctx):
    jobs = [j for j in _vehicle_jobs(ctx) if j[2] != "service"]
    by = defaultdict(list)
    for d, fault, *_ in jobs:
        by[fault].append(date.fromisoformat(d))
    repeats = {f: ds for f, ds in by.items() if any((b_ - a).days <= 60 for a, b_ in zip(ds, ds[1:]))}
    top = max(repeats, key=lambda f: len(repeats[f])) if repeats else None
    return _check("repeat_faults", "Repeat faults",
                  (f"{top} repaired {len(repeats[top])} times in the window, each within 60 days of the last; "
                   if top else "No fault repeated within 60 days; ")
                  + f"{len(jobs)} repair jobs on {len(by)} different faults in all.",
                  repeated_fault=top, repeats=len(repeats[top]) if top else 0, repair_jobs=len(jobs), faults=len(by))


def c_fleet_age(ctx):
    v = ctx["meta"]["object"]
    vr = ctx["b"]["vehicles"][v]
    cls = vr["vehicle_class"]
    years = sorted(int(_f(r["year_acquired"])) for r in ctx["b"]["vehicles"].values() if r["vehicle_class"] == cls)
    return _check("fleet_age", "Age",
                  f"Acquired {vr['year_acquired']}; its class runs from {years[0]} to {years[-1]}, odometer {_r(_f(vr['odometer_km']))} km.",
                  year=int(_f(vr["year_acquired"])), class_oldest=years[0], class_newest=years[-1],
                  odometer_km=_r(_f(vr["odometer_km"])))


def c_fleet_load(ctx):
    v = ctx["meta"]["object"]
    ts = [t for t in ctx["b"]["trips"] if t["vehicle"] == v]
    lf = [t["net_kg"] / 1000 / t["cap_t"] for t in ts]
    over = sum(1 for x in lf if x > 1.0)
    return _check("fleet_load", "Loads",
                  f"Median load {_pct(_med(lf) or 0, 1)}% of payload over the window; {over} of {len(lf)} trips over payload.",
                  median_load_pct=_pct(_med(lf) or 0, 1), overloaded_trips=over, trips=len(lf))


def c_fleet_roads(ctx):
    v = ctx["meta"]["object"]
    score = {finance._k(r["division_code"], r["block_code"]): _f(r["condition_score"]) for r in ctx["b"]["roads"].values()}
    mine = [score.get(t["key"]) for t in ctx["b"]["trips"] if t["vehicle"] == v]
    allv = [score.get(t["key"]) for t in ctx["b"]["trips"]]
    return _check("fleet_roads", "Roads it runs on",
                  f"Mean road condition score on its trips {_r(sum(x for x in mine if x) / max(1, len([x for x in mine if x])), 2)}; fleet mean {_r(sum(x for x in allv if x) / max(1, len([x for x in allv if x])), 2)}.",
                  its_score=_r(sum(x for x in mine if x) / max(1, len([x for x in mine if x])), 2),
                  fleet_score=_r(sum(x for x in allv if x) / max(1, len([x for x in allv if x])), 2))


def c_fleet_vendor(ctx):
    jobs = [j for j in _vehicle_jobs(ctx) if j[2] != "service"]
    by = defaultdict(int)
    for j in jobs:
        by[j[3]] += 1
    top = max(by, key=by.get) if by else None
    return _check("fleet_vendor", "Who repairs it",
                  f"{by.get(top, 0)} of {len(jobs)} repair jobs went to workshop {top}." if top else "No repair jobs.",
                  vendor=top, vendor_jobs=by.get(top, 0), repair_jobs=len(jobs))


def c_service_interval(ctx):
    ds = [date.fromisoformat(j[0]) for j in _vehicle_jobs(ctx) if j[2] == "service"]
    gaps = [(b_ - a).days for a, b_ in zip(ds, ds[1:])]
    return _check("service_interval", "Service interval",
                  f"{len(ds)} services in the window, a median {_r(_med(gaps) or 0)} days apart.",
                  services=len(ds), median_days=_r(_med(gaps) or 0))


def _division_sheets(ctx, division=None):
    return [s for s in ctx["b"]["sheets"] if division is None or s["division_code"] == division]


def _group_division(ctx):
    return ctx["meta"].get("division") or ctx["meta"]["object"]


def c_fa_by_division(ctx):
    by_md, by_kg = defaultdict(float), defaultdict(float)
    seen = set()
    for s in ctx["b"]["sheets"]:
        by_md[s["division_code"]] += _f(s["man_days"])
        key = (s["division_code"], s["block_code"], s["material"], s["issue_date"])
        if key not in seen:
            seen.add(key)
            by_kg[s["division_code"]] += _f(s["kg_applied"])
    rate = {d: by_md[d] / (by_kg[d] / 1000) for d in by_md if by_kg[d]}
    d = _group_division(ctx)
    others = [v for k, v in rate.items() if k != d]
    return _check("fa_by_division", "Man-days per tonne by division",
                  f"Division {d}: {_r(rate.get(d, 0), 1)} man-days a tonne applied; the other divisions a median {_r(_med(others) or 0, 1)}.",
                  division=d, md_per_t=_r(rate.get(d, 0), 1), others_median=_r(_med(others) or 0, 1))


def c_fa_terrain(ctx):
    d = _group_division(ctx)
    per = defaultdict(lambda: [0.0, 0.0])
    seen = set()
    for s in _division_sheets(ctx, d):
        k = finance._k(s["division_code"], s["block_code"])
        per[k][0] += _f(s["man_days"])
        key = (k, s["material"], s["issue_date"])
        if key not in seen:
            seen.add(key)
            per[k][1] += _f(s["kg_applied"])
    xs, ys = [], []
    for k, (md, kg) in per.items():
        bl = ctx["b"]["blocks"].get(k)
        if bl and kg:
            xs.append(bl["slope"] + bl["km_to_mill"])
            ys.append(md / (kg / 1000))
    corr = None
    if len(xs) > 4:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else 0.0
    spread = (_r(min(ys), 1), _r(max(ys), 1)) if ys else (None, None)
    return _check("fa_terrain", "Man-days against terrain",
                  f"Across Division {d}'s {len(ys)} blocks, man-days a tonne run {spread[0]} to {spread[1]}; their link to slope and distance is "
                  f"{_r(corr or 0, 2)} on a scale of -1 to 1.",
                  blocks=len(ys), low=spread[0], high=spread[1], terrain_correlation=_r(corr or 0, 2))


def c_fa_sheet_timing(ctx):
    d = _group_division(ctx)

    def late_share(rows):
        late = sum(1 for s in rows if (date.fromisoformat(s["date"]) - date.fromisoformat(s["issue_date"])).days > 3)
        return late, len(rows)
    l1, n1 = late_share(_division_sheets(ctx, d))
    lo = [late_share([s for s in ctx["b"]["sheets"] if s["division_code"] == x]) for x in
          sorted({s["division_code"] for s in ctx["b"]["sheets"]}) if x != d]
    other_pct = _med([_pct(a, n) for a, n in lo if n]) or 0
    return _check("fa_sheet_timing", "Sheet dates against the issue",
                  f"{l1} of Division {d}'s {n1} contract sheets are dated more than 3 days after the fertiliser was issued; "
                  f"in the other divisions a median {_r(other_pct)}%.",
                  late_sheets=l1, sheets=n1, others_late_pct=_r(other_pct))


def c_fa_contractor_elsewhere(ctx):
    d = _group_division(ctx)
    vend = next((s["lifnr"] for s in _division_sheets(ctx, d)), None)
    other = [s for s in ctx["b"]["sheets"] if s["lifnr"] == vend and s["division_code"] != d]
    md = sum(_f(s["man_days"]) for s in other)
    seen, kg = set(), 0.0
    for s in other:
        key = (s["division_code"], s["block_code"], s["material"], s["issue_date"])
        if key not in seen:
            seen.add(key)
            kg += _f(s["kg_applied"])
    divs = sorted({s["division_code"] for s in other})
    return _check("fa_contractor_elsewhere", "The same contractor elsewhere",
                  f"Contractor {vend} also works Division{'s' if len(divs) != 1 else ''} {', '.join(divs)} at "
                  f"{_r(md / (kg / 1000), 1) if kg else 0} man-days a tonne.",
                  contractor=vend, other_divisions=", ".join(divs), other_md_per_t=_r(md / (kg / 1000), 1) if kg else 0)


def c_oi_size(ctx):
    k = ctx["meta"]["object"] if ctx["meta"].get("unit_kind") == "block" else None
    rows = [r for r in ctx["b"]["fertiliser"] if r["issue_date"] and finance._in_window(r["issue_date"])
            and (k is None or finance._k(r["division_code"], r["block_code"]) == k)
            and (k is not None or r["division_code"] == _group_division(ctx))]
    ex = [_f(r["issued_kg"]) - _f(r["target_kg"]) for r in rows if _f(r["issued_kg"]) > _f(r["target_kg"])]
    return _check("oi_size", "Size of the excess",
                  f"{len(ex)} of {len(rows)} issues over prescription, by a median {_r(_med(ex) or 0)} kg (a bag is 50 kg).",
                  over_issues=len(ex), issues=len(rows), median_excess_kg=_r(_med(ex) or 0))


def c_oi_spread(ctx):
    d = _group_division(ctx) if ctx["meta"].get("unit_kind") != "block" else ctx["meta"]["division"]
    by = defaultdict(lambda: [0, 0])
    for r in ctx["b"]["fertiliser"]:
        if r["issue_date"] and finance._in_window(r["issue_date"]) and r["division_code"] == d:
            k = finance._k(r["division_code"], r["block_code"])
            by[k][1] += 1
            by[k][0] += 1 if _f(r["issued_kg"]) > _f(r["target_kg"]) else 0
    blocks_over = sum(1 for a, n in by.values() if a)
    return _check("oi_spread", "Across the division",
                  f"{blocks_over} of Division {d}'s {len(by)} blocks had at least one issue over prescription.",
                  blocks_over=blocks_over, blocks=len(by))


def _receipts(ctx):
    m = ctx["meta"]["object"]
    return [i for i in ctx["b"]["invoices"] if i["matnr"] == m and _in_month(i["budat"], ctx["mo"])]


def c_pv_order_type(ctx):
    rs = _receipts(ctx)
    verpr = _f(ctx["b"]["materials"][ctx["meta"]["object"]]["verpr"])
    rush = sum(_f(i["amount_idr"]) - _f(i["menge"]) * verpr for i in rs if i["bsart"] == "RUSH")
    tot = sum(_f(i["amount_idr"]) - _f(i["menge"]) * verpr for i in rs)
    return _check("pv_order_type", "Rush buys",
                  f"{sum(1 for i in rs if i['bsart'] == 'RUSH')} of {len(rs)} receipts were rush buys, carrying {_pct(max(rush, 0), max(tot, 1))}% of the excess.",
                  rush_receipts=sum(1 for i in rs if i["bsart"] == "RUSH"), receipts=len(rs),
                  rush_share_pct=_pct(max(rush, 0), max(tot, 1)))


def c_pv_invoice_vs_po(ctx):
    rs = [i for i in _receipts(ctx) if i["bsart"] != "RUSH"]
    over = [i for i in rs if _f(i["invoice_price"]) > _f(i["po_price"]) * 1.005]
    vend = sorted({i["lifnr"] for i in over})
    pct = _med([100 * (_f(i["invoice_price"]) / _f(i["po_price"]) - 1) for i in over]) if over else 0
    return _check("pv_invoice_vs_po", "Invoice against the PO",
                  f"{len(over)} of {len(rs)} normal receipts were invoiced above their own PO price"
                  + (f", by a median {_r(pct, 1)}%, all from {', '.join(vend)}." if over else "."),
                  invoices_over_po=len(over), normal_receipts=len(rs), median_over_pct=_r(pct, 1),
                  vendors=", ".join(vend) if vend else None)


def c_pv_market(ctx):
    m = ctx["meta"]["object"]
    verpr = _f(ctx["b"]["materials"][m]["verpr"])
    pos = [i for i in _receipts(ctx) if i["bsart"] != "RUSH"]
    po_pct = _med([100 * (_f(i["po_price"]) / verpr - 1) for i in pos]) if pos else None
    return _check("pv_market", "PO prices against standard",
                  f"Normal PO prices sat a median {_r(po_pct or 0, 1)}% from the standard price of {_r(verpr)}.",
                  po_vs_standard_pct=_r(po_pct or 0, 1), standard=_r(verpr))


def c_wo_counts(ctx):
    m = ctx["meta"]["object"]
    rows = [p for p in ctx["b"]["postings"] if p["racct"] == "6502000" and p["matnr"] == m]
    gains = sum(1 for p in rows if p["wsl"] < 0)
    return _check("wo_counts", "Counts", f"{len(rows)} count postings in the window, {gains} of them gains.",
                  postings=len(rows), gains=gains)


def _seg_orders(ctx):
    sid = ctx["meta"]["object"]
    return [o for o in ctx["b"]["road_orders"] if o["segment_id"] == sid]


def c_rr_interval(ctx):
    cs = sorted((o for o in _seg_orders(ctx) if o["activity"] == "culvert"), key=lambda o: o["date"])
    gaps = [(date.fromisoformat(b_["date"]) - date.fromisoformat(a["date"])).days for a, b_ in zip(cs, cs[1:])]
    costs = [_f(o["cost_idr"]) for o in cs]
    allc = [_f(o["cost_idr"]) for o in ctx["b"]["road_orders"] if o["activity"] == "culvert"]
    return _check("rr_interval", "Repairs on this culvert",
                  f"{len(cs)} culvert repairs, {', '.join(o['date'] for o in cs)}; {', '.join(str(g) for g in gaps)} days apart. "
                  f"Median cost {finance.rp(_med(costs) or 0)} against {finance.rp(_med(allc) or 0)} estate-wide.",
                  repairs=len(cs), dates=", ".join(o["date"] for o in cs), gaps_days=", ".join(str(g) for g in gaps),
                  median_cost=_r(_med(costs) or 0), estate_median_cost=_r(_med(allc) or 0))


def c_rr_rain(ctx):
    rain = ctx["b"]["rain"]
    cs = [o for o in _seg_orders(ctx) if o["activity"] == "culvert"]
    wet = 0
    for o in cs:
        d = date.fromisoformat(o["date"])
        week = sum(rain.get((d - timedelta(days=i)).isoformat(), 0.0) for i in range(1, 8))
        wet += 1 if week >= 60 else 0
    return _check("rr_rain", "Rain before the repairs",
                  f"{wet} of {len(cs)} repairs followed a week with 60 mm of rain or more.", after_wet_week=wet, repairs=len(cs))


def c_rr_traffic(ctx):
    r = ctx["b"]["roads"][ctx["meta"]["object"]]
    k = finance._k(r["division_code"], r["block_code"])
    trips = defaultdict(int)
    for t in ctx["b"]["trips"]:
        trips[t["key"]] += 1
    return _check("rr_traffic", "Traffic",
                  f"{trips.get(k, 0)} loaded trips from its block in the window; the median block had {_r(_med(trips.values()) or 0)}.",
                  trips=trips.get(k, 0), median_trips=_r(_med(trips.values()) or 0))


def c_bl_by_division(ctx):
    by = defaultdict(lambda: [0.0, 0.0, set()])
    for o in ctx["b"]["road_orders"]:
        if o["activity"] == "grading" and finance._in_window(o["date"]):
            a = by[o["division_code"]]
            a[0] += _f(o["km_billed"])
            a[1] += _f(o["length_km"])
            a[2].add(o["lifnr"])
    d = _group_division(ctx) if ctx["meta"].get("unit_kind") != "segment" else ctx["meta"]["division"]
    ratio = {x: _pct(a[0], a[1]) for x, a in by.items()}
    others = [v for x, v in ratio.items() if x != d]
    return _check("bl_by_division", "Billed against surveyed length",
                  f"Division {d}: {ratio.get(d)}% of the surveyed length billed, by {', '.join(sorted(by[d][2]))}; "
                  f"the other divisions a median {_r(_med(others) or 0)}%.",
                  division=d, billed_pct=ratio.get(d), others_median_pct=_r(_med(others) or 0),
                  contractor=", ".join(sorted(by[d][2])))


def c_bl_since(ctx):
    d = _group_division(ctx) if ctx["meta"].get("unit_kind") != "segment" else ctx["meta"]["division"]
    by = defaultdict(lambda: [0.0, 0.0])
    for o in ctx["b"]["road_orders"]:
        if o["activity"] == "grading" and o["division_code"] == d:
            a = by[o["date"][:7]]
            a[0] += _f(o["km_billed"])
            a[1] += _f(o["length_km"])
    rows = [(m, _pct(a[0], a[1])) for m, a in sorted(by.items()) if a[1]]
    first = next((m for m, p in rows if p and p > 110), None)
    return _check("bl_since", "Since when",
                  (f"Billing ran near the surveyed length until {first}, then over it in every month since." if first
                   else "No month billed more than 10% over length."),
                  first_month_over=first, months=len(rows))


def c_grading_interval(ctx):
    gs = sorted(o["date"] for o in _seg_orders(ctx) if o["activity"] == "grading")
    gaps = [(date.fromisoformat(b_) - date.fromisoformat(a)).days for a, b_ in zip(gs, gs[1:])]
    return _check("grading_interval", "Grading interval",
                  f"{len(gs)} gradings in 12 months, a median {_r(_med(gaps) or 0)} days apart.",
                  gradings=len(gs), median_days=_r(_med(gaps) or 0))


def c_upkeep_overdue_at_job(ctx):
    f = ctx["row"]["facts"]
    return _check("upkeep_overdue_at_job", "Overdue rounds",
                  f"Rounds on the block sat {_r(f.get('overdue_days', 0))} block-activity days past their interval this month.",
                  overdue_days=_r(f.get("overdue_days", 0)))


def c_upkeep_crew_pace(ctx):
    k = ctx["meta"]["object"]
    os_ = [o for o in ctx["b"]["orders"] if o["key"] == k and o["operation"] in ("prune", "weed", "spray")
           and _in_month(o["date"], ctx["mo"])]
    crews = sorted({o["crew_code"] for o in os_})
    return _check("upkeep_crew_pace", "Who did the work",
                  f"{len(os_)} upkeep orders by {', '.join(crews) or 'no crew'}; {sum(1 for o in os_ if o['status'] != 'completed')} not completed on the day.",
                  orders=len(os_), crews=", ".join(crews), partial=sum(1 for o in os_ if o["status"] != "completed"))


def c_dose_by_team(ctx):
    f = ctx["row"]["facts"]
    return _check("dose_by_team", "Dose", f"Herbicide issued {_r(f.get('over_pct', 0))}% above the dose for the hectares sprayed.",
                  over_pct=_r(f.get("over_pct", 0)))


def c_dose_by_job(ctx):
    k = ctx["meta"]["object"]
    n = sum(1 for m in ctx["b"]["movements"] if m["bwart"] == "201" and m["matnr"] in ("AC-001", "AC-002")
            and m["block_code"] and finance._k(m["kostl"][4:], m["block_code"]) == k and _in_month(m["budat"], ctx["mo"]))
    return _check("dose_by_job", "Issues", f"{n} herbicide issues to the block this month.", issues=n)


def c_pest_census_age(ctx):
    k = ctx["meta"]["object"]
    cs = sorted(c["census_date"] for c in ctx["b"]["census"] if finance._k(c["division_code"], c["block_code"]) == k)
    return _check("pest_census_age", "Census", f"Census rounds on the block: {', '.join(cs) or 'none'}.",
                  census_dates=", ".join(cs) or None)


def c_pest_neighbours(ctx):
    d = ctx["meta"]["division"]
    pos = sum(1 for c in ctx["b"]["census"] if c["division_code"] == d and _f(c["ganoderma_confirmed"]) > 0)
    return _check("pest_neighbours", "Neighbours", f"{pos} census rows in Division {d} found Ganoderma.", positive_rows=pos)


def c_pest_pace(ctx):
    f = ctx["row"]["facts"]
    return _check("pest_pace", "Pace", f"{_r(f.get('md_over', 0))} man-days above the rate card for the palms treated.",
                  md_over=_r(f.get("md_over", 0)))


CHECKS = {name[2:]: fn for name, fn in list(globals().items()) if name.startswith("c_") and callable(fn)}


# ── grounding, the call, validation ────────────────────────────────────────

def _ctx(uid, month):
    st = finance.state()
    mo = finance._month(month)
    if uid.startswith("group:"):
        g = finance._group_node(st, uid, mo)
        if not g.get("active"):
            return None
        _, f, d, drv = uid.split(":")
        meta = {"id": uid, "function": f, "unit_kind": "division", "object": d, "division": d,
                "label": g["label"], "blocks": g.get("blocks", []),
                "members": [m["id"] for m in g.get("members", [])]}
        row = {"facts": {}, "drivers": {x["driver"]: x["idr"] for x in g["drivers"]}, "top_driver": drv,
               "leakage": g["leakage_idr"], "S": g["spend_idr"], "L": g["lost_idr"], "N": g["necessary_idr"],
               "why": g["why"]}
        public = {k: g[k] for k in ("leakage_idr", "efficiency_pct", "spend_idr", "lost_idr", "necessary_idr",
                                    "driver", "driver_label", "driver_idr", "why", "label", "function_label")}
        public["members"] = [m["label"] for m in g.get("members", [])][:12]
    else:
        um = finance.unit_month(uid, mo)
        if not um:
            return None
        meta, row, public = um["meta"], um["row"], um["public"]
    return {"b": finance.base(), "st": st, "uid": uid, "mo": mo, "meta": meta, "row": row,
            "public": public, "av": st["av"]}


def run_checks(uid, month) -> dict | None:
    ctx = _ctx(uid, month)
    if not ctx:
        return None
    drv = ctx["row"]["top_driver"]
    if not drv:
        return {"driver": None, "checks": [], "ctx": ctx}
    out = []
    for cid in pb.driver(drv)["checks"]:
        fn = CHECKS.get(cid)
        if not fn:
            continue
        try:
            out.append(fn(ctx))
        except Exception as exc:                 # one broken check never sinks the rest
            log.warning("[diagnosis] check %s failed on %s: %s", cid, uid, exc)
    return {"driver": drv, "checks": out, "ctx": ctx}


def _past_findings(meta, driver) -> list:
    try:
        from gis import decisions
        rows = decisions.history(200)["decisions"]
    except Exception:
        return []
    out = []
    for r in rows:
        art = r.get("artifact") or {}
        obs = r.get("observed_effect") or {}
        if r.get("artifact_kind") != "cost_action" or not isinstance(obs, dict) or not obs.get("cause_found"):
            continue
        line = (art.get("lines") or [{}])[0]
        if line.get("unit_id") == meta["id"] or line.get("driver") == driver:
            out.append({"unit": line.get("unit"), "driver": line.get("driver"), "cause_found": obs["cause_found"],
                        "note": obs.get("note"), "closed_at": obs.get("closed_at"),
                        "same_unit": line.get("unit_id") == meta["id"]})
    return out[:6]


def grounding(uid, month, with_checks=True) -> dict | None:
    res = run_checks(uid, month)
    if not res or not res["driver"]:
        return None
    ctx, drv = res["ctx"], res["driver"]
    d = pb.driver(drv)
    p = ctx["public"]
    saving = p.get("driver_idr") or 0
    lang = "id" if int(assumptions.get("diagnosis_in_bahasa")) == 1 else "en"
    g = {
        "language": lang,
        "unit": {"id": uid, "label": p.get("label"), "function": pb.FUNCTIONS[ctx["meta"]["function"]]["label"],
                 "division": ctx["meta"].get("division") or None, "month": finance.MONTH_LABEL[ctx["mo"]],
                 "members": p.get("members"), "provenance": "synthetic: generated SAP FI/CO and operations records"},
        "figures": {"leakage": finance.rp(p.get("leakage_idr")), "efficiency_pct": p.get("efficiency_pct"),
                    "spend": finance.rp(p.get("spend_idr")), "crop_lost": finance.rp(p.get("lost_idr")),
                    "necessary": finance.rp(p.get("necessary_idr")), "this_driver": finance.rp(saving)},
        "driver": {"id": drv, "label": d["label"], "reading": p.get("why")},
        "candidate_causes": [dict(c) for c in d["causes"]] + [dict(WITHIN_NORM)],
        "checks": res["checks"] if with_checks else [],
        "actions": [{"id": a["id"], "text": a["text"], "owner": a["owner"], "saving_a_month": finance.rp(saving),
                     "document": a.get("document")} for a in d["actions"]],
        "past_findings": _past_findings(ctx["meta"], drv),
        "_saving_idr": saving,
    }
    if not with_checks:
        g["note"] = "Checks withheld for the ablation run."
    return g


def _validate(parsed: dict, g: dict) -> tuple[bool, list, dict]:
    problems = []
    causes = {c["id"] for c in g["candidate_causes"]} | {"other"}
    checks = {c["id"] for c in g["checks"]}
    actions = {a["id"] for a in g["actions"]}
    cause = parsed.get("cause") or {}
    if cause.get("id") not in causes:
        problems.append(f"cause {cause.get('id')!r} is not a candidate")
    for e in cause.get("evidence") or []:
        if e not in checks:
            problems.append(f"evidence {e!r} is not a check")
    for r in parsed.get("ruled_out") or []:
        if r.get("id") not in causes:
            problems.append(f"ruled-out cause {r.get('id')!r} is not a candidate")
        for e in r.get("evidence") or []:
            if e not in checks:
                problems.append(f"evidence {e!r} is not a check")
    if not parsed.get("actions"):
        problems.append("no actions")
    for a in parsed.get("actions") or []:
        if a.get("id") not in actions:
            problems.append(f"action {a.get('id')!r} is not in the playbook")
    text = " ".join(str(x) for x in _walk(parsed))
    audit = reasoning.audit_figures(text, g)
    if not audit["clean"]:
        problems.append(f"unverified figures: {', '.join(audit['unverified'][:6])}")
    return not problems, problems, audit


def _walk(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in ("id", "evidence", "confidence"):
                yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


def _shape(parsed: dict, g: dict) -> dict:
    causes = {c["id"]: c for c in g["candidate_causes"]}
    checks = {c["id"]: c for c in g["checks"]}
    actions = {a["id"]: a for a in g["actions"]}
    c = parsed.get("cause") or {}
    cid = c.get("id")
    return {
        "headline": str(parsed.get("headline", "")).strip(),
        "cause": {"id": cid, "label": (causes.get(cid) or {"label": "Something else"})["label"],
                  "confidence": c.get("confidence") if c.get("confidence") in ("likely", "possible") else "possible",
                  "evidence": [{"id": e, "label": checks[e]["label"]} for e in c.get("evidence") or [] if e in checks],
                  "sentence": str(c.get("sentence", "")).strip(), "vetted": cid != "other"},
        "ruled_out": [{"id": r.get("id"), "label": (causes.get(r.get("id")) or {"label": r.get("id")})["label"],
                       "evidence": [{"id": e, "label": checks[e]["label"]} for e in r.get("evidence") or [] if e in checks],
                       "sentence": str(r.get("sentence", "")).strip()}
                      for r in parsed.get("ruled_out") or [] if r.get("id") in causes],
        "confirm": str(parsed.get("confirm", "")).strip(),
        "actions": [{"id": a["id"], "text": str(a.get("text") or actions[a["id"]]["text"]).strip(),
                     "owner": actions[a["id"]]["owner"], "when": a.get("when") or "this week",
                     "saving_idr_month": g["_saving_idr"],
                     "document": actions[a["id"]].get("document")}
                    for a in (parsed.get("actions") or [])[:3] if a.get("id") in actions],
    }


def template(uid, month) -> dict | None:
    """The rule-based Why and Do: what the page shows without the model."""
    ctx = _ctx(uid, month)
    if not ctx or not ctx["row"]["top_driver"]:
        return None
    drv = ctx["row"]["top_driver"]
    a = pb.driver(drv)["actions"][0]
    return {"driver": drv, "driver_label": pb.driver(drv)["label"], "why": ctx["public"].get("why"),
            "do": {"id": a["id"], "text": a["text"], "owner": a["owner"],
                   "saving_idr_month": ctx["public"].get("driver_idr")}}


def _key(uid, mo, lang, with_checks):
    try:
        from gis import decisions
        closed = sum(1 for r in decisions.history(500)["decisions"]
                     if r.get("artifact_kind") == "cost_action" and r.get("observed_effect"))
    except Exception:
        closed = 0
    reg = hashlib.sha1(assumptions.fingerprint().encode()).hexdigest()[:10]
    return f"{uid}|{mo}|{lang}|{reg}|{closed}|{int(with_checks)}"


def cached(uid, month) -> dict | None:
    mo = finance._month(month)
    lang = "id" if int(assumptions.get("diagnosis_in_bahasa")) == 1 else "en"
    return _CACHE.get(_key(uid, mo, lang, True))


def diagnose(uid: str, month: str | None = None, refresh: bool = False, with_checks: bool = True,
             lang: str | None = None, force: bool = False) -> dict:
    """The likely cause of one unit's leak this month.

    Leaks under the materiality line are not sent to the model: they are within normal
    variation by the register's own definition, and the eval showed the model reads a
    confident cause into that noise. `force` skips the gate, for the eval only."""
    mo = finance._month(month)
    if not finance.available():
        return reasoning.unavailable("No CO ledger.")
    tmpl = template(uid, mo)
    if not tmpl:
        return {"available": False, "reason": "No leak to diagnose on this unit this month.", "id": uid, "month": mo}
    if not force and not uid.startswith("group:"):
        um = finance.unit_month(uid, mo)
        mat = assumptions.get("leak_materiality_idr")
        if um and um["row"]["leakage"] < mat:
            return {"available": False, "within_norm": True, "id": uid, "month": mo, "template": tmpl,
                    "reason": (f"Under the {finance.rp(mat)} line: within normal variation for its peers, "
                               "so it is not sent to the model.")}
    if int(assumptions.get("use_leak_diagnosis")) != 1:
        return {"available": False, "reason": "The AI diagnosis is switched off in the assumption register.",
                "id": uid, "month": mo, "template": tmpl, "switched_off": True}
    if lang in ("en", "id"):
        pass
    else:
        lang = "id" if int(assumptions.get("diagnosis_in_bahasa")) == 1 else "en"
    key = _key(uid, mo, lang, with_checks)
    if not refresh:
        hit = _CACHE.get(key)
        if hit:
            return hit
    g = grounding(uid, mo, with_checks)
    g["language"] = lang
    system = load_prompt("estate_leak_diagnosis_system")
    user = "Diagnose this leak.\n\n" + reasoning.clip({k: v for k, v in g.items() if not k.startswith("_")})
    attempts = []
    shaped = None
    audit = None
    meta = {}
    for temp in (0.2, 0.0):
        parsed, meta = reasoning.json_chat(system, user, tier="reasoning", max_tokens=1100, temperature=temp,
                                           task="estate_leak_diagnosis")
        if parsed is None:
            attempts.append({"temperature": temp, "problems": [meta.get("error") or "unparseable"]})
            if meta.get("error"):
                break                                   # the model is down; a retry will not help
            continue
        ok, problems, audit = _validate(parsed, g)
        attempts.append({"temperature": temp, "problems": problems})
        if ok:
            shaped = _shape(parsed, g)
            break
    if shaped is None:
        out = {"available": False, "id": uid, "month": mo, "template": tmpl,
               "reason": ("The model could not be reached." if any(
                   "error" in str(a) or "unparseable" in str(a) for a in attempts) and not audit
                          else "The model's answer did not pass the checks, so the rule-based reading stands."),
               "attempts": attempts, "checks": g["checks"]}
        return out
    out = {"available": True, "id": uid, "month": mo, "month_label": finance.MONTH_LABEL[mo], "lang": lang,
           "driver": g["driver"], "diagnosis": shaped, "checks": g["checks"], "audit": audit,
           "attempts": attempts, "with_checks": with_checks, "template": tmpl,
           "model": {"model": meta.get("model"), "latency_ms": meta.get("latency_ms"), "usage": meta.get("usage")},
           "grounding": g}
    _CACHE.put(key, out)
    return out


def warm_top(month: str | None = None, top: int = 10) -> None:
    """Diagnose the default month's top ten in the background, so the first click is instant."""
    if int(assumptions.get("use_leak_diagnosis")) != 1:
        return
    def run():
        try:
            st = finance.state()
            mo = finance._month(month)
            for r in finance.top_leaks(st, mo, top):
                try:
                    diagnose(r["id"], mo)
                except Exception as exc:
                    log.info("[diagnosis] warm %s failed: %s", r["id"], exc)
            log.info("[diagnosis] top %d warm for %s", top, mo)
        except Exception as exc:
            log.info("[diagnosis] warm-up skipped: %s", exc)
    threading.Thread(target=run, name="diagnosis-warm", daemon=True).start()


def clear() -> None:
    _CACHE.clear()
