"""Synthetic operations ledger: crews, attendance and work orders.

Run once:  python gis/build_operations.py
           (also run at the end of gis/build_synthetic.py)
Writes:    gis/data/synthetic/ec_crews.csv
           gis/data/synthetic/ec_attendance.csv
           gis/data/synthetic/ec_harvest_orders.csv
           gis/data/synthetic/ec_upkeep_orders.csv
           gis/data/synthetic/ec_pest_orders.csv
           gis/data/synthetic/ec_dispatch_orders.csv
Rewrites:  gis/data/synthetic/ec_upkeep.csv
           gis/data/synthetic/ec_pest_treatment.csv
           so the STATE feeds agree with the EVENT feeds written here.

What this is for
----------------
Every other synthetic feed is a demand signal: which blocks are overdue, where
the disease is, how much fruit is at the collection point. None of them says
who was sent where, what they were told to do, and what they actually did.
That supply side is the work order, one row shape for six operations:

    order_id  date  operation  division  block  crew
    headcount_plan / headcount_actual
    planned_qty / actual_qty  unit
    man_days_plan / man_days_actual
    status  started  finished  carried_to

planned_qty beside actual_qty is the whole point. Adherence, productivity and
slippage are all ratios of those two columns at different grains, and none of
them existed at any grain before this file.

Four rules, each of which is easy to get wrong
-----------------------------------------------
1. Actuals do not match plan. Harvest adherence is calibrated to a 78%
   bunch-weighted mean, upkeep runs wider, and every miss is driven by
   something real: rainfall ON THE DAY (Open-Meteo daily, real), the block's
   road condition (ec_roads.csv) and the crew's attendance that morning. A
   miss that correlates with nothing teaches the panel nothing.

2. Harvest orders reconcile to the real bunch counts. actual_qty is taken
   from the per-worker rows in ec_harvester_day.csv, which were themselves
   generated DOWN from the client's block-month totals. Sum the ledger by
   block and month and you get the EPMS figure. The PLAN is what is invented,
   backed out from the actual through the adherence model.

3. Attendance reproduces ec_labour.csv. The division-month attendance rate
   in that feed - including the Lebaran dip in March and April - is hit
   exactly, to the rounding, by the daily crew rows here.

4. Slippage chains. Upkeep and pest work are SIMULATED day by day: each crew
   picks its most overdue job every morning, works it with whoever turned up
   under whatever fell from the sky, and carries the remainder to tomorrow.
   A partial order names the order it became. The round stretches because the
   simulation stretched it, not because a column was drawn that way, and the
   upkeep state file is rewritten from the result so the two cannot disagree.

The anchor date
---------------
The real harvest export ends 2025-05-23. The ledger covers exactly that
window, so every harvest row can be checked line by line against EPMS.
"Tomorrow" throughout the build is 2025-05-24: the first day beyond the
client's own records.
"""

import csv
import logging
import math
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis import build_synthetic as bs
from gis.build_synthetic import (CENSUS_ROUNDS, OUT, SEED, UPKEEP_INTERVALS,
                                 _bk, _centroid, _latent_field, _write)

log = logging.getLogger("estate-command.operations")

WINDOW_START = date(2025, 1, 1)
WINDOW_END = bs.WINDOW_END            # 2025-05-23, the last day of the export
TOMORROW = WINDOW_END + timedelta(days=1)

# Lebaran 2025 fell on 31 March. The migration thins the workforce for a
# fortnight either side; ec_labour.csv already carries the month-level dip and
# this is where inside the month it sits.
LEBARAN = (date(2025, 3, 24), date(2025, 4, 9))

# Rain thresholds, in millimetres on the day. Above HEAVY a spray round is
# called off (herbicide washes off) and weeding slows; above STOP nobody cuts.
RAIN_HEAVY_MM = 25.0
RAIN_STOP_MM = 45.0

# Units per man-day. The same figures are registered in gis/assumptions.py,
# where the client can edit them; the ledger is generated at these values and
# the register prices tomorrow's plan at whatever they are set to.
RATES = {
    "pruning": 55.0,          # palms, mature stand, chisel and pole
    "circle_weeding": 1.1,    # ha, manual circle
    "path_upkeep": 1.5,       # ha, harvest path slashing
    "spraying": 2.6,          # ha, knapsack
    "census": 400.0,          # palms walked and inspected
    "treatment": {"ganoderma": 30.0, "rhinoceros_beetle": 120.0, "rat": 200.0},
}
ACTIVITY_OPERATION = {"pruning": "prune", "circle_weeding": "weed",
                      "path_upkeep": "weed", "spraying": "spray"}
ACTIVITY_UNIT = {"pruning": "palms", "circle_weeding": "ha",
                 "path_upkeep": "ha", "spraying": "ha"}

# Calibration targets for adherence. Harvest is hit exactly on the
# bunch-weighted mean; upkeep is a base the simulation runs at.
HARVEST_ADHERENCE = 0.78
UPKEEP_ADHERENCE = 0.80

# An order is "completed" at this share of its plan. A mandor signs off a
# block at 85% and moves on; below that the remainder is carried. The ops
# layer reads the same line from here so the ledger and the panel agree.
COMPLETE_AT = 0.85

# Herbicide washes off above this much rain on the day, so a spray round is
# called off. Weeding and pruning slow rather than stop until RAIN_STOP_MM.
SPRAY_RAIN_MM = 15.0

# Attendance and adherence the crews are sized against. Capacity that just
# meets the programme at average attendance is what makes a wet fortnight or
# Lebaran push a round late, which is the behaviour the ledger has to show.
PLANNING_ATTENDANCE = 0.85
PLANNING_ADHERENCE = 0.80
UPKEEP_CAPACITY_SLACK = 1.12
# Spray teams carry more, because a wet fortnight takes every sprayable day
# out at once and the backlog it leaves is what the ledger should show, not
# a team that never catches up from January.
SPRAY_CAPACITY_SLACK = 1.35

ORDER_HEADER = ["order_id", "date", "operation", "activity", "division_code",
                "block_code", "crew_code", "headcount_plan", "headcount_actual",
                "planned_qty", "actual_qty", "unit", "man_days_plan",
                "man_days_actual", "status", "started", "finished", "carried_to"]


# ── helpers ────────────────────────────────────────────────────────────────

def _read(name: str) -> list[dict]:
    path = OUT / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    return list(csv.DictReader(lines))


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _hhmm(h: float) -> str:
    h = max(0.0, min(23.98, h))
    return f"{int(h):02d}:{int(round((h % 1) * 60)) % 60:02d}"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _key(r) -> str:
    return _bk({"division_code": r["division_code"], "block_code": r["block_code"]})


def _block_info(blocks) -> dict:
    """What the generators need per block, keyed div|block."""
    info = {}
    for i, f in enumerate(blocks):
        p = f["properties"]
        k = _bk(p)
        info[k] = {
            "key": k, "index": i, "id": f["id"],
            "division": str(int(p["division_code"])),
            "block_code": p["block_code"],
            "label": p.get("block_label"),
            "ha": p.get("planted_ha") or 0.0,
            "palms": p.get("palms") or 0,
            "age": p.get("palm_age_years") or 10,
            "centroid": _centroid(f["geometry"]["coordinates"][0]),
            "bunches_by_month": p.get("bunches_by_month") or {},
            "harvest_days_by_month": p.get("harvest_days_by_month") or {},
        }
    return info


def _daily_rain() -> tuple[dict, str]:
    """Real daily rainfall over the estate, from the Open-Meteo pull.

    Falls back to spreading each month's REAL total across its REAL rain-day
    count when the archive was pulled before the daily series was kept. The
    provenance string says which happened, because the ledger's notes quote it.
    """
    from gis import environment
    daily = environment.rainfall_by_day("EC")
    if daily:
        return daily, "real: Open-Meteo daily precipitation on the day"
    rng = random.Random(f"{SEED}:rain")
    out = {}
    for m in environment.rainfall_months("EC"):
        y, mo = int(m["month"][:4]), int(m["month"][5:7])
        last = (date(y + (mo == 12), (mo % 12) + 1, 1) - timedelta(days=1)).day
        wet = sorted(rng.sample(range(1, last + 1), min(last, m["rain_days"] or 0)))
        weights = [rng.expovariate(1.0) for _ in wet]
        tot = sum(weights) or 1.0
        for dd in range(1, last + 1):
            out[date(y, mo, dd).isoformat()] = 0.0
        for dd, w in zip(wet, weights):
            out[date(y, mo, dd).isoformat()] = round(m["rain_mm"] * w / tot, 1)
    return out, ("real monthly totals and rain-day counts (Open-Meteo), daily "
                 "placement generated")


def _road_effect(cond: str, rain: float) -> float:
    base = {"good": 1.0, "fair": 0.97, "poor": 0.91,
            "impassable-when-wet": 0.86}.get(cond, 0.97)
    if cond == "impassable-when-wet" and rain >= 15:
        base *= 0.65
    elif cond == "poor" and rain >= 25:
        base *= 0.85
    return base


# ── crews ──────────────────────────────────────────────────────────────────

def gen_crews(info, rot, rng, rain=None) -> tuple[list, dict]:
    """Every crew on the estate, and the blocks each upkeep crew looks after.

    Harvest gangs are ec_gangs.csv carried through under the same gang_code,
    so the existing ec_rotation.csv join survives untouched. Upkeep crews are
    sized from the programme: enough men to keep every round at planning
    attendance and adherence, and no more, so weather and Lebaran show.
    """
    gangs = _read("ec_gangs.csv")
    by_div: dict[str, list] = defaultdict(list)
    for k, b in info.items():
        by_div[b["division"]].append(b)
    for blks in by_div.values():
        blks.sort(key=lambda b: b["index"])

    gang_blocks: dict[str, list] = defaultdict(list)
    for k, r in rot.items():
        gang_blocks[r["gang_code"]].append(info[k])

    crews, territory = [], {}
    window = [d.isoformat() for d in _days(WINDOW_START, WINDOW_END)]
    sprayable = (sum(1 for d in window if (rain or {}).get(d, 0.0) < SPRAY_RAIN_MM)
                 / len(window)) if rain else 0.7

    def home(blks):
        b = min(blks, key=lambda x: x["index"]) if blks else None
        return ((b["label"], round(b["centroid"][0], 6), round(b["centroid"][1], 6))
                if b else (None, None, None))

    def skill():
        return round(_clamp(rng.gauss(1.0, 0.08), 0.8, 1.2), 3)

    for g in gangs:
        blks = sorted(gang_blocks.get(g["gang_code"], []), key=lambda b: b["index"])
        lbl, lon, lat = home(blks)
        crews.append({
            "crew_code": g["gang_code"], "crew_type": "harvest",
            "name": g["name"], "division_code": g["division_code"],
            "establishment": int(g["headcount"]), "harvesters": int(g["harvesters"]),
            "mandor": 1, "kerani": 1, "skill": skill(),
            "blocks": len(blks), "home_block": lbl, "home_lon": lon, "home_lat": lat,
        })
        territory[g["gang_code"]] = [b["key"] for b in blks]

    for div in sorted(by_div, key=int):
        blks = by_div[div]
        # Programme man-days per calendar day for the three upkeep rounds.
        need = sum(b["palms"] / RATES["pruning"] / UPKEEP_INTERVALS["pruning"]
                   + b["ha"] / RATES["circle_weeding"] / UPKEEP_INTERVALS["circle_weeding"]
                   + b["ha"] / RATES["path_upkeep"] / UPKEEP_INTERVALS["path_upkeep"]
                   for b in blks)
        total = math.ceil(need / (PLANNING_ATTENDANCE * PLANNING_ADHERENCE)
                          * 7 / 6 * UPKEEP_CAPACITY_SLACK)
        n = max(2, round(total / 16))
        per = math.ceil(total / n)
        chunk = math.ceil(len(blks) / n)
        for i in range(n):
            code = f"U{div}-{i + 1:02d}"
            mine = blks[i * chunk:(i + 1) * chunk]
            lbl, lon, lat = home(mine)
            crews.append({
                "crew_code": code, "crew_type": "upkeep",
                "name": f"Upkeep {div}-{i + 1:02d}", "division_code": div,
                "establishment": per, "harvesters": None, "mandor": 1, "kerani": 0,
                "skill": skill(), "blocks": len(mine),
                "home_block": lbl, "home_lon": lon, "home_lat": lat,
            })
            territory[code] = [b["key"] for b in mine]

        # Spray teams are sized against the days a knapsack round can actually
        # go out. Southern Papua's wet season takes a third of them, and a team
        # sized as if every day were dry would be 80% overdue by May - which
        # the first pass of this generator duly produced.
        need_s = sum(b["ha"] / RATES["spraying"] / UPKEEP_INTERVALS["spraying"] for b in blks)
        est_s = max(6, math.ceil(need_s / (PLANNING_ATTENDANCE * PLANNING_ADHERENCE
                                           * sprayable) * 7 / 6 * SPRAY_CAPACITY_SLACK))
        lbl, lon, lat = home(blks)
        crews.append({
            "crew_code": f"S{div}-01", "crew_type": "spray",
            "name": f"Spray team {div}", "division_code": div,
            "establishment": est_s, "harvesters": None, "mandor": 1, "kerani": 0,
            "skill": skill(), "blocks": len(blks),
            "home_block": lbl, "home_lon": lon, "home_lat": lat,
        })
        territory[f"S{div}-01"] = [b["key"] for b in blks]

        crews.append({
            "crew_code": f"P{div}-01", "crew_type": "pest",
            "name": f"P&D team {div}", "division_code": div,
            "establishment": rng.randint(10, 14), "harvesters": None,
            "mandor": 1, "kerani": 0, "skill": skill(), "blocks": len(blks),
            "home_block": lbl, "home_lon": lon, "home_lat": lat,
        })
        territory[f"P{div}-01"] = [b["key"] for b in blks]

    _write("ec_crews.csv", crews,
           ["crew_code", "crew_type", "name", "division_code", "establishment",
            "harvesters", "mandor", "kerani", "skill", "blocks", "home_block",
            "home_lon", "home_lat"],
           "Every crew on the estate: harvest gangs carried through from "
           "ec_gangs.csv under the same code, plus upkeep crews, spray teams "
           "and P&D teams sized from the programme. EPMS has m_gang_employee "
           "for harvest gangs only; the EC export carries none of it.")
    return crews, territory


# ── attendance ─────────────────────────────────────────────────────────────

def gen_attendance(crews, rain, used_by_gang_day, rng) -> dict:
    """One row per crew per day, calibrated to ec_labour.csv exactly.

    Day shape: Sundays low, the Lebaran fortnight low, heavy rain slightly
    low. The multipliers are normalised inside each division-month so the
    on-roll-weighted mean lands on the labour feed's attendance_rate, and a
    final pass adjusts single rows until the monthly total matches to the man.

    Harvest gangs are additionally floored at the number of harvesters the
    per-worker output feed records on their blocks that day, so a gang never
    reports fewer men present than it had cutting.
    """
    labour = {(r["division_code"], r["month"]): float(r["attendance_rate"])
              for r in _read("ec_labour.csv")}
    days = list(_days(WINDOW_START, WINDOW_END))
    months = sorted({d.strftime("%Y-%m") for d in days})
    by_div: dict[str, list] = defaultdict(list)
    for c in crews:
        by_div[c["division_code"]].append(c)

    rows, idx = [], {}
    for div, crews_d in by_div.items():
        for mo in months:
            target = labour.get((div, mo), 0.86)
            dm = [d for d in days if d.strftime("%Y-%m") == mo]
            mult = {}
            for d in dm:
                m = 0.62 if d.weekday() == 6 else 1.0
                if LEBARAN[0] <= d <= LEBARAN[1]:
                    m *= 0.72
                if rain.get(d.isoformat(), 0.0) >= RAIN_HEAVY_MM:
                    m *= 0.93
                mult[d] = m
            mean_mult = sum(mult.values()) / len(dm)
            p = {d: min(0.985, target * mult[d] / mean_mult) for d in dm}

            month_rows = []
            for d in dm:
                for c in crews_d:
                    est = c["establishment"]
                    noise = _clamp(rng.gauss(1.0, 0.06), 0.8, 1.15)
                    present = int(round(est * p[d] * noise))
                    floor = min(used_by_gang_day.get((c["crew_code"], d.isoformat()), 0), est)
                    present = _clamp(max(present, floor), 0, est)
                    month_rows.append({"crew": c, "date": d, "present": present,
                                       "floor": floor})

            want = int(round(target * sum(c["establishment"] for c in crews_d) * len(dm)))
            have = sum(r["present"] for r in month_rows)
            guard = 0
            while have != want and guard < 40000:
                guard += 1
                r = rng.choice(month_rows)
                est = r["crew"]["establishment"]
                if have < want and r["present"] < est and r["date"].weekday() != 6:
                    r["present"] += 1
                    have += 1
                elif have > want and r["present"] > r["floor"]:
                    r["present"] -= 1
                    have -= 1

            for r in month_rows:
                c, d = r["crew"], r["date"]
                est = c["establishment"]
                absent = est - r["present"]
                if absent == 0:
                    reason = ""
                elif LEBARAN[0] <= d <= LEBARAN[1]:
                    reason = "lebaran"
                elif d.weekday() == 6:
                    reason = "rest day"
                elif rain.get(d.isoformat(), 0.0) >= RAIN_HEAVY_MM:
                    reason = "rain"
                else:
                    reason = rng.choices(["sick", "leave", "no show"], [4, 3, 2])[0]
                rows.append({
                    "crew_code": c["crew_code"], "crew_type": c["crew_type"],
                    "division_code": c["division_code"], "date": d.isoformat(),
                    "on_roll": est, "present": r["present"], "absent": absent,
                    "absent_reason": reason,
                })
                idx[(c["crew_code"], d.isoformat())] = r["present"]

    rows.sort(key=lambda r: (r["date"], r["crew_code"]))
    _write("ec_attendance.csv", rows,
           ["crew_code", "crew_type", "division_code", "date", "on_roll", "present",
            "absent", "absent_reason"],
           "Daily attendance per crew. EPMS has t_attendance; the EC export "
           "carries none. Calibrated so the division-month rate reproduces "
           "ec_labour.csv exactly, including the Lebaran dip; Sundays and "
           "heavy-rain days sit below the month, weekdays above it.")
    return idx


# ── harvest orders ─────────────────────────────────────────────────────────

def _fair_day(b, terrain) -> float:
    """The generator's own difficulty rule, so headcount plans are consistent
    with the crew sizes gen_harvester_days actually used."""
    slope = (terrain.get(b["key"]) or {}).get("slope_deg") or 2.5
    months = max(len(b["bunches_by_month"]), 1)
    dens = sum(b["bunches_by_month"].values()) / max(b["ha"], 0.1) / months
    diff = (1.0 - 0.055 * (slope - 2.5) - 0.022 * (b["age"] - 10)
            + 0.10 * min(1.0, dens / 250.0))
    return bs.HARVESTER_BASE_BUNCHES * max(0.55, min(1.45, diff))


def harvester_use_by_gang_day(rot) -> dict:
    """Harvesters the worker feed records per gang per day, for the
    attendance floor. Computed before attendance so the floor exists."""
    used: dict = defaultdict(int)
    for r in _read("ec_harvester_day.csv"):
        g = rot.get(_key(r), {}).get("gang_code")
        if g:
            used[(g, r["date"])] += 1
    return dict(used)


def gen_harvest_orders(info, rot, roads, rain, att, latent, rng) -> list:
    """One order per block per harvest day, actuals from the worker feed.

    The actual is the sum of the per-worker rows on that block that day, so
    it reconciles to the client's block-month totals by construction. The
    plan is backed out through an adherence model driven by real rain, road
    condition and the gang's attendance, then rescaled so the bunch-weighted
    mean adherence is exactly HARVEST_ADHERENCE.
    """
    from gis import environment
    terrain = environment.terrain_by_block("EC")
    labour = {(r["division_code"], r["month"]): float(r["attendance_rate"])
              for r in _read("ec_labour.csv")}
    crews = {c["crew_code"]: c for c in _read("ec_crews.csv")}

    by_bd: dict = defaultdict(lambda: {"bunches": 0, "workers": 0, "md": 0.0})
    for r in _read("ec_harvester_day.csv"):
        a = by_bd[(_key(r), r["date"])]
        a["bunches"] += int(r["bunches_cut"])
        a["workers"] += 1
        a["md"] += float(r["man_days"] or 1.0)

    per_block: dict[str, list] = defaultdict(list)
    for (k, d), v in by_bd.items():
        per_block[k].append((date.fromisoformat(d), v))
    for v in per_block.values():
        v.sort(key=lambda t: t[0])

    draft = []
    for k, seq in per_block.items():
        b = info[k]
        crew_code = rot[k]["gang_code"]
        crew = crews.get(crew_code) or {}
        est = int(crew.get("establishment") or 24)
        q = latent.get(b["id"], 0.0)
        fair = _fair_day(b, terrain)
        for d, v in seq:
            rr = rain.get(d.isoformat(), 0.0)
            rain_eff = _clamp(1.0 - 0.012 * max(0.0, rr - 10.0), 0.35, 1.0)
            road_eff = _road_effect(roads.get(k), rr)
            present = att.get((crew_code, d.isoformat()))
            month_rate = labour.get((b["division"], d.strftime("%Y-%m")), 0.86)
            att_eff = (_clamp((present / est) / month_rate, 0.6, 1.1)
                       if present is not None and est else 1.0)
            noise = math.exp(rng.gauss(0.0, 0.12))
            a_raw = rain_eff * road_eff * att_eff * noise * (1.0 + 0.04 * q)
            draft.append({"k": k, "date": d, "actual": v["bunches"],
                          "workers": v["workers"], "md": v["md"], "a_raw": a_raw,
                          "rain": rr, "crew": crew_code, "fair": fair, "est": est})

    for r in draft:
        r["status"] = None
        r["carry_to"] = None

    # Rain-outs and no-shows before a cut that happened: plan-only rows with a
    # zero actual, carried to the order that did the work. The totals are
    # untouched, which is the point of rule 2. Decided before calibration so
    # the zero-actual rows count against the mean the ledger is held to.
    extra = []
    by_key_date = {(r["k"], r["date"]): r for r in draft}
    for k, seq in per_block.items():
        prev = None
        for d, _ in seq:
            r = by_key_date[(k, d)]
            gap = (d - prev).days if prev else 99
            before = d - timedelta(days=1)
            if gap > 1:
                rb = rain.get(before.isoformat(), 0.0)
                if rb >= RAIN_STOP_MM and rng.random() < 0.6:
                    extra.append({**r, "date": before, "actual": 0, "workers": 0,
                                  "md": 0.0, "a": 0.0, "status": "weathered_off",
                                  "carry_to": (k, d), "rain": rb, "src": r})
                elif ((LEBARAN[0] <= before <= LEBARAN[1] and rng.random() < 0.28)
                      or (rb < RAIN_STOP_MM and rng.random() < 0.02)):
                    extra.append({**r, "date": before, "actual": 0, "workers": 0,
                                  "md": 0.0, "a": 0.0, "status": "not_started",
                                  "carry_to": (k, d), "rain": rb, "src": r})
            prev = d

    # Rescale so sum(actual) / sum(planned) == HARVEST_ADHERENCE over the whole
    # ledger, plan-only rows included. The clip makes it a fixed point rather
    # than one division, so iterate; it converges in a handful of passes.
    tot_actual = sum(r["actual"] for r in draft)
    kf = 1.0
    for _ in range(12):
        for r in draft:
            r["a"] = _clamp(r["a_raw"] * kf, 0.2, 1.15)
            r["planned"] = int(round(r["actual"] / r["a"]))
        tot_planned = (sum(r["planned"] for r in draft)
                       + sum(e["src"]["planned"] for e in extra))
        got = tot_actual / tot_planned
        if abs(got - HARVEST_ADHERENCE) < 0.0005:
            break
        kf *= HARVEST_ADHERENCE / got
    for e in extra:
        e["planned"] = e["src"]["planned"]
        e["fair"] = e["src"]["fair"]

    rows_all = draft + extra
    rows_all.sort(key=lambda r: (r["date"], int(r["k"].split("|")[0]),
                                 int(r["k"].split("|")[1])))
    seq_by_day: dict[date, int] = defaultdict(int)
    out = []
    for r in rows_all:
        d = r["date"]
        seq_by_day[d] += 1
        oid = f"WO-{d:%Y-%m%d}-H-{seq_by_day[d]:03d}"
        b = info[r["k"]]
        status = r["status"]
        if status is None:
            if r["rain"] >= RAIN_STOP_MM and r["a"] < 0.55:
                status = "weathered_off"
            elif r["a"] >= COMPLETE_AT:
                status = "completed"
            else:
                status = "partial"
        hc_plan = max(1, int(round(r["planned"] / r["fair"])))
        if r["actual"]:
            hc_act, md_act = r["workers"], r["md"]
        elif status == "weathered_off":
            hc_act, md_act = hc_plan, round(0.3 * hc_plan, 1)
        else:
            hc_act, md_act = 0, 0.0
        start_h = 6.75 + rng.uniform(0, 0.6)
        if status == "weathered_off":
            fin_h = start_h + rng.uniform(1.5, 3.5)
        elif status == "not_started":
            fin_h = None
        else:
            fin_h = start_h + _clamp(7.2 * r["a"] + rng.uniform(-0.4, 0.4), 2.0, 8.5)
        row = {
            "order_id": oid, "date": d.isoformat(), "operation": "harvest",
            "activity": "harvest", "division_code": b["division"],
            "block_code": b["block_code"], "crew_code": r["crew"],
            "headcount_plan": hc_plan, "headcount_actual": hc_act,
            "planned_qty": r["planned"], "actual_qty": r["actual"], "unit": "bunches",
            "man_days_plan": float(hc_plan), "man_days_actual": round(md_act, 1),
            "status": status, "started": _hhmm(start_h),
            "finished": _hhmm(fin_h) if fin_h is not None else "",
            "carried_to": "",
        }
        r["oid"] = oid
        r["row"] = row
        out.append(row)

    # Chains: a plan-only row carries to the order that did the work; a
    # partial carries to the next cut on the block within five days.
    real_id = {(r["k"], r["date"]): r["oid"] for r in draft}
    for r in extra:
        r["row"]["carried_to"] = real_id.get(r["carry_to"], "")
    for k, seq in per_block.items():
        for (d1, _), (d2, _) in zip(seq, seq[1:]):
            r1 = by_key_date[(k, d1)]
            if r1["row"]["status"] == "partial" and (d2 - d1).days <= 5:
                r1["row"]["carried_to"] = real_id[(k, d2)]

    _write("ec_harvest_orders.csv", out, ORDER_HEADER,
           "Harvest work orders, one per block per cutting day. actual_qty is "
           "the sum of the per-worker rows on that block that day and "
           "reconciles to the client's REAL block-month bunch counts. The "
           "plan is generated: backed out through an adherence model driven "
           "by real daily rainfall, road condition and gang attendance, "
           f"calibrated to a {HARVEST_ADHERENCE:.0%} bunch-weighted mean. "
           "Stands in for EPMS t_harvesting_plan / t_harvester_assignment.")
    return out


# ── upkeep orders: the simulation ──────────────────────────────────────────

def gen_upkeep_orders(info, crews, territory, roads, rain, att, latent, rng):
    """Prune, weed and spray, simulated crew by crew, day by day.

    Each morning every upkeep crew and spray team picks the most overdue job
    in its territory (or continues yesterday's unfinished one), plans the day
    off the men it expected, and does what the men who came, the rain and the
    road allowed. A job not finished is carried to tomorrow; a job finished
    resets the round. The state the simulation ends in becomes ec_upkeep.csv.
    """
    up0 = _read("ec_upkeep.csv")
    labour = {(r["division_code"], r["month"]): float(r["attendance_rate"])
              for r in _read("ec_labour.csv")}

    # Pre-window state: step each block-activity's last_done back until it
    # falls before the window, keeping the phase the state feed already had.
    state = {}
    for r in up0:
        k, act = _key(r), r["activity"]
        last = date.fromisoformat(r["last_done"])
        interval = UPKEEP_INTERVALS[act]
        while last >= WINDOW_START:
            last -= timedelta(days=interval)
        state[(k, act)] = last

    def qty_of(k, act):
        b = info[k]
        return float(b["palms"]) if act == "pruning" else float(b["ha"])

    crew_by = {c["crew_code"]: c for c in crews}
    jobs_of = {}
    for c in crews:
        if c["crew_type"] == "upkeep":
            jobs_of[c["crew_code"]] = [(k, a) for k in territory[c["crew_code"]]
                                       for a in ("pruning", "circle_weeding", "path_upkeep")]
        elif c["crew_type"] == "spray":
            jobs_of[c["crew_code"]] = [(k, "spraying") for k in territory[c["crew_code"]]]

    in_progress: dict = {c: None for c in jobs_of}
    orders = []
    seq_by_day: dict[date, int] = defaultdict(int)
    realised: dict = defaultdict(list)     # (k, act) -> completion dates

    for d in _days(WINDOW_START, WINDOW_END):
        if d.weekday() == 6:
            continue
        rr = rain.get(d.isoformat(), 0.0)
        for code, jobs in jobs_of.items():
            c = crew_by[code]
            est = c["establishment"]
            present = att.get((code, d.isoformat()), 0)
            expected = max(1, int(round(est * labour.get((c["division_code"], d.strftime("%Y-%m")), 0.86))))
            job = in_progress[code]
            if job is None:
                best, best_u = None, None
                for k, act in jobs:
                    due = state[(k, act)] + timedelta(days=UPKEEP_INTERVALS[act])
                    u = (d - due).days
                    if best_u is None or u > best_u:
                        best, best_u = (k, act), u
                if best is None or best_u < -5:
                    continue
                k, act = best
                job = {"k": k, "act": act, "qty": qty_of(k, act),
                       "remaining": qty_of(k, act), "prev": None, "started": d}
            k, act = job["k"], job["act"]
            b = info[k]
            rate = RATES[act] * c["skill"]
            unit = ACTIVITY_UNIT[act]
            planned = min(job["remaining"], expected * rate)

            q = latent.get(b["id"], 0.0)
            if act == "spraying" and rr >= SPRAY_RAIN_MM:
                status, actual = "weathered_off", 0.0
            elif rr >= RAIN_STOP_MM:
                status, actual = "weathered_off", 0.0
            elif present == 0 or (present < 0.5 * expected and rng.random() < 0.5):
                status, actual = "not_started", 0.0
            else:
                a = (UPKEEP_ADHERENCE * _clamp(1.0 - 0.010 * max(0.0, rr - 8.0), 0.4, 1.0)
                     * _road_effect(roads.get(k), rr) * (1.0 + 0.05 * q)
                     * math.exp(rng.gauss(0.0, 0.22)))
                a = _clamp(a, 0.15, 1.15)
                actual = min(job["remaining"], present * rate * a)
                if job["remaining"] - actual <= 0.03 * job["qty"]:
                    actual = job["remaining"]
                    status = "completed"
                else:
                    status = "partial"

            seq_by_day[d] += 1
            oid = f"WO-{d:%Y-%m%d}-U-{seq_by_day[d]:03d}"
            hc_plan = max(1, min(expected, math.ceil(planned / rate)))
            hc_act = 0 if status == "not_started" else present
            if status in ("completed", "partial"):
                md_act = float(present)
            elif status == "weathered_off":
                md_act = round(0.3 * present, 1)
            else:
                md_act = 0.0
            start_h = 6.75 + rng.uniform(0, 0.5)
            if status == "weathered_off":
                fin_h = start_h + rng.uniform(1.0, 3.0)
            elif status == "not_started":
                fin_h = None
            else:
                share = actual / max(planned, 1e-9)
                fin_h = start_h + _clamp(7.0 * min(1.0, share) + rng.uniform(-0.3, 0.5), 1.5, 8.5)
            dec = 0 if unit == "palms" else 2
            row = {
                "order_id": oid, "date": d.isoformat(),
                "operation": ACTIVITY_OPERATION[act], "activity": act,
                "division_code": b["division"], "block_code": b["block_code"],
                "crew_code": code, "headcount_plan": hc_plan, "headcount_actual": hc_act,
                "planned_qty": round(planned, dec) if dec else int(round(planned)),
                "actual_qty": round(actual, dec) if dec else int(round(actual)),
                "unit": unit, "man_days_plan": float(hc_plan),
                "man_days_actual": round(md_act, 1), "status": status,
                "started": _hhmm(start_h),
                "finished": _hhmm(fin_h) if fin_h is not None else "",
                "carried_to": "",
            }
            orders.append(row)
            if job["prev"] is not None:
                job["prev"]["carried_to"] = oid
            job["remaining"] -= actual
            if status == "completed":
                state[(k, act)] = d
                realised[(k, act)].append(d)
                in_progress[code] = None
            else:
                job["prev"] = row
                in_progress[code] = job

    _write("ec_upkeep_orders.csv", orders, ORDER_HEADER,
           "Upkeep work orders: pruning, circle weeding, path upkeep and "
           "spraying, SIMULATED crew by crew and day by day. Each crew takes "
           "its most overdue job each morning, plans the day off expected "
           "headcount, and does what attendance, real daily rainfall and road "
           "condition allowed; the remainder carries to tomorrow. Stands in "
           "for EPMS t_workplan / t_work_assignment.")

    # Rewrite the state feed from the simulation, so the two agree.
    new_state = []
    for r in up0:
        k, act = _key(r), r["activity"]
        last = state[(k, act)]
        since = (WINDOW_END - last).days
        new_state.append({
            "division_code": r["division_code"], "block_code": r["block_code"],
            "activity": act, "last_done": last.isoformat(),
            "interval_days": UPKEEP_INTERVALS[act], "days_since": since,
            "days_overdue": max(0, since - UPKEEP_INTERVALS[act]),
        })
    _write("ec_upkeep.csv", new_state,
           ["division_code", "block_code", "activity", "last_done",
            "interval_days", "days_since", "days_overdue"],
           "Upkeep rotation state as at 2025-05-23, READ BACK from the work "
           "order ledger (ec_upkeep_orders.csv) so state and events agree. "
           "This is 'days overdue against the standard rotation', NOT block "
           "condition - EPMS records that an activity happened, never what "
           "the block looks like.")
    return orders, {"state": state, "realised": dict(realised)}


# ── pest orders ────────────────────────────────────────────────────────────

def gen_pest_orders(info, crews, att, rain, rng) -> list:
    """Census rounds, treatments and follow-ups, on the P&D teams.

    Census walks are scheduled: each team starts nine days before the round
    date and walks its division block by block at the census rate, and the
    coverage the census feed already records becomes the order's adherence.
    Treatments land on their recorded dates. Follow-ups are the finding:
    about half are done late, the rest never, and the treatment feed is
    rewritten so its days_overdue says which.
    """
    labour = {(r["division_code"], r["month"]): float(r["attendance_rate"])
              for r in _read("ec_labour.csv")}
    census = _read("ec_pest_census.csv")
    treat = _read("ec_pest_treatment.csv")
    teams = {c["division_code"]: c for c in crews if c["crew_type"] == "pest"}
    by_div: dict[str, list] = defaultdict(list)
    for b in info.values():
        by_div[b["division"]].append(b)
    for blks in by_div.values():
        blks.sort(key=lambda b: b["index"])

    orders = []
    seq_by_day: dict[date, int] = defaultdict(int)

    def emit(d, div, b, activity, planned, actual, hc_plan, hc_act, status):
        seq_by_day[d] += 1
        oid = f"WO-{d:%Y-%m%d}-P-{seq_by_day[d]:03d}"
        start_h = 6.9 + rng.uniform(0, 0.5)
        if status == "not_started":
            fin = ""
        elif status == "weathered_off":
            fin = _hhmm(start_h + rng.uniform(1.0, 2.5))
        else:
            fin = _hhmm(start_h + _clamp(7.0 * min(1.0, actual / max(planned, 1))
                                         + rng.uniform(-0.3, 0.4), 1.5, 8.3))
        row = {
            "order_id": oid, "date": d.isoformat(), "operation": "pest",
            "activity": activity, "division_code": div, "block_code": b["block_code"],
            "crew_code": teams[div]["crew_code"], "headcount_plan": hc_plan,
            "headcount_actual": hc_act, "planned_qty": int(round(planned)),
            "actual_qty": int(round(actual)), "unit": "palms",
            "man_days_plan": float(hc_plan),
            "man_days_actual": float(hc_act) if status != "not_started" else 0.0,
            "status": status, "started": _hhmm(start_h), "finished": fin,
            "carried_to": "",
        }
        orders.append(row)
        return row

    # Census walks.
    cens = {(r["round"], _key(r)): r for r in census}
    for rnd, when in CENSUS_ROUNDS:
        if not (WINDOW_START <= when <= WINDOW_END):
            continue
        for div, blks in by_div.items():
            team = teams[div]
            d = when - timedelta(days=9)
            queue = list(blks)
            while queue and d <= WINDOW_END:
                if d.weekday() == 6:
                    d += timedelta(days=1)
                    continue
                present = att.get((team["crew_code"], d.isoformat()), 0)
                if present == 0:
                    d += timedelta(days=1)
                    continue
                expected = max(1, int(round(team["establishment"]
                                            * labour.get((div, d.strftime("%Y-%m")), 0.86))))
                cap = present * RATES["census"]
                done_today, todays = 0.0, []
                while queue and (not todays or done_today + queue[0]["palms"] * 0.5 <= cap):
                    b = queue.pop(0)
                    todays.append(b)
                    done_today += b["palms"]
                tot = sum(b["palms"] for b in todays) or 1
                for b in todays:
                    c = cens.get((rnd, b["key"]))
                    inspected = int(c["palms_inspected"]) if c else int(b["palms"] * 0.8)
                    cov = inspected / max(b["palms"], 1)
                    hc = max(1, int(round(present * b["palms"] / tot)))
                    hcp = max(1, int(round(expected * b["palms"] / tot)))
                    emit(d, div, b, "census", b["palms"], inspected, hcp, hc,
                         "completed" if cov >= COMPLETE_AT else "partial")
                d += timedelta(days=1)

    # Treatments and follow-ups.
    treat_out = []
    for t in treat:
        b = info[_key(t)]
        div = b["division"]
        rate = RATES["treatment"][t["pest"]]
        palms = int(t["palms_treated"])
        td = date.fromisoformat(t["treated_date"])
        row_t = {k: v for k, v in t.items() if k != "followup_done"}
        if WINDOW_START <= td <= WINDOW_END:
            if td.weekday() == 6:
                td += timedelta(days=1)
            a = _clamp(rng.gauss(0.86, 0.12), 0.5, 1.1)
            planned = max(palms, int(round(palms / a)))
            hc = max(1, math.ceil(palms / rate))
            hcp = max(1, math.ceil(planned / rate))
            emit(td, div, b, "treatment", planned, palms, hcp, hc,
                 "completed" if palms / planned >= COMPLETE_AT else "partial")
        due = date.fromisoformat(t["followup_due"])
        row_t["followup_done"] = ""
        if WINDOW_START <= due <= WINDOW_END:
            done = rng.random() < 0.55
            when = due + timedelta(days=max(0, int(rng.gauss(7, 6)))) if done else None
            if done and when <= WINDOW_END:
                if when.weekday() == 6:
                    when += timedelta(days=1)
                rr = rain.get(when.isoformat(), 0.0)
                a = _clamp(rng.gauss(0.84, 0.14) * (0.7 if rr >= RAIN_HEAVY_MM else 1.0), 0.3, 1.1)
                actual = int(round(palms * a))
                emit(when, div, b, "followup", palms, actual,
                     max(1, math.ceil(palms / rate)), max(1, math.ceil(actual / rate)),
                     "completed" if a >= COMPLETE_AT else "partial")
                row_t["followup_done"] = when.isoformat()
                row_t["days_overdue"] = 0
            else:
                emit(due, div, b, "followup", palms, 0,
                     max(1, math.ceil(palms / rate)), 0, "not_started")
        treat_out.append(row_t)

    orders.sort(key=lambda r: (r["date"], r["order_id"]))
    _write("ec_pest_orders.csv", orders, ORDER_HEADER,
           "Pest and disease work orders: census walks, treatments and "
           "follow-ups on the P&D teams. NO EPMS table exists for any of "
           "this - the client cannot schedule pest work today because there "
           "is nowhere to record that they did. Census coverage from "
           "ec_pest_census.csv is the order's adherence; about half the "
           "follow-ups due in the window were done late and the rest never.")
    _write("ec_pest_treatment.csv", treat_out,
           ["treatment_id", "division_code", "block_code", "pest", "method",
            "treated_date", "palms_treated", "interval_days", "followup_due",
            "days_overdue", "followup_done"],
           "Treatment records and follow-up dates. Invented; nothing in EPMS "
           "records a pest treatment. followup_done and days_overdue are READ "
           "BACK from the pest work order ledger (ec_pest_orders.csv).")
    return orders


# ── dispatch orders ────────────────────────────────────────────────────────

def gen_dispatch_orders(info, blocks, latent, harvest_orders) -> list:
    """The plan side of the trip ledger: what each vehicle was sent to carry.

    Planned tonnes on a block-day are the harvest plan's bunches at the
    block's bunch weight, split across the vehicles that actually went. Actual
    is what the weighbridge weighed. The gap is therefore harvest shortfall
    AND shrinkage together, which is exactly how a transport planner sees it:
    three loads planned, two came.
    """
    abw = bs._abw_map(blocks, latent)
    vehicles = {v["vehicle_id"]: v for v in _read("ec_vehicles.csv")}
    plan_b = {(_key(o), o["date"]): o for o in harvest_orders if o["actual_qty"]}

    groups: dict = defaultdict(lambda: {"loads": 0, "net": 0.0, "bunches": 0,
                                        "depart": [], "turn": 0.0, "drivers": set(),
                                        "route": None})
    for r in _read("ec_weighbridge.csv"):
        g = groups[(r["date"], _key(r), r["vehicle_id"])]
        g["loads"] += 1
        g["net"] += float(r["net_kg"])
        g["bunches"] += int(r["bunches_epms"])
        g["depart"].append(r["depart_time"])
        g["turn"] += float(r["turnaround_h"])
        g["drivers"].add(r["driver_id"])
        g["route"] = r["route_code"]

    day_bunches: dict = defaultdict(int)
    for (d, k, _), g in groups.items():
        day_bunches[(d, k)] += g["bunches"]

    out = []
    seq_by_day: dict[str, int] = defaultdict(int)
    for (d, k, vid), g in sorted(groups.items()):
        b = info[k]
        w = abw.get(k) or 8.0
        cap = float(vehicles.get(vid, {}).get("capacity_t") or 4.5)
        ho = plan_b.get((k, d))
        planned_bunches = int(ho["planned_qty"]) if ho else g["bunches"]
        share = g["bunches"] / max(day_bunches[(d, k)], 1)
        planned_t = planned_bunches * share * w / 1000.0
        actual_t = g["net"] / 1000.0
        loads_plan = max(1, math.ceil(planned_t / cap))
        ratio = actual_t / planned_t if planned_t else 1.0
        seq_by_day[d] += 1
        dep = sorted(g["depart"])
        h1 = int(dep[-1][:2]) + int(dep[-1][3:]) / 60.0 + g["turn"] / g["loads"]
        out.append({
            "order_id": f"WO-{d[:4]}-{d[5:7]}{d[8:]}-D-{seq_by_day[d]:03d}",
            "date": d, "operation": "dispatch", "activity": "evacuation",
            "division_code": b["division"], "block_code": b["block_code"],
            "crew_code": vid, "headcount_plan": loads_plan, "headcount_actual": g["loads"],
            "planned_qty": round(planned_t, 2), "actual_qty": round(actual_t, 2),
            "unit": "tonnes",
            "man_days_plan": round(loads_plan * (g["turn"] / g["loads"]) / 8.0, 2),
            "man_days_actual": round(g["turn"] / 8.0, 2),
            "status": "completed" if ratio >= COMPLETE_AT else "partial",
            "started": dep[0], "finished": _hhmm(h1), "carried_to": "",
            "driver_id": "/".join(sorted(g["drivers"])), "route_code": g["route"],
        })
    _write("ec_dispatch_orders.csv", out, ORDER_HEADER + ["driver_id", "route_code"],
           "Dispatch orders: the plan side of the trip ledger. Planned tonnes "
           "are the harvest plan's bunches at the block's bunch weight, split "
           "across the vehicles that went; actual is what the weighbridge "
           "weighed (ec_weighbridge.csv), so the gap is harvest shortfall and "
           "shrinkage together. Stands in for the SAP PM vehicle trip plan.")
    return out


# ── entry point ────────────────────────────────────────────────────────────

def build() -> dict:
    from gis import ontology
    blocks = ontology.blocks_geojson("EC", synthetic_world=True)["features"]
    # The same latent field every other feed was drawn against: it is the
    # first thing build_synthetic.build() draws from the seeded generator.
    latent = _latent_field(blocks, random.Random(SEED))
    rng = random.Random(SEED + 7)

    info = _block_info(blocks)
    rot = {_key(r): r for r in _read("ec_rotation.csv")}
    roads = {_key(r): r["condition"] for r in _read("ec_roads.csv")}
    rain, rain_prov = _daily_rain()

    crews, territory = gen_crews(info, rot, rng, rain)
    used = harvester_use_by_gang_day(rot)
    att = gen_attendance(crews, rain, used, rng)
    harvest = gen_harvest_orders(info, rot, roads, rain, att, latent, rng)
    upkeep, upstate = gen_upkeep_orders(info, crews, territory, roads, rain, att, latent, rng)
    pest = gen_pest_orders(info, crews, att, rain, rng)
    dispatch = gen_dispatch_orders(info, blocks, latent, harvest)

    checks = _checks(blocks, harvest, upkeep, pest, dispatch, upstate)
    checks["rain_provenance"] = rain_prov
    log.info("[operations] crews %d, attendance rows %d, orders H %d U %d P %d D %d",
             len(crews), len(att), len(harvest), len(upkeep), len(pest), len(dispatch))
    for k, v in checks.items():
        log.info("[operations] check %s: %s", k, v)
    return {"crews": len(crews), "attendance": len(att), "harvest_orders": len(harvest),
            "upkeep_orders": len(upkeep), "pest_orders": len(pest),
            "dispatch_orders": len(dispatch), "checks": checks}


def _checks(blocks, harvest, upkeep, pest, dispatch, upstate) -> dict:
    """The calibration constraints, measured rather than asserted."""
    real = sum(f["properties"]["bunches_total"] or 0 for f in blocks)
    ledger = sum(int(r["actual_qty"]) for r in harvest)

    labour = {(r["division_code"], r["month"]): float(r["attendance_rate"])
              for r in _read("ec_labour.csv")}
    agg: dict = defaultdict(lambda: [0, 0])
    for r in _read("ec_attendance.csv"):
        a = agg[(r["division_code"], r["date"][:7])]
        a[0] += int(r["present"])
        a[1] += int(r["on_roll"])
    worst = max(abs(a[0] / a[1] - labour[k]) for k, a in agg.items() if k in labour)

    def adherence(rows):
        p = sum(float(r["planned_qty"]) for r in rows)
        a = sum(float(r["actual_qty"]) for r in rows)
        return round(a / p, 4) if p else None

    chains = sum(1 for r in harvest + upkeep + pest if r["carried_to"])
    overdue = sum(1 for (k, act), last in upstate["state"].items()
                  if (WINDOW_END - last).days > UPKEEP_INTERVALS[act])
    return {
        "harvest_reconciles": ledger == real,
        "harvest_ledger_bunches": ledger, "real_bunches": real,
        "attendance_max_abs_error": round(worst, 4),
        "harvest_adherence": adherence(harvest),
        "upkeep_adherence": adherence(upkeep),
        "pest_adherence": adherence(pest),
        "dispatch_adherence": adherence(dispatch),
        "carried_forward": chains,
        "upkeep_block_activities_overdue": overdue,
        "of_block_activities": len(upstate["state"]),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    m = build()
    for k, v in m.items():
        if k != "checks":
            print(f"  {k:16s} {v}")
    print("  checks:")
    for k, v in m["checks"].items():
        print(f"    {k:34s} {v}")
    print(f"  -> {OUT}")
