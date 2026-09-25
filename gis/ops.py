"""The operations layer: the work-order ledger and what it says.

Reads the six feeds gis/build_operations.py writes and answers four
questions the rest of the build could not:

    ledger      what was worked, when, by whom, planned against actual
    adherence   actual over planned, by crew, block, division, month, driver
    capacity    who is on the roll, who is expected tomorrow, what they can do
    demand      what is due, how urgent, what deferring it costs

The scheduler in gis/models/scheduler.py reads capacity and demand and
writes nothing here. This module is a join, like gis/layers.py: it imports
cheaply, never fits anything, and serves a request in milliseconds.

Everything priced reads the assumption register (gis/assumptions.py) through
assumptions.values(), never a module constant, so an edit in the panel
reprices the next answer.

Provenance vocabulary, carried on every payload: the ledger is SYNTHETIC.
Inside it, the harvest actuals sum to the client's real bunch counts, the
rainfall on every row is real, and block identity and area are real. The
plan side, the crews and the attendance are generated.
"""

import csv
import logging
import math
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

from gis import assumptions, environment, layers, ontology
from gis.build_operations import (ACTIVITY_OPERATION, ACTIVITY_UNIT, COMPLETE_AT,
                                  TOMORROW, WINDOW_END, WINDOW_START)
from gis.build_synthetic import UPKEEP_INTERVALS

log = logging.getLogger("estate-command.ops")

_DIR = Path(__file__).parent / "data" / "synthetic"
_CACHE: dict = {}
_LOCK = Lock()

# One row shape, six operations. `activities` are the values the ledger's
# activity column takes for that operation; `crew_type` is who does it.
OPERATIONS = OrderedDict([
    ("harvest", {
        "label": "Harvesting", "domain": "harvesting", "unit": "bunches",
        "crew_type": "harvest", "crew_label": "gang", "file": "ec_harvest_orders.csv",
        "activities": ["harvest"],
        "stands_in_for": "EPMS t_harvesting_plan / t_harvester_assignment",
        "epms_table": "log_harvesting_plan_approval",
        "demand": "ripeness pressure: days since the last cut over the block's own round",
        "rate": "adjusted target per block from the productivity model, 97-187 bunches per man-day",
    }),
    ("prune", {
        "label": "Pruning", "domain": "upkeep", "unit": "palms",
        "crew_type": "upkeep", "crew_label": "crew", "file": "ec_upkeep_orders.csv",
        "activities": ["pruning"],
        "stands_in_for": "EPMS t_workplan / t_work_assignment",
        "epms_table": "log_workplan_approval",
        "demand": "days overdue against the 240-day pruning round",
        "rate": "palms per man-day, from the assumption register",
    }),
    ("weed", {
        "label": "Weeding", "domain": "upkeep", "unit": "ha",
        "crew_type": "upkeep", "crew_label": "crew", "file": "ec_upkeep_orders.csv",
        "activities": ["circle_weeding", "path_upkeep"],
        "stands_in_for": "EPMS t_workplan / t_work_assignment",
        "epms_table": "log_workplan_approval",
        "demand": "days overdue against the 75-day circle and 110-day path rounds",
        "rate": "hectares per man-day, from the assumption register",
    }),
    ("spray", {
        "label": "Spraying", "domain": "upkeep", "unit": "ha",
        "crew_type": "spray", "crew_label": "team", "file": "ec_upkeep_orders.csv",
        "activities": ["spraying"],
        "stands_in_for": "EPMS t_workplan / t_work_assignment",
        "epms_table": "log_workplan_approval",
        "demand": "days overdue against the 100-day spray round",
        "rate": "hectares per man-day, from the assumption register",
    }),
    ("pest", {
        "label": "Pest control", "domain": "pest", "unit": "palms",
        "crew_type": "pest", "crew_label": "team", "file": "ec_pest_orders.csv",
        "activities": ["census", "treatment", "followup"],
        "stands_in_for": "nothing: no EPMS table exists",
        "epms_table": None,
        "demand": "follow-ups overdue, and infected blocks with no treatment on record",
        "rate": "palms per man-day by method, from the assumption register",
    }),
    ("dispatch", {
        "label": "Transport", "domain": "transport", "unit": "tonnes",
        "crew_type": "transport", "crew_label": "vehicle", "file": "ec_dispatch_orders.csv",
        "activities": ["evacuation"],
        "stands_in_for": "SAP PM vehicle trip plan",
        "epms_table": None,
        "demand": "tonnes at the collection points from tomorrow's harvest plan",
        "rate": "loads per vehicle per day, from turnaround and working hours",
    }),
])

STATUSES = ("completed", "partial", "not_started", "weathered_off")

_RAIN_BUCKETS = (("dry, under 5 mm", 0, 5), ("5-25 mm", 5, 25),
                 ("25-45 mm", 25, 45), ("over 45 mm", 45, 10 ** 6))
_ATT_BUCKETS = (("under 70% present", 0, 0.70), ("70-85% present", 0.70, 0.85),
                ("85% and over", 0.85, 2.0))
_ROAD_ORDER = ("good", "fair", "poor", "impassable-when-wet")


# ── loading ────────────────────────────────────────────────────────────────

def _read(name: str) -> list[dict]:
    path = _DIR / name
    if not path.exists():
        log.warning("[ops] %s missing - run gis/build_operations.py", path)
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    return list(csv.DictReader(lines))


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


def _key(division, block) -> str:
    return f"{int(str(division).strip())}|{int(str(block).strip())}"


def _blocks(estate: str = "EC") -> dict:
    # The ledger lives in the synthetic world: its rates divide bunches by that
    # window's length, so it reads the harvest cut at the window's end.
    geo = ontology.blocks_geojson(estate, synthetic_world=True)
    out = {}
    for i, f in enumerate((geo or {}).get("features") or []):
        p = f["properties"]
        ring = f["geometry"]["coordinates"][0]
        n = len(ring) - 1
        out[_key(p["division_code"], p["block_code"])] = {
            "index": i, "id": f["id"], "label": p.get("block_label"),
            "division": str(int(p["division_code"])), "block_code": p["block_code"],
            "ha": p.get("planted_ha") or 0.0, "palms": p.get("palms") or 0,
            "bunches_total": p.get("bunches_total") or 0,
            "centroid": (sum(x[0] for x in ring[:n]) / n, sum(x[1] for x in ring[:n]) / n),
            "ring": ring,
        }
    return out


def _state() -> dict:
    with _LOCK:
        if "state" in _CACHE:
            return _CACHE["state"]
        blocks = _blocks()
        rain = environment.rainfall_by_day("EC")
        roads = {_key(r["division_code"], r["block_code"]): r["condition"]
                 for r in layers._read("ec_roads.csv")}
        abw = {_key(r["division_code"], r["block_code"]): _f(r["abw_kg"])
               for r in layers._read("ec_abw.csv")}
        rot = {_key(r["division_code"], r["block_code"]): {
            "gang_code": r["gang_code"], "target": _i(r["rotation_target_days"]),
            "last_harvest_date": r["last_harvest_date"],
            "days_since": _i(r["days_since_harvest"])}
            for r in layers._read("ec_rotation.csv")}

        crews = []
        for r in _read("ec_crews.csv"):
            crews.append({
                "crew_code": r["crew_code"], "crew_type": r["crew_type"],
                "name": r["name"], "division_code": r["division_code"],
                "establishment": _i(r["establishment"]) or 0,
                "harvesters": _i(r["harvesters"]),
                "blocks": _i(r["blocks"]), "home_block": r["home_block"] or None,
                "home": (_f(r["home_lon"]), _f(r["home_lat"])),
            })
        crew_by = {c["crew_code"]: c for c in crews}

        att: dict = {}
        att_series: dict = defaultdict(list)
        for r in _read("ec_attendance.csv"):
            row = {"on_roll": _i(r["on_roll"]) or 0, "present": _i(r["present"]) or 0,
                   "absent": _i(r["absent"]) or 0, "reason": r["absent_reason"]}
            att[(r["crew_code"], r["date"])] = row
            att_series[r["crew_code"]].append((r["date"], row))
        for v in att_series.values():
            v.sort()

        orders: dict[str, list] = {op: [] for op in OPERATIONS}
        seen_files = set()
        for op, meta in OPERATIONS.items():
            if meta["file"] in seen_files:
                continue
            seen_files.add(meta["file"])
            for r in _read(meta["file"]):
                k = _key(r["division_code"], r["block_code"])
                b = blocks.get(k) or {}
                planned = _f(r["planned_qty"], 0.0)
                actual = _f(r["actual_qty"], 0.0)
                md_act = _f(r["man_days_actual"], 0.0)
                crew = crew_by.get(r["crew_code"]) or {}
                a = att.get((r["crew_code"], r["date"]))
                d = date.fromisoformat(r["date"])
                row = {
                    "order_id": r["order_id"], "date": r["date"],
                    "operation": r["operation"], "activity": r.get("activity") or r["operation"],
                    "division_code": str(int(r["division_code"])),
                    "block_code": r["block_code"], "block_key": k,
                    "block_id": b.get("id"), "block_label": b.get("label"),
                    "planted_ha": b.get("ha"), "palms": b.get("palms"),
                    "crew_code": r["crew_code"], "crew_type": crew.get("crew_type"),
                    "headcount_plan": _i(r["headcount_plan"]) or 0,
                    "headcount_actual": _i(r["headcount_actual"]) or 0,
                    "planned_qty": planned, "actual_qty": actual, "unit": r["unit"],
                    "man_days_plan": _f(r["man_days_plan"], 0.0),
                    "man_days_actual": md_act,
                    "status": r["status"], "started": r["started"] or None,
                    "finished": r["finished"] or None,
                    "carried_to": r["carried_to"] or None,
                    "adherence": round(actual / planned, 4) if planned else None,
                    "output_per_man_day": round(actual / md_act, 2) if md_act else None,
                    "rain_mm": rain.get(r["date"]),
                    "road_condition": roads.get(k),
                    "attendance": (round(a["present"] / a["on_roll"], 3)
                                   if a and a["on_roll"] else None),
                    "week": (d - timedelta(days=d.weekday())).isoformat(),
                    "month": r["date"][:7],
                    "driver_id": r.get("driver_id"), "route_code": r.get("route_code"),
                }
                op_key = row["operation"] if row["operation"] in orders else op
                orders[op_key].append(row)
        for v in orders.values():
            v.sort(key=lambda r: (r["date"], r["order_id"]))

        upkeep_state = {}
        for r in layers._read("ec_upkeep.csv"):
            upkeep_state[(_key(r["division_code"], r["block_code"]), r["activity"])] = {
                "last_done": r["last_done"], "interval": _i(r["interval_days"]),
                "days_since": _i(r["days_since"]), "days_overdue": _i(r["days_overdue"]),
            }
        treatments: dict = defaultdict(list)
        for r in layers._read("ec_pest_treatment.csv"):
            treatments[_key(r["division_code"], r["block_code"])].append({
                "treatment_id": r["treatment_id"], "pest": r["pest"], "method": r["method"],
                "treated_date": r["treated_date"], "palms_treated": _i(r["palms_treated"]) or 0,
                "interval_days": _i(r["interval_days"]) or 90,
                "followup_due": r["followup_due"],
                "followup_done": r.get("followup_done") or None,
            })
        vehicles = [{
            "vehicle_id": v["vehicle_id"], "vehicle_class": v["vehicle_class"],
            "capacity_t": _f(v["capacity_t"], 4.5),
            "failures_per_1000_trips": _f(v["failures_per_1000_trips"], 0.0),
        } for v in layers._read("ec_vehicles.csv")]

        _CACHE["state"] = {
            "blocks": blocks, "rain": rain, "roads": roads, "abw": abw, "rot": rot,
            "crews": crews, "crew_by": crew_by, "att": att, "att_series": dict(att_series),
            "orders": orders, "upkeep_state": upkeep_state,
            "treatments": dict(treatments), "vehicles": vehicles,
        }
        log.info("[ops] loaded: %d crews, %d attendance rows, orders %s",
                 len(crews), len(att), {k: len(v) for k, v in orders.items()})
        return _CACHE["state"]


def reload_ops() -> None:
    with _LOCK:
        _CACHE.clear()


def meta(operation: str) -> dict | None:
    return OPERATIONS.get(operation)


def window() -> dict:
    return {"from": WINDOW_START.isoformat(), "to": WINDOW_END.isoformat(),
            "days": (WINDOW_END - WINDOW_START).days + 1,
            "tomorrow": TOMORROW.isoformat(),
            "note": ("The real harvest export ends 2025-05-23. The ledger covers "
                     "exactly that window; tomorrow is 2025-05-24, the first day "
                     "beyond the client's own records.")}


def _provenance(operation: str) -> str:
    if operation == "harvest":
        return ("synthetic ledger. Actual bunches per order sum to the client's REAL "
                "block-month counts; rainfall on each row is REAL (Open-Meteo); block "
                "identity and area are REAL. The plan, the gang and the attendance are "
                "generated.")
    if operation == "dispatch":
        return ("synthetic ledger. Planned tonnes rest on a calibrated bunch weight; "
                "weighed tonnes are from the generated trip ledger. Block identity, "
                "area and rainfall are REAL.")
    if operation == "pest":
        return ("synthetic ledger, and nothing in EPMS could hold it: no table records "
                "a pest treatment. Block identity, area and rainfall are REAL.")
    return ("synthetic ledger, simulated day by day against REAL daily rainfall and "
            "REAL block areas and palm counts. The crews, the rounds and the "
            "attendance are generated.")


# ── the ledger ─────────────────────────────────────────────────────────────

def _agg(rows: list[dict]) -> dict:
    planned = sum(r["planned_qty"] for r in rows)
    actual = sum(r["actual_qty"] for r in rows)
    md_p = sum(r["man_days_plan"] for r in rows)
    md_a = sum(r["man_days_actual"] for r in rows)
    return {
        "orders": len(rows),
        "planned_qty": round(planned, 1), "actual_qty": round(actual, 1),
        "adherence_pct": round(100 * actual / planned, 1) if planned else None,
        "man_days_plan": round(md_p, 1), "man_days_actual": round(md_a, 1),
        "output_per_man_day": round(actual / md_a, 1) if md_a else None,
        "planned_per_man_day": round(planned / md_p, 1) if md_p else None,
        "carried_forward": sum(1 for r in rows if r["carried_to"]),
        **{s: sum(1 for r in rows if r["status"] == s) for s in STATUSES},
    }


def _bucket(v, buckets):
    if v is None:
        return None
    for label, lo, hi in buckets:
        if lo <= v < hi:
            return label
    return buckets[-1][0]


def _drivers(rows: list[dict]) -> dict:
    """Adherence grouped by the thing that drove the miss.

    This table is what the generated ledger exists to teach. If adherence
    were flat across rain, road and attendance, a plan could ignore all three.
    """
    def group(keyfn, order):
        g: dict = defaultdict(list)
        for r in rows:
            k = keyfn(r)
            if k is not None:
                g[k].append(r)
        out = []
        for k in order:
            if k in g:
                a = _agg(g[k])
                out.append({"bucket": k, "orders": a["orders"],
                            "planned_qty": a["planned_qty"], "actual_qty": a["actual_qty"],
                            "adherence_pct": a["adherence_pct"],
                            "weathered_off": a["weathered_off"]})
        return out

    return {
        "rain": group(lambda r: _bucket(r["rain_mm"], _RAIN_BUCKETS),
                      [b[0] for b in _RAIN_BUCKETS]),
        "road": group(lambda r: r["road_condition"], _ROAD_ORDER),
        "attendance": group(lambda r: _bucket(r["attendance"], _ATT_BUCKETS),
                            [b[0] for b in _ATT_BUCKETS]),
        "note": ("Rainfall on the day is real. Road condition and attendance are "
                 "generated feeds. A miss that lines up with all three is the "
                 "ledger behaving like an estate; the plan reads these buckets to "
                 "set tomorrow's expected adherence."),
    }


def _chains(rows: list[dict]) -> dict:
    by_id = {r["order_id"]: r for r in rows}
    nxt = {r["order_id"]: r["carried_to"] for r in rows if r["carried_to"] in by_id}
    heads = set(nxt) - set(nxt.values())
    chains = []
    for h in heads:
        ids, c = [h], h
        while c in nxt and nxt[c] not in ids:
            c = nxt[c]
            ids.append(c)
        first, last = by_id[ids[0]], by_id[ids[-1]]
        chains.append({
            "orders": len(ids),
            "days": (date.fromisoformat(last["date"]) - date.fromisoformat(first["date"])).days + 1,
            "block_label": first["block_label"], "crew_code": first["crew_code"],
            "activity": first["activity"], "from": first["date"], "to": last["date"],
            "first_order": ids[0], "last_order": ids[-1],
        })
    chains.sort(key=lambda c: (-c["days"], -c["orders"]))
    return {
        "chained_orders": len(nxt),
        "chains": len(chains),
        "mean_chain_days": round(sum(c["days"] for c in chains) / len(chains), 1) if chains else None,
        "longest": chains[:5],
        "note": ("A partial order names the order it became. The chain is how a "
                 "round stretches without anyone deciding it should."),
    }


def _round_stretch(operation: str, rows: list[dict], st: dict) -> dict | None:
    """Realised interval between completions, month by month, against target.

    For harvest the target is the block's own early-year round; for upkeep it
    is the standard interval. The stretch is the finding: a 5-day round that
    reads 7.7 by May in the client's own day counts.
    """
    if operation in ("pest", "dispatch"):
        return None
    # Harvest: every cut is a completion of the round. Upkeep: only a completed
    # order closes a round; the days inside a chain are the same job.
    done = [r for r in rows if (r["actual_qty"] > 0 if operation == "harvest"
                                else r["status"] == "completed")]
    by_block: dict = defaultdict(list)
    for r in done:
        by_block[(r["block_key"], r["activity"])].append(date.fromisoformat(r["date"]))
    gaps_by_month: dict = defaultdict(list)
    for (k, act), ds in by_block.items():
        ds = sorted(set(ds))
        for a, b in zip(ds, ds[1:]):
            gap = (b - a).days
            if gap > 0:
                gaps_by_month[b.strftime("%Y-%m")].append(gap)
    if not gaps_by_month:
        return None
    if operation == "harvest":
        targets = [v["target"] for v in st["rot"].values() if v.get("target")]
        target = sorted(targets)[len(targets) // 2] if targets else None
    else:
        acts = OPERATIONS[operation]["activities"]
        target = round(sum(UPKEEP_INTERVALS[a] for a in acts) / len(acts))
    months = []
    for m in sorted(gaps_by_month):
        g = sorted(gaps_by_month[m])
        months.append({"month": m, "median_interval_days": g[len(g) // 2],
                       "intervals": len(g)})
    first, last = months[0], months[-1]
    return {
        "target_days": target,
        "by_month": months,
        "stretch_days": last["median_interval_days"] - first["median_interval_days"],
        "reading": (f"median realised interval {first['median_interval_days']} days in "
                    f"{first['month']}, {last['median_interval_days']} in {last['month']}, "
                    f"against a {target}-day target."),
    }


def ledger(operation: str, crew: str | None = None, block: str | None = None,
           division: str | None = None, date_from: str | None = None,
           date_to: str | None = None, status: str | None = None,
           activity: str | None = None, limit: int = 150, offset: int = 0) -> dict:
    m = OPERATIONS.get(operation)
    if not m:
        return {"available": False, "reason": f"No operation {operation!r}.",
                "available_operations": list(OPERATIONS)}
    st = _state()
    rows = st["orders"].get(operation) or []
    if not rows:
        return {"available": False,
                "reason": "No work-order ledger. Run python gis/build_operations.py."}

    def keep(r):
        if crew and r["crew_code"] != crew:
            return False
        if block and str(r["block_label"]) != str(block) and r["block_key"] != block:
            return False
        if division and r["division_code"] != str(int(division)):
            return False
        if date_from and r["date"] < date_from:
            return False
        if date_to and r["date"] > date_to:
            return False
        if status and r["status"] != status:
            return False
        if activity and r["activity"] != activity:
            return False
        return True

    picked = [r for r in rows if keep(r)]
    totals = _agg(picked)
    totals["days"] = len({r["date"] for r in picked})
    totals["crews"] = len({r["crew_code"] for r in picked})
    totals["blocks"] = len({r["block_key"] for r in picked})
    totals["unit"] = m["unit"]
    if operation == "harvest" and picked:
        abw = st["abw"]
        t_p = sum(r["planned_qty"] * (abw.get(r["block_key"]) or 0) for r in picked) / 1000
        t_a = sum(r["actual_qty"] * (abw.get(r["block_key"]) or 0) for r in picked) / 1000
        totals["planned_tonnes"] = round(t_p, 1)
        totals["actual_tonnes"] = round(t_a, 1)
        totals["tonnes_short"] = round(t_p - t_a, 1)

    by_week: dict = defaultdict(list)
    for r in picked:
        by_week[r["week"]].append(r)
    weeks = []
    for w in sorted(by_week):
        a = _agg(by_week[w])
        days = {r["date"] for r in by_week[w]}
        weeks.append({"week": w, "orders": a["orders"], "planned_qty": a["planned_qty"],
                      "actual_qty": a["actual_qty"], "adherence_pct": a["adherence_pct"],
                      "weathered_off": a["weathered_off"], "carried_forward": a["carried_forward"],
                      "rain_mm": round(sum(st["rain"].get(d, 0.0) for d in days), 1)})

    by_crew: dict = defaultdict(list)
    for r in picked:
        by_crew[r["crew_code"]].append(r)
    crews = []
    for c, rs in by_crew.items():
        a = _agg(rs)
        crews.append({"crew_code": c, "crew_type": rs[0]["crew_type"],
                      "division_code": rs[0]["division_code"], **a,
                      "blocks": len({r["block_key"] for r in rs}),
                      "days": len({r["date"] for r in rs})})
    crews.sort(key=lambda c: (c["adherence_pct"] if c["adherence_pct"] is not None else 999))

    by_activity = []
    if len(m["activities"]) > 1:
        for act in m["activities"]:
            rs = [r for r in picked if r["activity"] == act]
            if rs:
                by_activity.append({"activity": act, **_agg(rs)})

    recent = sorted(picked, key=lambda r: (r["date"], r["order_id"]), reverse=True)
    page = recent[offset:offset + max(1, min(limit, 500))]
    fields = ("order_id", "date", "crew_code", "block_label", "block_id", "division_code",
              "activity", "planned_qty", "actual_qty", "unit", "adherence",
              "headcount_plan", "headcount_actual", "man_days_plan", "man_days_actual",
              "output_per_man_day", "status", "started", "finished", "carried_to",
              "rain_mm", "road_condition", "attendance", "driver_id")
    return {
        "available": True,
        "operation": operation, "label": m["label"], "unit": m["unit"],
        "stands_in_for": m["stands_in_for"],
        "window": window(),
        "filters": {"crew": crew, "block": block, "division": division,
                    "date_from": date_from, "date_to": date_to, "status": status,
                    "activity": activity},
        "matched": len(picked), "offset": offset, "limit": limit,
        "totals": totals,
        "footer": _footer(totals, m["unit"]),
        "by_week": weeks,
        "by_crew": crews,
        "by_activity": by_activity,
        "drivers": _drivers(picked),
        "slippage": {**_chains(picked), "round": _round_stretch(operation, picked, st)},
        "rows": [{f: r.get(f) for f in fields} for r in page],
        "complete_at": COMPLETE_AT,
        "statuses": list(STATUSES),
        "provenance": _provenance(operation),
        "note": (f"An order is completed at {COMPLETE_AT:.0%} of plan and carried below "
                 f"it. Adherence is actual over planned {m['unit']}, summed, not a mean "
                 "of ratios."),
    }


def _footer(t: dict, unit: str) -> str:
    adh = f"{t['adherence_pct']}% adherence" if t.get("adherence_pct") is not None else "no plan"
    return (f"{t['days']} days, {t['orders']:,} orders, {adh}, "
            f"{t['carried_forward']} carried forward, {t['weathered_off']} weathered off.")


# ── adherence ──────────────────────────────────────────────────────────────

def adherence(operation: str, top: int = 12) -> dict:
    m = OPERATIONS.get(operation)
    if not m:
        return {"available": False, "reason": f"No operation {operation!r}."}
    st = _state()
    rows = st["orders"].get(operation) or []
    if not rows:
        return {"available": False, "reason": "No work-order ledger."}

    def grouped(keyfn, min_orders=1):
        g: dict = defaultdict(list)
        for r in rows:
            g[keyfn(r)].append(r)
        out = []
        for k, rs in g.items():
            if len(rs) < min_orders:
                continue
            a = _agg(rs)
            out.append({"key": k, **a})
        return out

    by_crew = grouped(lambda r: r["crew_code"])
    for c in by_crew:
        c["crew_code"] = c.pop("key")
        c["division_code"] = st["crew_by"].get(c["crew_code"], {}).get("division_code")
    by_crew.sort(key=lambda c: c["adherence_pct"] if c["adherence_pct"] is not None else 999)

    by_block = grouped(lambda r: r["block_key"], min_orders=3)
    for b in by_block:
        k = b.pop("key")
        blk = st["blocks"].get(k) or {}
        b.update({"block_key": k, "block_label": blk.get("label"), "block_id": blk.get("id"),
                  "division_code": blk.get("division"), "planted_ha": blk.get("ha"),
                  "road_condition": st["roads"].get(k)})
    by_block.sort(key=lambda b: b["adherence_pct"] if b["adherence_pct"] is not None else 999)

    by_division = grouped(lambda r: r["division_code"])
    for d in by_division:
        d["division_code"] = d.pop("key")
    by_division.sort(key=lambda d: int(d["division_code"]))

    by_month = grouped(lambda r: r["month"])
    for d in by_month:
        d["month"] = d.pop("key")
    by_month.sort(key=lambda d: d["month"])

    tot = _agg(rows)
    return {
        "available": True, "operation": operation, "label": m["label"], "unit": m["unit"],
        "totals": tot,
        "by_crew": by_crew,
        "worst_blocks": by_block[:top],
        "best_blocks": list(reversed(by_block[-top:])) if by_block else [],
        "by_division": by_division,
        "by_month": by_month,
        "drivers": _drivers(rows),
        "spread": {"best_crew_pct": by_crew[-1]["adherence_pct"] if by_crew else None,
                   "worst_crew_pct": by_crew[0]["adherence_pct"] if by_crew else None},
        "provenance": _provenance(operation),
        "note": ("Adherence is measured, not assumed: every order carries what was "
                 "planned and what was done. Blocks need three orders to be ranked."),
    }


# ── capacity ───────────────────────────────────────────────────────────────

def _expected_present(st: dict, crew_code: str, on: date, lookback: int,
                      use_model: bool = False) -> dict:
    """Tomorrow's headcount for one crew.

    With the headcount forecast on (gis/models/headcount.py), every date is
    planned on the forecast: a most likely figure and a range, from what was
    knowable the evening before. On a replay the men who actually came are
    carried beside it, never used to plan. Off, the old behaviour: the crew's
    trailing attendance applied to its roll, or the recorded attendance on a
    date inside the ledger.
    """
    c = st["crew_by"].get(crew_code) or {}
    roll = c.get("establishment") or 0
    rec = st["att"].get((crew_code, on.isoformat()))
    series = st["att_series"].get(crew_code) or []
    upto = min(on - timedelta(days=1), WINDOW_END).isoformat()
    tail = [v for d, v in series if d <= upto][-lookback:]
    rate = (sum(v["present"] for v in tail) / sum(v["on_roll"] for v in tail)) if tail else 0.85
    if use_model:
        from gis.models import headcount
        h = headcount.expected_present(crew_code, on)
        if h:
            return {"on_roll": h["on_roll"], "present": h["most_likely"],
                    "attendance_pct": h["turnout_pct"],
                    "basis": f"headcount forecast: likely {h['low']} to {h['high']}",
                    "trailing_attendance_pct": round(100 * rate, 1),
                    "present_low": h["low"], "present_high": h["high"],
                    "model_present": h["expected"], "drivers": h["drivers"],
                    "recorded_present": rec["present"] if rec else None,
                    "present_source": "forecast"}
    if rec:
        return {"on_roll": rec["on_roll"], "present": rec["present"],
                "attendance_pct": round(100 * rec["present"] / rec["on_roll"], 1) if rec["on_roll"] else None,
                "basis": "recorded attendance on the day",
                "trailing_attendance_pct": round(100 * rate, 1),
                "recorded_present": rec["present"], "present_source": "recorded"}
    return {"on_roll": roll, "present": int(round(roll * rate)),
            "attendance_pct": round(100 * rate, 1),
            "basis": f"mean attendance over the trailing {lookback} days, applied to the roll",
            "trailing_attendance_pct": round(100 * rate, 1), "present_source": "average"}


def _headcount_in_use(av: dict) -> bool:
    if int(av.get("use_headcount_model", 0)) != 1:
        return False
    from gis.models import headcount
    return bool(headcount.backtest().get("grade", {}).get("passes"))


def _rate_for(crew_type: str, av: dict) -> tuple[float, str]:
    if crew_type == "harvest":
        return av["harvest_bunches_per_man_day"], "bunches"
    if crew_type == "upkeep":
        return av["circle_weed_ha_per_man_day"], "ha (weeding)"
    if crew_type == "spray":
        return av["spray_ha_per_man_day"], "ha"
    if crew_type == "pest":
        return av["pest_census_palms_per_man_day"], "palms (census)"
    return 0.0, ""


def _vehicle_turnaround(st: dict) -> dict:
    """Mean turnaround per vehicle from the dispatch ledger, in hours."""
    tot: dict = defaultdict(lambda: [0.0, 0])
    for r in st["orders"].get("dispatch") or []:
        t = tot[r["crew_code"]]
        t[0] += r["man_days_actual"] * 8.0
        t[1] += r["headcount_actual"]
    return {v: (t[0] / t[1] if t[1] else 2.5) for v, t in tot.items()}


def _vehicle_daily_loads(st: dict) -> dict:
    """The 80th percentile of loads a vehicle actually ran in a day.

    The trip ledger already shows what the fleet moves on a busy day, and a
    plan that credits a truck with fewer loads than it demonstrably runs
    would leave fruit at the platform on paper that was hauled in fact.
    """
    per_day: dict = defaultdict(lambda: defaultdict(int))
    for r in st["orders"].get("dispatch") or []:
        per_day[r["crew_code"]][r["date"]] += r["headcount_actual"]
    out = {}
    for v, days in per_day.items():
        vals = sorted(days.values())
        out[v] = vals[int(0.8 * (len(vals) - 1))] if vals else 0
    return out


def capacity(on: str | None = None, crew_type: str | None = None) -> dict:
    st = _state()
    if not st["crews"]:
        return {"available": False, "reason": "No crew feed. Run python gis/build_operations.py."}
    d = date.fromisoformat(on) if on else TOMORROW
    av = assumptions.values()
    lookback = int(av["attendance_lookback_days"])
    use_model = _headcount_in_use(av)
    last_day = min(d - timedelta(days=1), WINDOW_END).isoformat()

    open_work: dict = defaultdict(list)
    for op, rows in st["orders"].items():
        for r in rows:
            if r["date"] == last_day and r["status"] in ("partial", "weathered_off", "not_started") \
                    and not r["carried_to"]:
                open_work[r["crew_code"]].append({
                    "order_id": r["order_id"], "block_label": r["block_label"],
                    "activity": r["activity"],
                    "remaining_qty": round(max(0.0, r["planned_qty"] - r["actual_qty"]), 2),
                    "unit": r["unit"]})

    crews = []
    for c in st["crews"]:
        if crew_type and c["crew_type"] != crew_type:
            continue
        exp = _expected_present(st, c["crew_code"], d, lookback, use_model)
        rate, unit = _rate_for(c["crew_type"], av)
        cutters = None
        if c["crew_type"] == "harvest" and c["harvesters"] and c["establishment"]:
            cutters = int(round(exp["present"] * c["harvesters"] / c["establishment"]))
        man_days = cutters if cutters is not None else exp["present"]
        crews.append({
            "crew_code": c["crew_code"], "crew_type": c["crew_type"], "name": c["name"],
            "division_code": c["division_code"], "home_block": c["home_block"],
            **exp, "cutters": cutters,
            "man_days": man_days,
            "rate_per_man_day": rate, "rate_unit": unit,
            "capacity_units": round(man_days * rate, 1),
            "open_work": open_work.get(c["crew_code"], []),
        })

    vehicles = []
    if crew_type in (None, "transport"):
        turn = _vehicle_turnaround(st)
        seen = _vehicle_daily_loads(st)
        hours = av["truck_hours_per_day"]
        for v in st["vehicles"]:
            t = turn.get(v["vehicle_id"], 2.5)
            theory = max(1, int(hours // max(t, 0.5)))
            observed = seen.get(v["vehicle_id"], 0)
            loads = max(theory, observed)
            basis = (f"{hours:.0f} truck hours over a {t:.1f} h mean turnaround"
                     if theory >= observed else
                     f"busy-day loads in the trip ledger ({observed}); the {t:.1f} h "
                     f"turnaround would allow {theory}")
            vehicles.append({
                "crew_code": v["vehicle_id"], "crew_type": "transport",
                "vehicle_class": v["vehicle_class"], "capacity_t": v["capacity_t"],
                "mean_turnaround_h": round(t, 2), "loads_per_day": loads,
                "loads_theoretical": theory, "loads_observed_p80": observed,
                "capacity_units": round(loads * v["capacity_t"], 1), "rate_unit": "tonnes",
                "present": 1, "on_roll": 1, "man_days": loads,
                "basis": basis,
            })

    by_type: dict = defaultdict(lambda: {"crews": 0, "on_roll": 0, "present": 0, "man_days": 0})
    for c in crews:
        t = by_type[c["crew_type"]]
        t["crews"] += 1
        t["on_roll"] += c["on_roll"]
        t["present"] += c["present"]
        t["man_days"] += c["man_days"]
    return {
        "available": True,
        "date": d.isoformat(),
        "is_tomorrow": d == TOMORROW,
        "inside_ledger": d <= WINDOW_END,
        "lookback_days": lookback,
        "crews": crews, "vehicles": vehicles,
        "by_type": dict(by_type),
        "totals": {"crews": len(crews), "on_roll": sum(c["on_roll"] for c in crews),
                   "present": sum(c["present"] for c in crews),
                   "man_days": sum(c["man_days"] for c in crews),
                   "vehicles": len(vehicles),
                   "haul_capacity_t": round(sum(v["capacity_units"] for v in vehicles), 1)},
        "assumptions_used": assumptions.used(
            ["attendance_lookback_days", "harvest_bunches_per_man_day",
             "circle_weed_ha_per_man_day", "spray_ha_per_man_day",
             "pest_census_palms_per_man_day", "truck_hours_per_day"]),
        "headcount_source": "forecast" if use_model else "trailing average",
        "provenance": ("synthetic: crews, rolls and attendance are generated; the "
                       "attendance reproduces the labour feed's division-month rates "
                       "exactly. Rates per man-day are assumptions."),
        "note": (("Present figures are the headcount forecast: most likely, with a range. "
                  if use_model else "Tomorrow's present figure is a forecast from trailing attendance. ")
                 + "Edit it on the plan before it becomes an assignment."),
    }


# ── demand ─────────────────────────────────────────────────────────────────

def _annual_value(st: dict, k: str, av: dict) -> tuple[float, float]:
    """A block's annual tonnes and rupiah, annualised from the real window."""
    b = st["blocks"][k]
    days = (WINDOW_END - WINDOW_START).days + 1
    tonnes = b["bunches_total"] / days * 365 * (st["abw"].get(k) or av["abw_kg"]) / 1000
    return tonnes, tonnes * 1000 * av["ffb_price_idr_kg"]


def _urgency_weight(u: float) -> float:
    return max(0.2, min(2.5, u))


def _harvest_rates(estate: str = "EC") -> dict:
    """Per-block adjusted target, from the productivity model's cached fit."""
    try:
        from gis.models import productivity
        return productivity.block_targets(estate)
    except Exception:
        return {}


def demand(operation: str, on: str | None = None) -> dict:
    """What is due on a date, how urgent, and what deferring it costs.

    Every figure is computed here and returned; the scheduler adds terms but
    invents no quantities. `items` is the full candidate list, `not_due` the
    blocks left out and why.
    """
    m = OPERATIONS.get(operation)
    if not m:
        return {"available": False, "reason": f"No operation {operation!r}."}
    st = _state()
    d = date.fromisoformat(on) if on else TOMORROW
    av = assumptions.values()
    used = ["ffb_price_idr_kg", "abw_kg"]
    items, not_due = [], []

    if operation == "harvest":
        used += ["harvest_loss_pct_per_day_overdue", "harvest_bunches_per_man_day",
                 "harvest_ripening_lookback_days"]
        rates = _harvest_rates()
        pace: dict = {}
        if int(av.get("use_learned_rates", 0)) == 1:
            from gis.models import rates as learned
            if learned.backtest_passes("harvest"):
                pace = learned.speeds_at("harvest", d)
        lookback = int(av["harvest_ripening_lookback_days"])
        since = (d - timedelta(days=lookback)).isoformat()
        cut: dict = defaultdict(float)
        last: dict = {}
        for r in st["orders"]["harvest"]:
            if r["date"] >= d.isoformat() or r["actual_qty"] <= 0:
                continue
            if r["date"] >= since:
                cut[r["block_key"]] += r["actual_qty"]
            last[r["block_key"]] = r["date"]
        for k, b in st["blocks"].items():
            rot = st["rot"].get(k) or {}
            target = rot.get("target") or 7
            last_cut = last.get(k)
            days_since = (d - date.fromisoformat(last_cut)).days if last_cut else target
            rate_day = cut.get(k, 0.0) / lookback
            ready = rate_day * days_since
            if ready <= 0:
                ready = b["bunches_total"] / ((WINDOW_END - WINDOW_START).days + 1) * days_since
            pressure = days_since / target
            abw = st["abw"].get(k) or av["abw_kg"]
            tonnes = ready * abw / 1000
            value = tonnes * 1000 * av["ffb_price_idr_kg"]
            rate = rates.get(f"{b['division']}-{b['block_code']}") or av["harvest_bunches_per_man_day"]
            factor = (pace.get(k) or {}).get("factor", 1.0)
            rate_source = ("productivity model, block-adjusted" if f"{b['division']}-{b['block_code']}" in rates
                           else "flat quota assumption")
            if abs(factor - 1.0) > 1e-9:
                rate *= factor
                rate_source += ", scaled by this block's record"
            item = {
                "block_key": k, "block_id": b["id"], "block_label": b["label"],
                "division_code": b["division"], "block_code": b["block_code"],
                "planted_ha": b["ha"], "palms": b["palms"],
                "last_done": last_cut, "days_since": days_since, "target_days": target,
                "days_over_round": days_since - target, "urgency": round(pressure, 2),
                "qty": round(ready), "unit": "bunches",
                "rate_per_man_day": round(rate, 1),
                "rate_source": rate_source, "pace_factor": round(factor, 3),
                "man_days": round(ready / rate, 2) if rate else None,
                "tonnes_at_risk": round(tonnes, 2), "value_idr": round(value),
                "deferral_cost_idr_per_day": round(value * av["harvest_loss_pct_per_day_overdue"] / 100
                                                   * _urgency_weight(pressure)),
                "horizon_days": target,
                "road_condition": st["roads"].get(k), "gang_code": rot.get("gang_code"),
            }
            (items if pressure >= 0.6 else not_due).append(item)

    elif operation in ("prune", "weed", "spray"):
        used += ["upkeep_loss_pct_per_day_overdue"]
        rate_key = {"pruning": "prune_palms_per_man_day", "circle_weeding": "circle_weed_ha_per_man_day",
                    "path_upkeep": "path_upkeep_ha_per_man_day", "spraying": "spray_ha_per_man_day"}
        used += sorted({rate_key[a] for a in m["activities"]})
        rows = st["orders"][operation]
        for k, b in st["blocks"].items():
            for act in m["activities"]:
                interval = UPKEEP_INTERVALS[act]
                mine = [r for r in rows if r["block_key"] == k and r["activity"] == act
                        and r["date"] < d.isoformat()]
                done = [r for r in mine if r["status"] == "completed"]
                if done:
                    last_done = date.fromisoformat(done[-1]["date"])
                else:
                    s = st["upkeep_state"].get((k, act)) or {}
                    ld = date.fromisoformat(s["last_done"]) if s.get("last_done") else None
                    if ld is None or ld >= d:
                        first = mine[0]["date"] if mine else None
                        ld = (date.fromisoformat(first) - timedelta(days=interval)) if first \
                            else d - timedelta(days=int(interval * 0.9))
                    last_done = ld
                since_done = [r for r in mine if date.fromisoformat(r["date"]) > last_done]
                qty_full = float(b["palms"]) if act == "pruning" else float(b["ha"])
                remaining = qty_full - sum(r["actual_qty"] for r in since_done)
                in_progress = bool(since_done) and remaining < qty_full
                qty = max(0.0, remaining) if in_progress else qty_full
                days_since = (d - last_done).days
                urgency = days_since / interval
                rate = av[rate_key[act]]
                tonnes_yr, value_yr = _annual_value(st, k, av)
                loss = av["upkeep_loss_pct_per_day_overdue"] / 100
                item = {
                    "block_key": k, "block_id": b["id"], "block_label": b["label"],
                    "division_code": b["division"], "block_code": b["block_code"],
                    "planted_ha": b["ha"], "palms": b["palms"], "activity": act,
                    "last_done": last_done.isoformat(), "days_since": days_since,
                    "target_days": interval, "days_over_round": days_since - interval,
                    "urgency": round(urgency, 2), "in_progress": in_progress,
                    "qty": round(qty, 2 if act != "pruning" else 0), "unit": ACTIVITY_UNIT[act],
                    "rate_per_man_day": rate, "rate_source": "assumption register",
                    "man_days": round(qty / rate, 2) if rate else None,
                    "tonnes_at_risk": round(tonnes_yr * loss * interval, 2),
                    "value_idr": round(value_yr * loss * interval),
                    "deferral_cost_idr_per_day": round(value_yr * loss * _urgency_weight(urgency)),
                    "horizon_days": 7,
                    "road_condition": st["roads"].get(k),
                }
                (items if urgency >= 0.85 or in_progress else not_due).append(item)

    elif operation == "pest":
        used += ["pest_spread_loss_pct_per_day", "pest_treatment_palms_per_man_day"]
        rows = st["orders"]["pest"]
        done_follow = {(r["block_key"], r["date"]) for r in rows
                       if r["activity"] == "followup" and r["actual_qty"] > 0 and r["date"] < d.isoformat()}
        by_key = {}
        for r in layers.block_rows("EC", synthetic_world=True) or []:
            by_key[_key(r["division_code"], r["block_code"])] = r
        treated_recent = set()
        for k, ts in st["treatments"].items():
            for t in ts:
                if (d - date.fromisoformat(t["treated_date"])).days <= 180:
                    treated_recent.add(k)
        for k, b in st["blocks"].items():
            tonnes_yr, value_yr = _annual_value(st, k, av)
            loss = av["pest_spread_loss_pct_per_day"] / 100
            for t in st["treatments"].get(k) or []:
                due = date.fromisoformat(t["followup_due"])
                if due > d:
                    continue
                if t["followup_done"] and t["followup_done"] < d.isoformat():
                    continue
                overdue = (d - due).days
                urgency = 1 + overdue / t["interval_days"]
                rate = (av["pest_treatment_palms_per_man_day"] if t["pest"] == "ganoderma"
                        else 120.0 if t["pest"] == "rhinoceros_beetle" else 200.0)
                items.append({
                    "block_key": k, "block_id": b["id"], "block_label": b["label"],
                    "division_code": b["division"], "block_code": b["block_code"],
                    "planted_ha": b["ha"], "palms": b["palms"], "activity": "followup",
                    "pest": t["pest"], "method": t["method"], "treatment_id": t["treatment_id"],
                    "last_done": t["treated_date"], "days_since": (d - date.fromisoformat(t["treated_date"])).days,
                    "target_days": t["interval_days"], "days_over_round": overdue,
                    "urgency": round(urgency, 2),
                    "qty": t["palms_treated"], "unit": "palms",
                    "rate_per_man_day": rate, "rate_source": "assumption register",
                    "man_days": round(t["palms_treated"] / rate, 2),
                    "tonnes_at_risk": round(tonnes_yr * loss * t["interval_days"], 2),
                    "value_idr": round(value_yr * loss * t["interval_days"]),
                    "deferral_cost_idr_per_day": round(value_yr * loss * _urgency_weight(urgency)),
                    "horizon_days": 7, "road_condition": st["roads"].get(k),
                })
            br = by_key.get(k) or {}
            pct = br.get("ganoderma_pct")
            if pct and pct >= 3.0 and k not in treated_recent:
                palms = int(round((br.get("palms_inspected") or b["palms"]) * pct / 100 * 1.5))
                urgency = 1 + pct / 5.0
                rate = av["pest_treatment_palms_per_man_day"]
                items.append({
                    "block_key": k, "block_id": b["id"], "block_label": b["label"],
                    "division_code": b["division"], "block_code": b["block_code"],
                    "planted_ha": b["ha"], "palms": b["palms"], "activity": "treatment",
                    "pest": "ganoderma", "method": "soil mounding + trunk injection",
                    "last_done": None, "days_since": None, "target_days": 180,
                    "days_over_round": None, "urgency": round(urgency, 2),
                    "ganoderma_pct": pct,
                    "qty": palms, "unit": "palms",
                    "rate_per_man_day": rate, "rate_source": "assumption register",
                    "man_days": round(palms / rate, 2),
                    "tonnes_at_risk": round(tonnes_yr * loss * 180, 2),
                    "value_idr": round(value_yr * loss * 180),
                    "deferral_cost_idr_per_day": round(value_yr * loss * _urgency_weight(urgency)),
                    "horizon_days": 7, "road_condition": st["roads"].get(k),
                })

    elif operation == "dispatch":
        used += ["dispatch_ffa_loss_pct_per_day", "truck_hours_per_day"]
        from gis.models import scheduler
        hp = scheduler.plan("harvest", d.isoformat())
        cap_t = (sum(v["capacity_t"] for v in st["vehicles"]) / len(st["vehicles"])) if st["vehicles"] else 4.5
        for c in hp.get("crews") or []:
            for blk in c["blocks"]:
                k = blk["block_key"]
                b = st["blocks"][k]
                tonnes = blk.get("tonnes") or 0.0
                if tonnes <= 0:
                    continue
                value = tonnes * 1000 * av["ffb_price_idr_kg"]
                loads = max(1, math.ceil(tonnes / cap_t))
                items.append({
                    "block_key": k, "block_id": b["id"], "block_label": b["label"],
                    "division_code": b["division"], "block_code": b["block_code"],
                    "planted_ha": b["ha"], "palms": b["palms"], "activity": "evacuation",
                    "harvest_crew": c["crew_code"], "bunches": blk.get("qty"),
                    "last_done": None, "days_since": 0, "target_days": 1, "days_over_round": 0,
                    "urgency": 1.0,
                    "qty": round(tonnes, 2), "unit": "tonnes",
                    "rate_per_man_day": round(cap_t, 2), "rate_source": "mean vehicle capacity",
                    "man_days": loads, "loads": loads,
                    "tonnes_at_risk": round(tonnes, 2), "value_idr": round(value),
                    "deferral_cost_idr_per_day": round(value * av["dispatch_ffa_loss_pct_per_day"] / 100),
                    "horizon_days": 2, "road_condition": st["roads"].get(k),
                    "route_code": f"R-{int(b['division']):02d}",
                })

    items.sort(key=lambda i: (-i["deferral_cost_idr_per_day"], -i["urgency"]))
    not_due.sort(key=lambda i: -i["urgency"])
    return {
        "available": True, "operation": operation, "date": d.isoformat(),
        "items": items, "not_due": not_due,
        "totals": {
            "blocks_due": len(items),
            "qty_due": round(sum(i["qty"] for i in items), 1), "unit": m["unit"],
            "ha_due": round(sum(i["planted_ha"] or 0 for i in items), 1),
            "man_days_due": round(sum(i["man_days"] or 0 for i in items), 1),
            "tonnes_at_risk": round(sum(i["tonnes_at_risk"] for i in items), 1),
            "deferral_cost_idr_per_day": round(sum(i["deferral_cost_idr_per_day"] for i in items)),
            "overdue": sum(1 for i in items if i["urgency"] >= 1.0),
        },
        "assumptions_used": assumptions.used(sorted(set(used))),
        "demand_signal": m["demand"],
        "provenance": ("scheduled: quantities, urgency and rupiah are computed from the "
                       "ledger, the client's real block areas, and the assumption register."),
    }


# ── summary for the rail ───────────────────────────────────────────────────

def summary() -> dict:
    st = _state()
    out = {}
    for op, m in OPERATIONS.items():
        rows = st["orders"].get(op) or []
        if not rows:
            out[op] = {"available": False}
            continue
        a = _agg(rows)
        out[op] = {"available": True, "label": m["label"], "orders": a["orders"],
                   "adherence_pct": a["adherence_pct"], "carried_forward": a["carried_forward"],
                   "unit": m["unit"], "last_date": rows[-1]["date"]}
    out["window"] = window()
    out["crews"] = len(st["crews"])
    return out


# ── did it work ────────────────────────────────────────────────────────────

def _forecast_check(st: dict, exp: dict, due: str, observed: dict | None, exp_qty) -> dict | None:
    """What the forecasts said when a plan was accepted, set against the day."""
    f = exp.get("forecast") or {}
    if not f or due > WINDOW_END.isoformat():
        return None
    checks = []
    fell = st["rain"].get(due)
    if f.get("rain_washoff_pct") is not None and fell is not None:
        p = f["rain_washoff_pct"]
        came = fell >= 15.0
        # A chance cannot be right or wrong on one day; the check is whether
        # the side it leaned to happened.
        right = (p >= 50) == came
        checks.append({"forecast": "rain", "said": f"{p}% chance of 15 mm or more",
                       "happened": f"{fell:.0f} mm fell", "right": right,
                       "plain": (f"Rain: it said a {p}% chance of 15 mm or more, and {fell:.0f} mm fell. "
                                 + ("It leaned the right way." if right else "It leaned the wrong way.")
                                 + " One day cannot prove a chance right or wrong; the track record is in the outlook.")})
    codes = exp.get("crew_codes") or []
    if f.get("present_low") is not None and codes:
        came = [st["att"].get((c, due)) for c in codes]
        if all(came):
            total = sum(a["present"] for a in came)
            right = f["present_low"] <= total <= f["present_high"]
            checks.append({"forecast": "headcount", "said": f"{f['present_low']} to {f['present_high']}",
                           "happened": f"{total} came", "right": right,
                           "plain": (f"Headcount: it said {f['present_low']:,} to {f['present_high']:,} would turn up; "
                                     f"{total:,} came, {'inside' if right else 'outside'} the range.")})
    # Work done is a forecast of the share of planned work that gets done, so it
    # is set against the ledger's own done-over-planned on the planned blocks it
    # worked that day. Blocks the estate did not work at all are a different
    # question (was the plan followed?), answered by realisation above.
    if f.get("done_pct") is not None and observed and observed.get("planned_in_ledger"):
        share = 100 * observed["qty"] / observed["planned_in_ledger"]
        right = f["done_low_pct"] <= share <= f["done_high_pct"]
        checks.append({"forecast": "work_done", "said": f"about {f['done_pct']}% ({f['done_low_pct']} to {f['done_high_pct']}%)",
                       "happened": f"{share:.0f}% done", "right": right,
                       "plain": (f"Work done: it said about {f['done_pct']}% of the planned work would get done "
                                 f"({f['done_low_pct']}% to {f['done_high_pct']}%). On the {observed['blocks_worked']} "
                                 f"planned block{'' if observed['blocks_worked'] == 1 else 's'} the estate worked that day, {share:.0f}% of what was planned got done, "
                                 f"{'inside' if right else 'outside'} the range.")})
    if f.get("spray_verdict"):
        checks.append({"forecast": "spray", "said": f["spray_verdict"],
                       "happened": f"{fell:.0f} mm fell" if fell is not None else "rain not recorded",
                       "right": (f["spray_verdict"] == "Spray") == ((fell or 0) < 15.0),
                       "plain": (f"Spray call: {f['spray_verdict'].lower()}; "
                                 + (f"{fell:.0f} mm fell, so spraying would "
                                    f"{'have washed off' if (fell or 0) >= 15 else 'have held'}." if fell is not None else
                                    "rain on the day is not recorded."))})
    return {"checks": checks} if checks else None


def outcomes(estate: str = "EC") -> dict:
    """Accepted plans read back against the ledger once their date has passed.

    Two halves. The decisions half reads the decision log's due_date,
    expected_effect and order_ref and looks for what the ledger recorded on
    that date for those blocks. The baseline half is the same reading over
    the whole generated history: every day's plan against every day's actual,
    which is the loop the ledger closes on its own.
    """
    from gis import decisions
    st = _state()
    log_rows = decisions.history(500, estate)["decisions"]
    plans = [r for r in log_rows if r.get("due_date") and r.get("expected_effect")]

    tracked = []
    for r in plans:
        exp = r["expected_effect"] if isinstance(r["expected_effect"], dict) else {}
        op = exp.get("operation")
        due = r["due_date"]
        blocks = set(exp.get("block_keys") or [])
        observed = None
        status = "pending"
        if op in OPERATIONS and due <= WINDOW_END.isoformat():
            day_rows = [x for x in st["orders"].get(op) or [] if x["date"] == due]
            rows = [x for x in day_rows if not blocks or x["block_key"] in blocks]
            if rows:
                observed = {
                    "qty": round(sum(x["actual_qty"] for x in rows), 1),
                    "planned_in_ledger": round(sum(x["planned_qty"] for x in rows), 1),
                    "orders": len(rows),
                    "blocks_worked": len({x["block_key"] for x in rows}),
                    "man_days": round(sum(x["man_days_actual"] for x in rows), 1),
                }
                if op == "harvest":
                    observed["tonnes"] = round(sum(
                        x["actual_qty"] * (st["abw"].get(x["block_key"]) or 0) for x in rows) / 1000, 2)
                observed["blocks_planned"] = len(blocks)
                observed["blocks_not_worked"] = len(blocks) - observed["blocks_worked"]
                observed["estate_qty_that_day"] = round(sum(x["actual_qty"] for x in day_rows), 1)
                observed["basis"] = ("the ledger's actual on the planned blocks that day; a "
                                     "planned block the estate did not work counts as zero")
                status = "executed"
            else:
                status = "not_executed"
        elif due > WINDOW_END.isoformat():
            status = "beyond_ledger"
        exp_qty = exp.get("qty")
        realised = (round(100 * observed["qty"] / exp_qty, 1)
                    if observed and exp_qty else None)
        forecast_check = _forecast_check(st, exp, due, observed, exp_qty)
        tracked.append({
            "decision_id": r["id"], "created_at": r["created_at"], "title": r["title"],
            "action": r["action"], "operation": op, "due_date": due,
            "expected": exp, "observed": observed, "status": status,
            "realisation_pct": realised,
            "order_ref": r.get("order_ref") or [],
            "forecast_check": forecast_check,
        })

    executed = [t for t in tracked if t["status"] == "executed"]
    exp_t = sum((t["expected"].get("tonnes") or 0) for t in executed)
    obs_t = sum((t["observed"].get("tonnes") or 0) for t in executed if t["observed"])
    exp_q = sum((t["expected"].get("qty") or 0) for t in executed)
    obs_q = sum((t["observed"].get("qty") or 0) for t in executed if t["observed"])

    baseline = []
    for op, m in OPERATIONS.items():
        rows = st["orders"].get(op) or []
        if not rows:
            continue
        a = _agg(rows)
        by_month = defaultdict(list)
        for r in rows:
            by_month[r["month"]].append(r)
        baseline.append({
            "operation": op, "label": m["label"], "unit": m["unit"],
            "orders": a["orders"], "planned_qty": a["planned_qty"], "actual_qty": a["actual_qty"],
            "realisation_pct": a["adherence_pct"],
            "by_month": [{"month": mo, "realisation_pct": _agg(rs)["adherence_pct"]}
                         for mo, rs in sorted(by_month.items())],
        })

    checks = [c for t in tracked for c in (t["forecast_check"] or {}).get("checks", [])]
    forecast_summary = {
        "checked": len(checks),
        "right": sum(1 for c in checks if c["right"]),
        "plain": (f"Of {len(checks)} forecasts checked against what happened, {sum(1 for c in checks if c['right'])} "
                  "landed where they said." if checks else
                  "No accepted plan with forecasts has reached its day inside the ledger yet."),
    }

    accepted = [t for t in tracked if t["action"] == "accepted"]
    headline = (f"{len(accepted)} plans accepted, {len(executed)} executed, "
                + (f"expected {exp_t:,.1f} t, observed {obs_t:,.1f} t, "
                   f"{100 * obs_t / exp_t:.0f}% realisation."
                   if exp_t else
                   f"expected {exp_q:,.0f}, observed {obs_q:,.0f}"
                   + (f", {100 * obs_q / exp_q:.0f}% realisation." if exp_q else ".")))
    return {
        "available": True,
        "headline": headline,
        "plans": tracked,
        "counts": {"accepted": len(accepted), "executed": len(executed),
                   "pending": sum(1 for t in tracked if t["status"] in ("pending", "beyond_ledger")),
                   "not_executed": sum(1 for t in tracked if t["status"] == "not_executed")},
        "expected_tonnes": round(exp_t, 1), "observed_tonnes": round(obs_t, 1),
        "realisation_pct": round(100 * obs_t / exp_t, 1) if exp_t else None,
        "baseline": baseline,
        "forecast_summary": forecast_summary,
        "window": window(),
        "how_to_test": ("Open a plan for a date inside the ledger, for example "
                        f"{(WINDOW_END - timedelta(days=11)).isoformat()}, accept it, and "
                        "come back here: the ledger already holds what happened that day."),
        "provenance": ("the decision log is real: what a person accepted in this app. "
                       "The observed side is read from the synthetic ledger."),
        "note": ("This is the only thing in the build that improves with use. A plan "
                 "accepted for a date beyond 2025-05-23 stays pending until a ledger "
                 "extract covers it."),
    }
