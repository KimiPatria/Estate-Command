"""Cost efficiency: where the estate loses money, node by node (buildplan_finance.md).

Every unit of the estate (a block, a vehicle, a driver, a crew, a material, a
road segment) gets the same four figures each month:

    S  spend       what the CO ledger posted to it (ec_co_postings.csv)
    L  crop lost   value the unit lost without paying for it: loose fruit,
                   late rounds, shrinkage, mill-queue FFA, downtime
    N  necessary   its volume at the rate its best peers achieve (or a hard
                   spec: litres per km, the prescription, the rate card)

    leakage    = max(0, S + L - N)
    efficiency = min(100%, N / (S + L))

and the gap split into named drivers that sum to it. Lists rank by rupiah;
the percentage only reads. A parent's efficiency is sum(N) / sum(S + L) over
its children, so the estate figure and the node figures always agree.

Three rules the code keeps:

  * Each loss has one owner. Overtime posts to the block order in CO but is
    the gang's; material posts to the block at standard and the store owns
    the price variance; the store rolls up as its variance only, because the
    material itself is already in the blocks.
  * Spending less is not efficiency. An overdue upkeep round is reported as
    Deferred, valued at the register's loss per day overdue, and never lifts
    the score, which is capped at 100%.
  * The answer key (ec_fi_leak_key.csv) is read only by accuracy(), to score
    the engine. Nothing that ranks or explains a leak ever sees it.

Nothing here fits a model. Benchmarks are quantiles of peers, so a register
edit reprices in a second and the window never waits.
"""

import logging
import math
import threading
from collections import defaultdict
from datetime import date, timedelta
from threading import Lock

from gis import assumptions
from gis import finance_playbook as pb
from gis.build_operations import WINDOW_END, WINDOW_START, _read

log = logging.getLogger("estate-command.finance")

MONTHS = ("2025-01", "2025-02", "2025-03", "2025-04", "2025-05")
DEFAULT_MONTH = "2025-04"          # the last closed month; May runs to the 23rd
MONTH_LABEL = {"2025-01": "January 2025", "2025-02": "February 2025", "2025-03": "March 2025",
               "2025-04": "April 2025", "2025-05": "May 2025 (to the 23rd)"}
MONTH_SHORT = {"2025-01": "Jan", "2025-02": "Feb", "2025-03": "Mar", "2025-04": "Apr",
               "2025-05": "May"}
WINDOW_DAYS = (WINDOW_END - WINDOW_START).days + 1
KG_PER_LOOSE_FRUIT = 0.012          # loose_fruit.KG_PER_LOOSE_FRUIT, a placeholder there too
BLOCK_FUNCTIONS = ("harvesting", "fertiliser", "upkeep", "pest", "roads")
GROUP_MIN_UNITS = 5
GROUP_MIN_SHARE = 0.4
FLEET_PEERS_MIN = 3
PEST_RATE = {"ganoderma": None, "rhinoceros_beetle": 120.0, "rat": 200.0}
SPRAY_RAIN_MM = 15.0               # build_operations.SPRAY_RAIN_MM
FERT_MATNR = {"NPK 12-12-17-2": "FE-001", "Urea": "FE-002", "Kieserite": "FE-003", "Borate": "FE-004"}
UPKEEP_RATE_KEY = {"pruning": "prune_palms_per_man_day", "circle_weeding": "circle_weed_ha_per_man_day",
                   "path_upkeep": "path_upkeep_ha_per_man_day", "spraying": "spray_ha_per_man_day"}
REGISTER_KEYS = ["ffb_price_idr_kg", "man_day_cost_idr", "harvest_loss_pct_per_day_overdue",
                 "upkeep_loss_pct_per_day_overdue", "pest_spread_loss_pct_per_day",
                 "vehicle_hire_idr_per_hour", "ffa_idr_per_kg_point", "fert_kg_per_man_day",
                 "benchmark_quantile", "min_peer_group", "leak_materiality_idr",
                 "part_load_threshold_pct", "rain_cutoff_mm", "pest_census_palms_per_man_day",
                 "pest_treatment_palms_per_man_day", "glyphosate_l_per_ha", "metsulfuron_kg_per_ha",
                 "prune_palms_per_man_day", "circle_weed_ha_per_man_day", "path_upkeep_ha_per_man_day",
                 "spray_ha_per_man_day"]

_LOCK = Lock()
_CACHE: dict = {}


# ── small helpers ──────────────────────────────────────────────────────────

def _f(v, d=0.0):
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _k(div, block) -> str:
    return f"{int(float(div))}|{int(float(block))}"


def _q(vals, q: float):
    """Linear-interpolated quantile; None for an empty list."""
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _terciles(values: dict) -> dict:
    """key -> 0, 1, 2 by tercile of its value."""
    ks = sorted(values, key=lambda k: values[k])
    n = len(ks)
    return {k: min(2, 3 * i // max(n, 1)) for i, k in enumerate(ks)}


def _month_of(d) -> str:
    return (d if isinstance(d, str) else d.isoformat())[:7]


def _month_days(mo: str) -> int:
    y, m = int(mo[:4]), int(mo[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return max(0, (min(nxt - timedelta(days=1), WINDOW_END) - max(first, WINDOW_START)).days + 1)


def rp(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"Rp {v / 1e9:,.2f} bn"
    if a >= 1e7:
        return f"Rp {v / 1e6:,.0f} m"
    if a >= 1e5:
        return f"Rp {v / 1e6:,.1f} m"
    return f"Rp {v:,.0f}"


# ── loading: the ledger and every feed it was posted from ─────────────────

def _in_window(d: str) -> bool:
    return WINDOW_START.isoformat() <= d <= WINDOW_END.isoformat()


def available() -> bool:
    return bool(_read("ec_co_postings.csv"))


def _load() -> dict:
    from gis import environment, ontology
    postings = _read("ec_co_postings.csv")
    if not postings:
        return {}
    for p in postings:
        p["wsl"] = _f(p["wsl"])
        p["quantity"] = _f(p["quantity"])
        p["month"] = p["budat"][:7]

    geo = ontology.blocks_geojson("EC", synthetic_world=True) or {"features": []}
    blocks = {}
    for f in geo["features"]:
        pr = f["properties"]
        k = _k(pr["division_code"], pr["block_code"])
        bm = {m: (pr.get("bunches_by_month") or {}).get(m, 0) or 0 for m in MONTHS}
        lm = {m: (pr.get("loose_by_month") or {}).get(m, 0) or 0 for m in MONTHS}
        try:
            geom = f["geometry"]
            ring = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
            lon = sum(p[0] for p in ring) / len(ring)
            lat = sum(p[1] for p in ring) / len(ring)
        except (KeyError, IndexError, TypeError, ZeroDivisionError):
            lon = lat = None
        blocks[k] = {"key": k, "id": f["id"], "label": pr.get("block_label") or k,
                     "lon": lon, "lat": lat,
                     "records": pr.get("harvest_records") or 0, "loose_records": pr.get("loose_records") or 0,
                     "division": str(int(float(pr["division_code"]))),
                     "block_code": str(int(float(pr["block_code"]))),
                     "ha": _f(pr.get("planted_ha")), "palms": _f(pr.get("palms")),
                     "bunches_by_month": bm, "loose_by_month": lm,
                     "bunches": sum(bm.values()), "loose": sum(lm.values())}

    slope = defaultdict(list)
    hday = _read("ec_harvester_day.csv")
    for r in hday:
        slope[_k(r["division_code"], r["block_code"])].append(_f(r["slope_deg"]))
    km = {}
    for r in _read("ec_block_costs.csv"):
        km.setdefault(_k(r["division_code"], r["block_code"]), _f(r["km_to_mill"]))
    for k, b in blocks.items():
        b["slope"] = sum(slope[k]) / len(slope[k]) if slope.get(k) else 0.0
        b["km_to_mill"] = km.get(k, 0.0)
        b["density"] = b["bunches"] / b["ha"] if b["ha"] else 0.0
    dims = {name: _terciles({k: b[field] for k, b in blocks.items()})
            for name, field in (("density", "density"), ("slope", "slope"), ("distance", "km_to_mill"))}
    for k, b in blocks.items():
        b["peer"] = {n: dims[n][k] for n in dims}

    rot = {_k(r["division_code"], r["block_code"]): r for r in _read("ec_rotation.csv")}
    abw = {_k(r["division_code"], r["block_code"]): _f(r["abw_kg"], 8.0) for r in _read("ec_abw.csv")}
    vehicles = {r["vehicle_id"]: r for r in _read("ec_vehicles.csv")}
    crews = {r["crew_code"]: r for r in _read("ec_crews.csv")}
    materials = {r["matnr"]: r for r in _read("ec_mm_materials.csv")}

    trips = []
    for t in _read("ec_weighbridge.csv"):
        if not _in_window(t["date"]):
            continue
        v = vehicles.get(t["vehicle_id"], {})
        k = _k(t["division_code"], t["block_code"])
        hh, mm = (t.get("depart_time") or "0:0").split(":")[:2]
        trips.append({"trip_id": t["trip_id"], "date": t["date"], "month": t["month"], "key": k,
                      "division": str(int(float(t["division_code"]))),
                      "vehicle": t["vehicle_id"], "cls": t["vehicle_class"], "driver": t["driver_id"],
                      "route": t["route_code"], "km": _f(t["km_to_mill"]), "diesel": _f(t["diesel_l"]),
                      "net_kg": _f(t["net_kg"]), "bunches": _f(t["bunches_epms"]),
                      "cap_t": _f(v.get("capacity_t"), 1.0), "spec": _f(v.get("litres_per_100km")),
                      "queue_h": _f(t["queue_h"]), "turnaround_h": _f(t["turnaround_h"]),
                      "depart_h": int(hh) + int(mm) / 60.0, "abw": abw.get(k, 8.0),
                      "loose_kg": _f(t["loose_fruit_kg"])})

    orders = []
    for fn in ("ec_harvest_orders.csv", "ec_upkeep_orders.csv", "ec_pest_orders.csv"):
        for o in _read(fn):
            if not _in_window(o["date"]):
                continue
            o["key"] = _k(o["division_code"], o["block_code"])
            o["md"] = _f(o["man_days_actual"])
            o["qty"] = _f(o["actual_qty"])
            o["plan_qty"] = _f(o["planned_qty"])
            orders.append(o)

    return {
        "postings": postings, "blocks": blocks, "rot": rot, "abw": abw, "vehicles": vehicles,
        "crews": crews, "materials": materials, "trips": trips, "orders": orders,
        "harvester_day": [r for r in hday if _in_window(r["date"])],
        "attendance": [a for a in _read("ec_attendance.csv") if _in_window(a["date"])],
        "wage_types": _read("ec_hr_wage_types.csv"),
        "fuel_log": _read("ec_fuel_log.csv"),
        "invoices": _read("ec_fi_invoices.csv"),
        "po": {(r["ebeln"], r["ebelp"]): r for r in _read("ec_mm_purchase_orders.csv")},
        "sheets": _read("ec_fert_application.csv"),
        "services": {r["job_id"]: r for r in _read("ec_pm_services.csv")},
        "pm_orders": {r["order_id"]: r for r in _read("ec_pm_orders.csv")},
        "road_orders": _read("ec_road_orders.csv"),
        "roads": {r["segment_id"]: r for r in _read("ec_roads.csv")},
        "fertiliser": _read("ec_fertiliser.csv"),
        "upkeep_state": _read("ec_upkeep.csv"),
        "census": _read("ec_pest_census.csv"),
        "treatments": _read("ec_pest_treatment.csv"),
        "movements": [m for m in _read("ec_mm_movements.csv") if _in_window(m["budat"])],
        "budget": _read("ec_co_budget.csv"),
        "gl": {r["racct"]: r for r in _read("ec_fi_gl_accounts.csv")},
        "rain": environment.rainfall_by_day("EC") or {},
    }


def _base() -> dict:
    with _LOCK:
        b = _CACHE.get("base")
    if b is None:
        b = _load()
        with _LOCK:
            _CACHE["base"] = b
    return b


# ── the record every unit-month carries ────────────────────────────────────

class Units:
    def __init__(self):
        self.meta = {}
        self.rec = {}

    def unit(self, uid, function, kind, obj, label, division="", blocks=None, extra=None):
        if uid not in self.meta:
            self.meta[uid] = {"id": uid, "function": function, "unit_kind": kind, "object": obj,
                              "label": label, "division": division, "blocks": blocks or [],
                              **(extra or {})}
        return self.meta[uid]

    def add(self, uid, month, S=0.0, L=0.0, N=0.0, drivers=None, facts=None, deferred=0.0,
            volume=None, volume_unit=None, peer=None):
        r = self.rec.setdefault((uid, month), {"S": 0.0, "L": 0.0, "N": 0.0, "drivers": defaultdict(float),
                                               "facts": {}, "deferred": 0.0, "volume": 0.0,
                                               "volume_unit": volume_unit, "peer": None})
        r["S"] += S
        r["L"] += L
        r["N"] += N
        r["deferred"] += deferred
        for d, v in (drivers or {}).items():
            r["drivers"][d] += v
        if facts:
            r["facts"].update(facts)
        if volume is not None:
            r["volume"] += volume
        if volume_unit:
            r["volume_unit"] = volume_unit
        if peer is not None:
            r["peer"] = peer
        return r


def _rolling_rate(cost: dict, volume: dict, keys, months) -> dict:
    """Each unit's cost per unit of volume over up to three months to date.

    One month of a small block is a handful of cutting days, and the best
    quarter of such noisy rates is a benchmark nobody can meet. Three months
    keeps it honest without hiding a change: the unit's own month is still
    judged on its own spend."""
    out = {}
    for k in keys:
        c = sum(cost.get((k, m), 0.0) for m in months)
        v = sum(volume.get((k, m), 0.0) for m in months)
        if c > 0 and v > 0.05:
            out[k] = c / v
    return out


def _peer_group(key, cands: dict, b: dict, minimum: int):
    """The unit's peers among `cands` (key -> value): full key, then dropping slope, then distance."""
    me = b["blocks"][key]["peer"]
    tries = [(("density", "slope", "distance"), []), (("density", "distance"), ["slope"]),
             (("density",), ["slope", "distance"]), ((), ["slope", "distance", "density"])]
    for dims, dropped in tries:
        group = [k for k in cands if all(b["blocks"][k]["peer"][d] == me[d] for d in dims)]
        if len(group) >= minimum or not dims:
            return group, dropped
    return list(cands), ["slope", "distance", "density"]


# ── functions ──────────────────────────────────────────────────────────────

def _harvesting(b, av, U: Units):
    price = av["ffb_price_idr_kg"]
    q = av["benchmark_quantile"] / 100.0
    basic, prem = defaultdict(float), defaultdict(float)
    for p in b["postings"]:
        if p["racct"] == "6101000":
            basic[(_k(p["division_code"], p["block_code"]), p["month"])] += p["wsl"]
        elif p["racct"] == "6101100":
            prem[(_k(p["division_code"], p["block_code"]), p["month"])] += p["wsl"]
    tonnes = defaultdict(float)
    for t in b["trips"]:
        tonnes[(t["key"], t["month"])] += t["net_kg"] / 1000.0

    # Loose fruit: the block's window gap against the best quarter, by its months' bunches.
    ratios = {k: bl["loose"] / bl["bunches"] for k, bl in b["blocks"].items() if bl["bunches"]}
    lf_bench = _q(list(ratios.values()), 1 - q) or 0.0
    lf_month = defaultdict(float)
    for k, r in ratios.items():
        bl = b["blocks"][k]
        gap_kg = max(0.0, lf_bench - r) * bl["bunches"] * KG_PER_LOOSE_FRUIT
        for m in MONTHS:
            if bl["bunches"]:
                lf_month[(k, m)] = gap_kg * price * bl["bunches_by_month"][m] / bl["bunches"]

    # Late rounds: each cut past the block's own round loses a share of that cut a day.
    cuts = defaultdict(list)
    for o in b["orders"]:
        if o["operation"] == "harvest" and o["qty"] > 0:
            cuts[o["key"]].append((o["date"], o["qty"]))
    late, over_days, n_cuts = defaultdict(float), defaultdict(float), defaultdict(int)
    pct = av["harvest_loss_pct_per_day_overdue"] / 100.0
    for k, cs in cuts.items():
        days = sorted({d for d, _ in cs})
        qty = defaultdict(float)
        for d, x in cs:
            qty[d] += x
        target = _f((b["rot"].get(k) or {}).get("rotation_target_days"), 10.0)
        abw = b["abw"].get(k, 8.0)
        for d0, d1 in zip(days, days[1:]):
            gap = (date.fromisoformat(d1) - date.fromisoformat(d0)).days
            if gap <= 1:
                continue                     # the same round, carried over a day
            over = max(0.0, gap - target)
            mo = d1[:7]
            n_cuts[(k, mo)] += 1
            over_days[(k, mo)] += over
            late[(k, mo)] += over * pct * qty[d1] * abw * price

    minimum = int(av["min_peer_group"])
    for i, mo in enumerate(MONTHS):
        rate = _rolling_rate(basic, tonnes, b["blocks"], MONTHS[max(0, i - 2):i + 1])
        for k, bl in b["blocks"].items():
            s_b, s_p, t = basic[(k, mo)], prem[(k, mo)], tonnes[(k, mo)]
            L_lf, L_late = lf_month[(k, mo)], late[(k, mo)]
            if not (s_b or s_p or t or L_lf or L_late):
                continue
            group, dropped = _peer_group(k, rate, b, minimum)
            bench = _q([rate[g] for g in group], q) or 0.0
            uid = f"harvesting:block:{k}"
            U.unit(uid, "harvesting", "block", k, f"Block {bl['label']}", bl["division"], [bl["label"]])
            n_c = n_cuts[(k, mo)]
            U.add(uid, mo, S=s_b + s_p, L=L_lf + L_late, N=t * bench + s_p,
                  drivers={"rate_gap": s_b - t * bench, "loose_fruit": L_lf, "late_rounds": L_late},
                  volume=t, volume_unit="t",
                  peer={"basis": "Rp per tonne in cutters' wages", "n": len(group),
                        "dropped": dropped, "rate": round(bench), "quantile": q,
                        "key": dict(bl["peer"]), "values": sorted(round(rate[g]) for g in group)},
                  facts={"rate_t": s_b / t if t > 0.05 else 0.0, "bench_t": bench, "tonnes": round(t, 1),
                         "lf_ratio": ratios.get(k, 0.0), "lf_bench": lf_bench,
                         "days_over": over_days[(k, mo)] / n_c if n_c else 0.0, "cuts": n_c,
                         "premium_idr": s_p})


def _transport(b, av, U: Units):
    from gis import ffa
    price = _f(b["materials"]["FU-001"]["verpr"], 13_500)
    q = av["benchmark_quantile"] / 100.0
    thr = av["part_load_threshold_pct"] / 100.0
    try:
        slope = (ffa.position("EC").get("fit") or {}).get("turnaround", {}).get("slope_per_h") or 0.0
    except Exception:                         # the FFA panel is optional to this one
        slope = 0.0
    idr_pt = av["ffa_idr_per_kg_point"]
    spend = defaultdict(float)
    for p in b["postings"]:
        if p["racct"] == "6203000" and p["vehicle_id"]:
            spend[(p["vehicle_id"], p["month"])] += p["wsl"]
    issued = defaultdict(float)
    for f in b["fuel_log"]:
        if f["vehicle_id"] and _in_window(f["date"]):
            issued[(f["vehicle_id"], f["date"][:7])] += _f(f["litres"])

    agg = defaultdict(lambda: defaultdict(float))
    for t in b["trips"]:
        a = agg[(t["vehicle"], t["month"])]
        spec_l = 2.0 * t["km"] * t["spec"] / 100.0
        lf = t["net_kg"] / 1000.0 / t["cap_t"] if t["cap_t"] else 1.0
        a["trips"] += 1
        a["burned"] += t["diesel"]
        a["spec"] += spec_l
        a["part_l"] += spec_l * (1.0 - min(1.0, lf / thr)) if thr else 0.0
        a["part_trips"] += 1 if lf < thr else 0
        a["tonnes"] += t["net_kg"] / 1000.0
        a["queue_idr"] += slope * t["queue_h"] * t["net_kg"] * idr_pt
        a["queue_h"] += t["queue_h"]
        a["km"] += 2 * t["km"]

    for mo in MONTHS:
        by_cls = defaultdict(list)
        for (v, m), a in agg.items():
            if m == mo and a["tonnes"]:
                by_cls[b["vehicles"][v]["vehicle_class"]].append(a["queue_idr"] / a["tonnes"])
        qbench = {c: _q(vs, q) or 0.0 for c, vs in by_cls.items()}
        for v, vr in b["vehicles"].items():
            a = agg.get((v, mo))
            S = spend[(v, mo)]
            if not a and not S:
                continue
            a = a or defaultdict(float)
            cls = vr["vehicle_class"]
            burned_idr = a["burned"] * price
            fuel_gap = S - burned_idr
            over_spec = (a["burned"] - a["spec"]) * price
            part = a["part_l"] * price
            N = (a["spec"] - a["part_l"]) * price
            L = max(0.0, a["queue_idr"] - a["tonnes"] * qbench.get(cls, 0.0))
            uid = f"transport:vehicle:{v}"
            U.unit(uid, "transport", "vehicle", v, f"{cls.replace('-', ' ').capitalize()} {v}",
                   extra={"vehicle_class": cls})
            iss = issued[(v, mo)]
            U.add(uid, mo, S=S, L=L, N=N,
                  drivers={"fuel_gap": fuel_gap, "over_spec": over_spec, "part_loads": part, "queue_ffa": L},
                  volume=a["tonnes"], volume_unit="t",
                  peer={"basis": "vehicle spec: litres per 100 km at full load", "n": 1, "dropped": [],
                        "rate": _f(vr["litres_per_100km"]), "quantile": None, "key": {"class": cls}},
                  facts={"gap_l": iss - a["burned"], "gap_pct": 100 * (iss - a["burned"]) / a["burned"]
                         if a["burned"] else 0.0, "issued_l": iss, "burned_l": a["burned"],
                         "over_pct": 100 * (a["burned"] - a["spec"]) / a["spec"] if a["spec"] else 0.0,
                         "part_share": 100 * a["part_trips"] / a["trips"] if a["trips"] else 0.0,
                         "threshold": thr * 100, "queue_idr": L,
                         "queue_h": a["queue_h"] / a["trips"] if a["trips"] else 0.0,
                         "trips": int(a["trips"]), "tonnes": round(a["tonnes"], 1)})

    # Drivers: the fruit each loses between platform and mill, beyond the fleet's own norm.
    price_kg = av["ffb_price_idr_kg"]
    devs = []
    for t in b["trips"]:
        exp = t["bunches"] * t["abw"]
        if exp > 0:
            devs.append((exp - t["net_kg"]) / exp)
    med = _q(devs, 0.5) or 0.0
    wages = defaultdict(float)
    for p in b["postings"]:
        if p["racct"] == "6106000":
            wages[(p["driver_id"], p["month"])] += p["wsl"]
    sh = defaultdict(lambda: [0.0, 0.0, 0.0])
    for t in b["trips"]:
        exp = t["bunches"] * t["abw"]
        if exp <= 0:
            continue
        a = sh[(t["driver"], t["month"])]
        a[0] += exp - t["net_kg"] - exp * med
        a[1] += exp - t["net_kg"]
        a[2] += exp
    drivers = sorted({d for (d, _) in wages} | {d for (d, _) in sh})
    for drv in drivers:
        for mo in MONTHS:
            S = wages[(drv, mo)]
            a = sh.get((drv, mo))
            if not S and not a:
                continue
            excess_kg = max(0.0, a[0]) if a else 0.0
            L = excess_kg * price_kg
            uid = f"transport:driver:{drv}"
            U.unit(uid, "transport", "driver", drv, f"Driver {drv}")
            U.add(uid, mo, S=S, L=L, N=S, drivers={"shrinkage": L}, volume=a[2] / 1000 if a else 0.0,
                  volume_unit="t expected",
                  peer={"basis": "fleet median shrinkage", "n": len(drivers), "dropped": [],
                        "rate": round(med * 100, 2), "quantile": 0.5, "key": {}},
                  facts={"shrink_pct": 100 * a[1] / a[2] if a and a[2] else 0.0, "fleet_pct": med * 100,
                         "excess_kg": excess_kg})


def _fleet(b, av, U: Units):
    q = av["benchmark_quantile"] / 100.0
    hire = av["vehicle_hire_idr_per_hour"]
    cost = defaultdict(lambda: defaultdict(float))
    for p in b["postings"]:
        if p["racct"] not in ("6301000", "6204000") or not p["vehicle_id"]:
            continue
        kind = "break"
        if p["src_type"] == "service_job" and (b["services"].get(p["src_id"]) or {}).get("kind") == "service":
            kind = "serv"
        c = cost[(p["vehicle_id"], p["month"])]
        c[f"{kind}_S"] += p["wsl"]
        if p["unit"] == "H":
            c[f"{kind}_h"] += p["quantity"]
        if kind == "break" and p["racct"] == "6301000":
            c["jobs"] += 1
    trips = defaultdict(int)
    for t in b["trips"]:
        trips[(t["vehicle"], t["month"])] += 1

    rate = {}
    for v in b["vehicles"]:
        tw = sum(trips[(v, m)] for m in MONTHS)
        bw = sum(cost[(v, m)]["break_S"] + cost[(v, m)]["break_h"] * hire for m in MONTHS)
        sw = sum(cost[(v, m)]["serv_S"] + cost[(v, m)]["serv_h"] * hire for m in MONTHS)
        rate[v] = (bw / tw if tw else None, sw / tw if tw else None)
    for v, vr in b["vehicles"].items():
        cls = vr["vehicle_class"]
        peers = [u for u, r in b["vehicles"].items() if r["vehicle_class"] == cls]
        if len(peers) < FLEET_PEERS_MIN:
            peers = list(b["vehicles"])
        pb_ = _q([rate[u][0] for u in peers], q) or 0.0
        ps_ = _q([rate[u][1] for u in peers], q) or 0.0
        uid = f"fleet:vehicle:{v}"
        U.unit(uid, "fleet", "vehicle", v, f"{cls.replace('-', ' ').capitalize()} {v}",
               extra={"vehicle_class": cls, "year": vr.get("year_acquired")})
        for mo in MONTHS:
            c, n = cost.get((v, mo)) or defaultdict(float), trips[(v, mo)]
            S = c["break_S"] + c["serv_S"]
            L = (c["break_h"] + c["serv_h"]) * hire
            if not (S or L or n):
                continue
            U.add(uid, mo, S=S, L=L, N=n * (pb_ + ps_),
                  drivers={"breakdowns": c["break_S"] + c["break_h"] * hire - n * pb_,
                           "services": c["serv_S"] + c["serv_h"] * hire - n * ps_},
                  volume=n, volume_unit="trips",
                  peer={"basis": "Rp per trip in repairs and downtime, its class over the window",
                        "n": len(peers), "dropped": [], "rate": round(pb_), "quantile": q,
                        "key": {"class": cls}, "values": sorted(round(rate[u][0] or 0) for u in peers)},
                  facts={"repair_idr": c["break_S"], "downtime_h": c["break_h"] + c["serv_h"],
                         "jobs": int(c["jobs"]), "bench_trip": pb_, "trips": n})


def _fertiliser(b, av, U: Units):
    verpr = {m: _f(r["verpr"]) for m, r in b["materials"].items()}
    s_mat, s_app = defaultdict(float), defaultdict(float)
    s_month = defaultdict(lambda: defaultdict(float))
    for p in b["postings"]:
        if p["racct"] in ("6201000", "6104000") and p["block_code"]:
            k = _k(p["division_code"], p["block_code"])
            (s_mat if p["racct"] == "6201000" else s_app)[k] += p["wsl"]
            s_month[k][p["month"]] += p["wsl"]
    n_mat, over, kg, target, lag = (defaultdict(float) for _ in range(5))
    n_issue = defaultdict(int)
    for r in b["fertiliser"]:
        if not r["issue_date"] or not _in_window(r["issue_date"]) or _f(r["issued_kg"]) <= 0:
            continue
        k = _k(r["division_code"], r["block_code"])
        price = verpr[FERT_MATNR[r["material"]]]
        iss, tgt = _f(r["issued_kg"]), _f(r["target_kg"])
        n_mat[k] += min(iss, tgt) * price
        over[k] += max(0.0, iss - tgt) * price
        kg[k] += iss
        target[k] += tgt
        lag[k] += _f(r["application_lag_days"])
        n_issue[k] += 1
    rate_kg = av["fert_kg_per_man_day"]
    app_kg, app_md, app_rate, sheets_n, sheet_kg = (defaultdict(float) for _ in range(5))
    seen = set()
    for s in b["sheets"]:
        k = _k(s["division_code"], s["block_code"])
        app_md[k] += _f(s["man_days"])
        sheets_n[k] += 1
        app_rate[k] = _f(s["rate_idr_per_md"])
        issue = (k, s["material"], s["issue_date"])
        if issue not in seen:
            seen.add(issue)
            app_kg[k] += _f(s["kg_applied"])
    for k in set(s_mat) | set(s_app):
        bl = b["blocks"].get(k)
        if not bl:
            continue
        n_app = app_kg[k] / rate_kg * app_rate[k] if rate_kg else 0.0
        S = s_mat[k] + s_app[k]
        N = n_mat[k] + n_app
        drivers = {"over_issue": over[k], "application_labour": s_app[k] - n_app,
                   "other": s_mat[k] - n_mat[k] - over[k]}
        uid = f"fertiliser:block:{k}"
        U.unit(uid, "fertiliser", "block", k, f"Block {bl['label']}", bl["division"], [bl["label"]])
        tot = sum(s_month[k].values()) or 1.0
        for mo in MONTHS:
            w = s_month[k].get(mo, 0.0) / tot
            if w <= 0:
                continue
            U.add(uid, mo, S=S * w, N=N * w, drivers={d: v * w for d, v in drivers.items()},
                  volume=kg[k] / 1000 * w, volume_unit="t applied",
                  peer={"basis": "the block's prescription and the application rate card", "n": 1,
                        "dropped": [], "rate": rate_kg, "quantile": None, "key": {}},
                  facts={"md_per_t": app_md[k] / (app_kg[k] / 1000) if app_kg[k] else 0.0,
                         "std_md_per_t": 1000 / rate_kg if rate_kg else 0.0,
                         "over_pct": 100 * over[k] / n_mat[k] if n_mat[k] else 0.0,
                         "lag_days": lag[k] / n_issue[k] if n_issue[k] else 0.0,
                         "kg_per_palm_prescribed": target[k] / bl["palms"] if bl["palms"] else 0.0,
                         "sheets": int(sheets_n[k]), "window_grain": True})


def _annual_value(b, av) -> dict:
    t = defaultdict(float)
    for tr in b["trips"]:
        t[tr["key"]] += tr["net_kg"]
    return {k: v * 365 / WINDOW_DAYS * av["ffb_price_idr_kg"] for k, v in t.items()}


def _upkeep(b, av, U: Units):
    md_cost = av["man_day_cost_idr"]
    verpr = {m: _f(r["verpr"]) for m, r in b["materials"].items()}
    wage, chem = defaultdict(float), defaultdict(float)
    for p in b["postings"]:
        if p["block_code"] and p["racct"] in ("6102000", "6202000"):
            k = (_k(p["division_code"], p["block_code"]), p["month"])
            (wage if p["racct"] == "6102000" else chem)[k] += p["wsl"]
    # Labour: Rp per unit of work against the best quarter of blocks doing the same activity,
    # three months to date. The rate card stays as a fact beside it: every crew on this estate
    # runs well under the card, which makes it a planning figure, not a benchmark.
    q = av["benchmark_quantile"] / 100.0
    by_order = {}
    card_md = defaultdict(float)
    spray_ha = {}
    qty_act = defaultdict(lambda: defaultdict(float))
    for o in b["orders"]:
        if o["operation"] not in ("prune", "weed", "spray"):
            continue
        by_order[o["order_id"]] = o
        if o["qty"] <= 0:
            continue
        r = av[UPKEEP_RATE_KEY[o["activity"]]]
        card_md[(o["key"], o["date"][:7])] += o["qty"] / r if r else 0.0
        qty_act[o["activity"]][(o["key"], o["date"][:7])] += o["qty"]
        if o["activity"] == "spraying":
            spray_ha[o["order_id"]] = o["qty"]
    wage_act = defaultdict(lambda: defaultdict(float))
    for p in b["postings"]:
        if p["racct"] == "6102000" and p["src_type"] == "work_order":
            o = by_order.get(p["src_id"])
            if o:
                wage_act[o["activity"]][(o["key"], p["month"])] += p["wsl"]
    n_lab = defaultdict(float)
    for i, mo in enumerate(MONTHS):
        window = MONTHS[max(0, i - 2):i + 1]
        for act, wages in wage_act.items():
            rate = _rolling_rate(wages, qty_act[act], {k for (k, _) in wages}, window)
            bench = _q(list(rate.values()), q) or 0.0
            for (k, m), x in qty_act[act].items():
                if m == mo:
                    n_lab[(k, mo)] += x * bench
    n_chem = defaultdict(float)
    dose = {"AC-001": av["glyphosate_l_per_ha"], "AC-002": av["metsulfuron_kg_per_ha"]}
    for m in b["movements"]:
        if m["bwart"] == "201" and m["matnr"] in dose and m["aufnr"] in spray_ha:
            div = m["kostl"][4:]
            n_chem[(_k(div, m["block_code"]), m["budat"][:7])] += spray_ha[m["aufnr"]] * dose[m["matnr"]] * verpr[m["matnr"]]

    # Deferred: the days each round sat past its interval, valued per day.
    value = _annual_value(b, av)
    loss = av["upkeep_loss_pct_per_day_overdue"] / 100.0
    done = defaultdict(list)
    for o in b["orders"]:
        if o["operation"] in ("prune", "weed", "spray") and o["status"] == "completed":
            done[(o["key"], o["activity"])].append(date.fromisoformat(o["date"]))
    deferred = defaultdict(float)
    overdue_days = defaultdict(float)
    for s in b["upkeep_state"]:
        k, act = _k(s["division_code"], s["block_code"]), s["activity"]
        interval = int(_f(s["interval_days"], 90))
        ds = sorted(done.get((k, act), []))
        last = ds[0] - timedelta(days=interval) if ds else date.fromisoformat(s["last_done"]) if s["last_done"] else WINDOW_START - timedelta(days=interval)
        if ds and ds[0] - timedelta(days=interval) > WINDOW_START:
            last = WINDOW_START - timedelta(days=int(interval * 0.9))
        i = 0
        d = WINDOW_START
        per_day = value.get(k, 0.0) * loss
        while d <= WINDOW_END:
            while i < len(ds) and ds[i] <= d:
                last = ds[i]
                i += 1
            if (d - last).days > interval:
                deferred[(k, d.isoformat()[:7])] += per_day
                overdue_days[(k, d.isoformat()[:7])] += 1
            d += timedelta(days=1)

    keys = set(wage) | set(chem) | set(n_lab) | set(deferred)
    for (k, mo) in keys:
        bl = b["blocks"].get(k)
        if not bl:
            continue
        S = wage[(k, mo)] + chem[(k, mo)]
        N = n_lab[(k, mo)] + n_chem[(k, mo)]
        uid = f"upkeep:block:{k}"
        U.unit(uid, "upkeep", "block", k, f"Block {bl['label']}", bl["division"], [bl["label"]])
        U.add(uid, mo, S=S, N=N, deferred=deferred[(k, mo)],
              drivers={"labour_over_standard": wage[(k, mo)] - n_lab[(k, mo)],
                       "chemical_over_dose": chem[(k, mo)] - n_chem[(k, mo)]},
              volume=bl["ha"], volume_unit="ha",
              peer={"basis": "Rp per hectare or palm by activity, best quarter of blocks, three months to date; "
                             "herbicide at the dose per hectare", "n": None, "dropped": [], "rate": None,
                    "quantile": q, "key": {}},
              facts={"md_over": (wage[(k, mo)] - n_lab[(k, mo)]) / md_cost if md_cost else 0.0,
                     "card_md": card_md[(k, mo)],
                     "over_pct": 100 * (chem[(k, mo)] - n_chem[(k, mo)]) / n_chem[(k, mo)] if n_chem[(k, mo)] else 0.0,
                     "overdue_days": overdue_days[(k, mo)]})


def _pest(b, av, U: Units):
    md_cost = av["man_day_cost_idr"]
    wage, chem = defaultdict(float), defaultdict(float)
    for p in b["postings"]:
        if p["block_code"] and p["racct"] in ("6103000", "6202100"):
            k = (_k(p["division_code"], p["block_code"]), p["month"])
            (wage if p["racct"] == "6103000" else chem)[k] += p["wsl"]
    census = defaultdict(list)
    for c in b["census"]:
        pos = sum(int(_f(c[f])) for f in ("ganoderma_confirmed", "ganoderma_suspect", "beetle_damaged", "rat_damaged"))
        census[_k(c["division_code"], c["block_code"])].append((c["census_date"], pos))
    treat = defaultdict(list)
    for t in b["treatments"]:
        treat[_k(t["division_code"], t["block_code"])].append(t)

    def pest_of(k, d):
        cands = treat.get(k) or []
        if not cands:
            return None
        return min(cands, key=lambda t: abs((date.fromisoformat(t["treated_date"]) - date.fromisoformat(d)).days))["pest"]

    n_lab, untrig = defaultdict(float), defaultdict(float)
    for o in b["orders"]:
        if o["operation"] != "pest" or o["qty"] <= 0:
            continue
        mo = o["date"][:7]
        if o["activity"] == "census":
            n_lab[(o["key"], mo)] += o["qty"] / av["pest_census_palms_per_man_day"] * md_cost
            continue
        prior = [p for d, p in census.get(o["key"], []) if d <= o["date"]]
        triggered = bool(prior and prior[-1] > 0) or bool(treat.get(o["key"]))
        r = PEST_RATE.get(pest_of(o["key"], o["date"]) or "ganoderma") or av["pest_treatment_palms_per_man_day"]
        if triggered:
            n_lab[(o["key"], mo)] += o["qty"] / r * md_cost
        else:
            untrig[(o["key"], mo)] += o["md"] * md_cost

    value = _annual_value(b, av)
    loss = av["pest_spread_loss_pct_per_day"] / 100.0
    deferred = defaultdict(float)
    for t in b["treatments"]:
        k = _k(t["division_code"], t["block_code"])
        due = date.fromisoformat(t["followup_due"])
        done = date.fromisoformat(t["followup_done"]) if t["followup_done"] else None
        d = max(due + timedelta(days=1), WINDOW_START)
        while d <= WINDOW_END and (done is None or d < done):
            deferred[(k, d.isoformat()[:7])] += value.get(k, 0.0) * loss
            d += timedelta(days=1)

    for (k, mo) in set(wage) | set(chem) | set(deferred):
        bl = b["blocks"].get(k)
        if not bl:
            continue
        S = wage[(k, mo)] + chem[(k, mo)]
        N = n_lab[(k, mo)] + chem[(k, mo)]
        uid = f"pest:block:{k}"
        U.unit(uid, "pest", "block", k, f"Block {bl['label']}", bl["division"], [bl["label"]])
        U.add(uid, mo, S=S, N=N, deferred=deferred[(k, mo)],
              drivers={"pest_labour": wage[(k, mo)] - n_lab[(k, mo)] - untrig[(k, mo)],
                       "untriggered_work": untrig[(k, mo)]},
              volume=bl["palms"], volume_unit="palms",
              peer={"basis": "the census and the rate card", "n": 1, "dropped": [], "rate": None,
                    "quantile": None, "key": {}},
              facts={"md_over": (wage[(k, mo)] - n_lab[(k, mo)]) / md_cost if md_cost else 0.0})


def _workforce(b, av, U: Units):
    q = av["benchmark_quantile"] / 100.0
    cutoff = av["rain_cutoff_mm"]
    order_md = defaultdict(float)
    for o in b["orders"]:
        order_md[(o["crew_code"], o["date"])] += o["md"]
    daily = defaultdict(lambda: defaultdict(float))
    for a in b["attendance"]:
        crew, d = a["crew_code"], a["date"]
        present = int(_f(a["present"]))
        un = max(0.0, present - order_md.get((crew, d), 0.0))
        x = daily[(crew, d[:7])]
        x["present"] += present
        # Spraying stops at a lighter rain than the rest of the field work.
        stop = SPRAY_RAIN_MM if a["crew_type"] == "spray" else cutoff
        wet = b["rain"].get(d, 0.0) >= stop or date.fromisoformat(d).weekday() == 6
        x["rest_md" if wet else "idle_md"] += un
        if order_md.get((crew, d), 0) > 0:
            x["work_days"] += 1
    s_un, q_un, ot = defaultdict(float), defaultdict(float), defaultdict(float)
    ot_days = defaultdict(set)
    for p in b["postings"]:
        if p["racct"] == "6105000" and p["crew_code"]:
            s_un[(p["crew_code"], p["month"])] += p["wsl"]
            q_un[(p["crew_code"], p["month"])] += p["quantity"]
        elif p["racct"] == "6101200" and p["crew_code"]:
            ot[(p["crew_code"], p["month"])] += p["wsl"]
            ot_days[(p["crew_code"], p["month"])].add(p["budat"])
    wt = {(r["crew_code"], r["month"]): r for r in b["wage_types"]}

    for mo in MONTHS:
        share, otr = defaultdict(list), defaultdict(list)
        for crew, c in b["crews"].items():
            x = daily.get((crew, mo))
            if x and x["present"]:
                share[c["crew_type"]].append(x["idle_md"] / x["present"])
                otr[c["crew_type"]].append(ot[(crew, mo)] / x["present"])
        for crew, c in b["crews"].items():
            x = daily.get((crew, mo))
            w = wt.get((crew, mo))
            if not x or not w:
                continue
            rate = s_un[(crew, mo)] / q_un[(crew, mo)] if q_un[(crew, mo)] else av["man_day_cost_idr"]
            pad_md = max(0.0, _f(w["paid_md"]) - _f(w["present_md"]))
            # Necessary idle: the crew's own support roles (mandor, kerani and, in a harvest gang,
            # everyone who is not a cutter: no cutter's order carries them), or the best quarter
            # of its type, whichever is larger.
            est = _f(c["establishment"])
            roles = (est - _f(c["harvesters"])) if c["crew_type"] == "harvest" and c["harvesters"] \
                else _f(c["mandor"]) + _f(c["kerani"])
            own_share = roles / est if est else 0.0
            b_share = max(own_share, _q(share[c["crew_type"]], q) or 0.0)
            b_ot = _q(otr[c["crew_type"]], q) or 0.0
            n_idle = x["present"] * b_share * rate
            n_rest = x["rest_md"] * rate
            n_ot = x["present"] * b_ot
            S = s_un[(crew, mo)] + ot[(crew, mo)]
            pay_over = pad_md * rate
            uid = f"workforce:crew:{crew}"
            U.unit(uid, "workforce", "crew", crew, c["name"].replace("Gang", "Gang").strip() or crew,
                   str(int(float(c["division_code"]))), extra={"crew_type": c["crew_type"]})
            U.add(uid, mo, S=S, N=n_rest + n_idle + n_ot,
                  drivers={"paid_idle": s_un[(crew, mo)] - pay_over - n_rest - n_idle,
                           "pay_over_attendance": pay_over, "overtime": ot[(crew, mo)] - n_ot},
                  volume=x["present"], volume_unit="man-days present",
                  peer={"basis": f"idle share and overtime of the best {c['crew_type']} crews, same month",
                        "n": len(share[c["crew_type"]]), "dropped": [], "rate": round(b_share * 100, 1),
                        "quantile": q, "key": {"crew_type": c["crew_type"]}},
                  facts={"ot_days": len(ot_days[(crew, mo)]), "work_days": int(x["work_days"]),
                         "ot_idr": ot[(crew, mo)], "pad_md": pad_md, "idle_md": x["idle_md"],
                         "present_md": x["present"], "idle_share": 100 * x["idle_md"] / x["present"],
                         "bench_share": 100 * b_share})


def _stores(b, av, U: Units):
    verpr = {m: _f(r["verpr"]) for m, r in b["materials"].items()}
    agg = defaultdict(lambda: defaultdict(float))
    for inv in b["invoices"]:
        a = agg[(inv["matnr"], inv["budat"][:7])]
        q_ = _f(inv["menge"])
        a["std"] += q_ * verpr.get(inv["matnr"], 0.0)
        a["inv"] += _f(inv["amount_idr"])
        a["receipts"] += 1
        a["rush"] += 1 if inv["bsart"] == "RUSH" else 0
    for p in b["postings"]:
        if p["racct"] == "6502000":
            agg[(p["matnr"], p["month"])]["wo"] += p["wsl"]
    for (m, mo), a in agg.items():
        mat = b["materials"].get(m, {})
        uid = f"stores:material:{m}"
        U.unit(uid, "stores", "material", m, f"{mat.get('maktx', m)} ({m})",
               extra={"group": mat.get("matkl")})
        U.add(uid, mo, S=a["inv"] + a["wo"], N=a["std"],
              drivers={"price_variance": a["inv"] - a["std"], "write_offs": a["wo"]},
              volume=a["std"], volume_unit="Rp received at standard",
              peer={"basis": "the standard price (MBEW)", "n": 1, "dropped": [], "rate": verpr.get(m),
                    "quantile": None, "key": {}},
              facts={"ppv": a["inv"] - a["std"], "receipts": int(a["receipts"]), "rush": int(a["rush"]),
                     "writeoff": a["wo"]})


def _roads(b, av, U: Units):
    q = av["benchmark_quantile"] / 100.0
    by_seg = defaultdict(list)
    for o in b["road_orders"]:
        by_seg[o["segment_id"]].append(o)
    seg = {}
    for sid, os_ in by_seg.items():
        r = b["roads"][sid]
        length = _f(r["length_km"])
        x = defaultdict(float)
        x["length"] = length
        culverts = sorted((o for o in os_ if o["activity"] == "culvert"), key=lambda o: o["date"])
        months = defaultdict(float)
        for o in os_:
            if not _in_window(o["date"]):
                continue
            c = _f(o["cost_idr"])
            months[o["date"][:7]] += c
            if o["activity"] == "grading":
                kmb = _f(o["km_billed"])
                rate = c / kmb if kmb else 0.0
                x["grading"] += c
                x["passes"] += 1
                x["n_grading"] += length * rate
                x["over"] += max(0.0, kmb - length) * rate
                x["km_over"] += max(0.0, kmb - length)
            else:
                prev = [p for p in culverts if p["date"] < o["date"]
                        and (date.fromisoformat(o["date"]) - date.fromisoformat(p["date"])).days <= 365]
                x["repeat" if prev else "first"] += c
        x["repairs"] = len(culverts)
        seg[sid] = (x, months)
    for sid, (x, months) in seg.items():
        r = b["roads"][sid]
        S = x["grading"] + x["first"] + x["repeat"]
        if not S:
            continue
        k = _k(r["division_code"], r["block_code"])
        bl = b["blocks"].get(k, {})
        n_grading = x["n_grading"]
        N = n_grading + x["first"]
        drivers = {"repeat_repairs": x["repeat"], "billing_over_length": x["over"],
                   "grading_cost": x["grading"] - x["over"] - n_grading}
        uid = f"roads:segment:{sid}"
        U.unit(uid, "roads", "segment", sid, f"Road {sid} · block {bl.get('label', k)}",
               str(int(float(r["division_code"]))), [bl["label"]] if bl else [],
               extra={"condition": r["condition"]})
        for mo, c in months.items():
            w = c / S
            U.add(uid, mo, S=S * w, N=N * w, drivers={d: v * w for d, v in drivers.items()},
                  volume=x["length"] * w, volume_unit="km",
                  peer={"basis": "grading at the contract rate on the segment's surveyed length; the first "
                                 "culvert repair in 12 months", "n": 1, "dropped": [], "rate": None,
                        "quantile": None, "key": {"condition": r["condition"]}},
                  facts={"repairs": int(x["repairs"]), "repeat_idr": x["repeat"], "km_over": x["km_over"],
                         "passes": int(x["passes"]), "condition": r["condition"], "window_grain": True})


def _overhead(b, av, U: Units):
    s = defaultdict(float)
    for p in b["postings"]:
        g = (b["gl"].get(p["racct"]) or {}).get("group")
        if g == "OVERHEAD" and p["rcntr"].startswith("EC-D"):
            s[(p["rcntr"][4:], p["month"])] += p["wsl"]
        elif p["racct"] == "6203000" and p["rcntr"] == "EC-GEN":
            s[("GEN", p["month"])] += p["wsl"]
    ha = defaultdict(float)
    for bl in b["blocks"].values():
        ha[bl["division"]] += bl["ha"]
    for (d, mo), v in s.items():
        if d == "GEN":
            uid = "overhead:centre:EC-GEN"
            U.unit(uid, "overhead", "centre", "EC-GEN", "Generators")
            U.add(uid, mo, S=v, N=v, volume=0.0, volume_unit="")
        else:
            uid = f"overhead:division:{d}"
            U.unit(uid, "overhead", "division", d, f"Division {d} overhead", d)
            U.add(uid, mo, S=v, N=v, volume=ha[d], volume_unit="ha",
                  facts={"idr_per_ha": v / ha[d] if ha[d] else 0.0})


# ── the computation, cached per register ──────────────────────────────────

def _finalise(meta: dict, r: dict, materiality: float) -> dict:
    """Leakage is the sum of the drivers that ran over: a saving on one never hides waste on
    another. Necessary cost counts each driver at the lower of what it cost and what its
    benchmark allows, so leakage = S + L - N still holds, and so does efficiency = N / (S + L)."""
    S, L, N_bench = r["S"], r["L"], r["N"]
    scored = meta["function"] != "overhead"
    drivers = {d: v for d, v in r["drivers"].items() if abs(v) >= 1}
    leak = sum(v for v in drivers.values() if v > 0) if scored else 0.0
    N = S + L - leak if scored else N_bench
    eff = (min(1.0, max(0.0, N / (S + L))) if (S + L) > 0 else None) if scored else None
    pos = sorted(((d, v) for d, v in drivers.items() if v > 0), key=lambda x: -x[1])
    top = pos[0][0] if pos and leak > 0 else None
    out = {"S": S, "L": L, "N": N, "N_bench": N_bench, "leakage": leak, "efficiency": eff,
           "deferred": r["deferred"],
           "drivers": drivers, "top_driver": top, "facts": r["facts"], "volume": r["volume"],
           "volume_unit": r["volume_unit"], "peer": r["peer"], "material": leak >= materiality}
    if top:
        out["why"] = pb.why(top, r["facts"])
        out["do"] = pb.do(top, meta["label"])
    else:
        out["why"] = "At or better than its benchmark" if scored and (S + L) else ""
        out["do"] = None
    return out


def _compute(av: dict) -> dict:
    b = _base()
    if not b:
        return {}
    U = Units()
    for fn in (_harvesting, _transport, _fleet, _fertiliser, _upkeep, _pest, _workforce, _stores,
               _roads, _overhead):
        try:
            fn(b, av, U)
        except Exception as exc:
            log.exception("[finance] %s failed: %s", fn.__name__, exc)
            raise
    mat = av["leak_materiality_idr"]
    rows = {key: _finalise(U.meta[key[0]], r, mat) for key, r in U.rec.items()}

    # Roll-ups. The store contributes its variance only: the material is in the blocks.
    func = defaultdict(lambda: defaultdict(float))
    for (uid, mo), r in rows.items():
        f = U.meta[uid]["function"]
        a = func[(f, mo)]
        S, N = (r["S"] - r["N"], 0.0) if f == "stores" else (r["S"], r["N"])
        a["S"] += r["S"]
        a["N"] += r["N"]
        a["S_roll"] += S
        a["N_roll"] += N
        a["L"] += r["L"]
        a["leakage"] += r["leakage"]
        a["deferred"] += r["deferred"]
        a["units"] += 1
        a["flagged"] += 1 if r["material"] else 0
    return {"meta": U.meta, "rows": rows, "func": func, "av": av}


def _state() -> dict:
    av = assumptions.values(REGISTER_KEYS)
    fp = tuple(sorted(av.items()))
    with _LOCK:
        c = _CACHE.get("calc")
    if c and c[0] == fp:
        return c[1]
    st = _compute(av)
    with _LOCK:
        _CACHE["calc"] = (fp, st)
    return st


def warm() -> None:
    def run():
        try:
            _state()
            log.info("[finance] warm")
            from gis import diagnosis
            diagnosis.warm_top()
        except Exception as exc:
            log.warning("[finance] warm-up failed: %s", exc)
    threading.Thread(target=run, name="finance-warm", daemon=True).start()


def reload() -> None:
    with _LOCK:
        _CACHE.clear()


def assumption_changed(key: str | None) -> None:
    with _LOCK:
        _CACHE.pop("calc", None)


# ── shaping ────────────────────────────────────────────────────────────────

def _month(m: str | None) -> str:
    return m if m in MONTHS else DEFAULT_MONTH


def _eff(N, SL):
    return round(100 * min(1.0, N / SL), 1) if SL > 0 else None


def _func_totals(st, mo) -> list:
    out = []
    for f in pb.FUNCTION_ORDER:
        a = st["func"].get((f, mo))
        if not a:
            continue
        out.append({"function": f, "label": pb.FUNCTIONS[f]["label"], "spend_idr": round(a["S"]),
                    "lost_idr": round(a["L"]), "necessary_idr": round(a["N"]),
                    "leakage_idr": round(a["leakage"]), "deferred_idr": round(a["deferred"]),
                    "efficiency_pct": _eff(a["N"], a["S"] + a["L"]) if f != "overhead" else None,
                    "units": int(a["units"]), "flagged": int(a["flagged"]), "scored": f != "overhead",
                    "rolls_up_as": "variance" if f == "stores" else ("not scored" if f == "overhead" else "full")})
    return out


def _estate(st, mo) -> dict:
    S = N = L = leak = dfr = spend = 0.0
    for f in pb.SCORED:
        a = st["func"].get((f, mo))
        if not a:
            continue
        S += a["S_roll"]
        N += a["N_roll"]
        L += a["L"]
        leak += a["leakage"]
        dfr += a["deferred"]
    for f in pb.FUNCTION_ORDER:
        a = st["func"].get((f, mo))
        if a and f != "stores":
            spend += a["S"]
    oh = st["func"].get(("overhead", mo))
    return {"leakage_idr": round(leak), "efficiency_pct": _eff(N, S + L), "deferred_idr": round(dfr),
            "spend_idr": round(spend + (st["func"].get(("stores", mo)) or {}).get("S_roll", 0.0)),
            "scored_spend_idr": round(S), "lost_idr": round(L), "necessary_idr": round(N),
            "overhead_idr": round(oh["S"]) if oh else 0}


def _row(st, uid, mo, rank=None) -> dict:
    m, r = st["meta"][uid], st["rows"][(uid, mo)]
    return {
        "rank": rank, "kind": "unit", "id": uid, "function": m["function"],
        "function_label": pb.FUNCTIONS[m["function"]]["label"], "unit_kind": m["unit_kind"],
        "object": m["object"], "label": m["label"], "division": m["division"], "month": mo,
        "leakage_idr": round(r["leakage"]), "efficiency_pct": _eff(r["N"], r["S"] + r["L"]) if r["efficiency"] is not None else None,
        "spend_idr": round(r["S"]), "lost_idr": round(r["L"]), "necessary_idr": round(r["N"]),
        "deferred_idr": round(r["deferred"]), "driver": r["top_driver"],
        "driver_label": pb.driver(r["top_driver"])["label"] if r["top_driver"] else None,
        "driver_idr": round(r["drivers"].get(r["top_driver"], 0.0)) if r["top_driver"] else 0,
        "why": r["why"], "do": r["do"], "blocks": m["blocks"], "material": r["material"],
        "volume": round(r["volume"], 2), "volume_unit": r["volume_unit"],
    }


def _groups(st, mo) -> tuple[list, set]:
    """Division groups: one driver tops most of a division's units AND the division stands out.

    A driver every division shows at about the same rate is how this estate runs, not a place
    to look; it stays in the node tables. A group needs the division's rate on that driver
    (driver rupiah over the division's spend in the function) at 1.5 times the median of the
    divisions, so a group on the first screen always means "this division, not the others"."""
    by = defaultdict(list)
    active = defaultdict(int)
    intensity_num = defaultdict(float)
    intensity_den = defaultdict(float)
    for (uid, m), r in st["rows"].items():
        if m != mo:
            continue
        meta = st["meta"][uid]
        if meta["function"] not in BLOCK_FUNCTIONS or not meta["division"]:
            continue
        f, d = meta["function"], meta["division"]
        active[(f, d)] += 1
        intensity_den[(f, d)] += r["S"] + r["L"]
        for drv, v in r["drivers"].items():
            if v > 0:
                intensity_num[(f, d, drv)] += v
        if r["top_driver"] and r["leakage"] > 0:
            by[(f, d, r["top_driver"])].append(uid)
    divisions = defaultdict(list)
    for (f, d), den in intensity_den.items():
        divisions[f].append(d)

    def intensity(f, d, drv):
        den = intensity_den.get((f, d), 0.0)
        return intensity_num.get((f, d, drv), 0.0) / den if den else 0.0

    groups, grouped = [], set()
    mat = st["av"]["leak_materiality_idr"]
    for (f, d, drv), uids in by.items():
        total = sum(st["rows"][(u, mo)]["leakage"] for u in uids)
        med = _q([intensity(f, x, drv) for x in divisions[f]], 0.5) or 0.0
        stands_out = intensity(f, d, drv) >= 1.5 * med and intensity(f, d, drv) > 0
        if (len(uids) >= GROUP_MIN_UNITS and len(uids) >= GROUP_MIN_SHARE * active[(f, d)]
                and total >= mat and stands_out):
            grouped.update(uids)
            rows = [st["rows"][(u, mo)] for u in uids]
            S = sum(r["S"] for r in rows)
            L = sum(r["L"] for r in rows)
            N = sum(r["N"] for r in rows)
            unit_word = {"segment": "segments"}.get(pb.FUNCTIONS[f]["unit"], "blocks")
            first = st["meta"][uids[0]]
            gid = f"group:{f}:{d}:{drv}"
            drv_idr = sum(r["drivers"].get(drv, 0.0) for r in rows)
            facts = _group_facts(st, uids, mo, drv)
            groups.append({
                "rank": None, "kind": "group", "id": gid, "function": f,
                "function_label": pb.FUNCTIONS[f]["label"], "unit_kind": "division", "object": d,
                "label": f"Division {d} · {len(uids)} {unit_word}", "division": d, "month": mo,
                "leakage_idr": round(total), "efficiency_pct": _eff(N, S + L), "spend_idr": round(S),
                "lost_idr": round(L), "necessary_idr": round(N),
                "deferred_idr": round(sum(r["deferred"] for r in rows)), "driver": drv,
                "driver_label": pb.driver(drv)["label"], "driver_idr": round(drv_idr),
                "why": pb.why(drv, facts) if facts else pb.driver(drv)["label"],
                "do": pb.do(drv, f"Division {d}"),
                "blocks": sorted({lab for u in uids for lab in st["meta"][u]["blocks"]}),
                "members": [{"id": u, "label": st["meta"][u]["label"],
                             "leakage_idr": round(st["rows"][(u, mo)]["leakage"])} for u in
                            sorted(uids, key=lambda u: -st["rows"][(u, mo)]["leakage"])],
                "material": True, "volume": None, "volume_unit": None,
                "of_units": active[(f, d)], "first_unit": first["id"],
            })
    return groups, grouped


def _group_facts(st, uids, mo, drv) -> dict:
    """The group's version of the driver's why: sums where the facts are sums, weighted where rates."""
    rows = [st["rows"][(u, mo)] for u in uids]
    fs = [r["facts"] for r in rows]
    if drv == "application_labour":
        return {"md_per_t": _q([f.get("md_per_t") for f in fs], 0.5) or 0.0,
                "std_md_per_t": fs[0].get("std_md_per_t", 0.0)}
    if drv == "late_rounds":
        return {"days_over": _q([f.get("days_over") for f in fs if f.get("cuts")], 0.5) or 0.0}
    if drv == "rate_gap":
        return {"rate_t": _q([f.get("rate_t") for f in fs], 0.5) or 0.0,
                "bench_t": _q([f.get("bench_t") for f in fs], 0.5) or 0.0}
    if drv == "billing_over_length":
        return {"km_over": sum(f.get("km_over", 0.0) for f in fs)}
    if drv == "loose_fruit":
        return {"lf_ratio": _q([f.get("lf_ratio") for f in fs], 0.5) or 0.0, "lf_bench": fs[0].get("lf_bench", 0.0)}
    if drv == "over_issue":
        return {"over_pct": _q([f.get("over_pct") for f in fs], 0.5) or 0.0}
    if drv == "chemical_over_dose":
        return {"over_pct": _q([f.get("over_pct") for f in fs], 0.5) or 0.0}
    if drv in ("labour_over_standard", "pest_labour"):
        return {"md_over": sum(f.get("md_over", 0.0) for f in fs)}
    if drv == "repeat_repairs":
        return {"repairs": sum(f.get("repairs", 0) for f in fs), "repeat_idr": sum(f.get("repeat_idr", 0.0) for f in fs)}
    return {}


def top_leaks(st, mo, top=10) -> list:
    groups, grouped = _groups(st, mo)
    units = [_row(st, uid, mo) for (uid, m), r in st["rows"].items()
             if m == mo and uid not in grouped and st["meta"][uid]["function"] != "overhead" and r["leakage"] > 0]
    rows = [r for r in groups + units if r["material"]]
    rows.sort(key=lambda r: -r["leakage_idr"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows[:top] if top else rows


def _decorate_with_diagnosis(rows: list) -> None:
    """A row whose diagnosis is cached shows the model's headline beside the template."""
    try:
        from gis import diagnosis
    except Exception:
        return
    for r in rows:
        d = diagnosis.cached(r["id"], r["month"])
        if d and d.get("available"):
            r["diagnosis_headline"] = d["diagnosis"]["headline"]
            r["diagnosis_cause"] = (d["diagnosis"].get("cause") or {}).get("label")
            acts = d["diagnosis"].get("actions") or []
            if acts:
                # The row's Do follows the likely cause once there is one.
                r["do"] = {"id": acts[0]["id"], "text": acts[0]["text"], "owner": acts[0]["owner"],
                           "unit": r["label"], "from_diagnosis": True}


# ── the endpoints' payloads ────────────────────────────────────────────────

def _unavailable() -> dict:
    return {"available": False,
            "reason": "No CO ledger. Run python gis/build_finance.py (or gis/build_synthetic.py)."}


def summary(month: str | None = None, top: int = 10) -> dict:
    if not available():
        return _unavailable()
    st = _state()
    mo = _month(month)
    est = _estate(st, mo)
    leaks = top_leaks(st, mo, top)
    _decorate_with_diagnosis(leaks)
    trend = []
    for m in MONTHS:
        e = _estate(st, m)
        trend.append({"month": m, "label": MONTH_SHORT[m], "leakage_idr": e["leakage_idr"],
                      "efficiency_pct": e["efficiency_pct"], "deferred_idr": e["deferred_idr"],
                      "days": _month_days(m), "partial": _month_days(m) < 28})
    funcs = _func_totals(st, mo)
    first = leaks[0] if leaks else None
    headline = (f"{rp(est['leakage_idr'])} leaked in {MONTH_LABEL[mo]}; estate efficiency "
                f"{est['efficiency_pct']:.0f}%."
                + (f" The largest is {first['label']} ({first['function_label'].lower()}): "
                   f"{rp(first['leakage_idr'])}, {first['why'][0].lower() + first['why'][1:] if first['why'] else ''}."
                   if first else ""))
    return {
        "available": True, "month": mo, "month_label": MONTH_LABEL[mo],
        "months": [{"month": m, "label": MONTH_LABEL[m], "short": MONTH_SHORT[m]} for m in MONTHS],
        "default_month": DEFAULT_MONTH, "headline": headline, "estate": est, "functions": funcs,
        "top": leaks, "trend": trend,
        "leaks_listed": len(top_leaks(st, mo, 0)),
        "materiality_idr": st["av"]["leak_materiality_idr"],
        "assumptions": assumptions.used(["ffb_price_idr_kg", "man_day_cost_idr", "benchmark_quantile",
                                         "leak_materiality_idr", "vehicle_hire_idr_per_hour",
                                         "ffa_idr_per_kg_point"]),
        "provenance": ("synthetic: generated SAP FI/CO records (gis/build_finance.py), posted from the "
                       "generated work orders, trips and material documents. Block identity, planted "
                       "area and the bunch and loose fruit counts underneath are the client's real figures."),
    }


def nodes(function: str, month: str | None = None, division: str | None = None) -> dict:
    if not available():
        return _unavailable()
    if function not in pb.FUNCTIONS:
        return {"available": False, "reason": f"No function {function!r}.",
                "functions": pb.FUNCTION_ORDER}
    st = _state()
    mo = _month(month)
    rows = [_row(st, uid, mo) for (uid, m) in st["rows"] if m == mo
            and st["meta"][uid]["function"] == function
            and (not division or st["meta"][uid]["division"] == str(division))]
    rows.sort(key=lambda r: (-r["leakage_idr"], -(r["spend_idr"] or 0)))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    groups, _ = _groups(st, mo)
    tot = next((f for f in _func_totals(st, mo) if f["function"] == function), None)
    drv_tot = defaultdict(float)
    for (uid, m), r in st["rows"].items():
        if m == mo and st["meta"][uid]["function"] == function:
            for d, v in r["drivers"].items():
                drv_tot[d] += v
    return {"available": True, "function": function, "label": pb.FUNCTIONS[function]["label"],
            "unit": pb.FUNCTIONS[function]["unit"], "month": mo, "month_label": MONTH_LABEL[mo],
            "totals": tot, "rows": rows,
            "groups": [g for g in groups if g["function"] == function],
            "drivers": [{"driver": d, "label": pb.driver(d)["label"], "idr": round(v)}
                        for d, v in sorted(drv_tot.items(), key=lambda x: -x[1])]}


def _documents(b, meta, mo, limit=60) -> list:
    """The postings behind one unit-month, newest first."""
    f, obj = meta["function"], meta["object"]
    accts = {"harvesting": ("6101000", "6101100"), "fertiliser": ("6201000", "6104000"),
             "upkeep": ("6102000", "6202000"), "pest": ("6103000", "6202100"),
             "fleet": ("6301000", "6204000"), "roads": ("6302000",),
             "workforce": ("6105000", "6101200"), "stores": ("6501000", "6502000"),
             "overhead": ("6401000", "6402000", "6203000")}
    out = []
    for p in b["postings"]:
        if p["month"] != mo and not (meta["function"] in ("fertiliser", "roads")):
            continue
        hit = False
        if f in ("harvesting", "fertiliser", "upkeep", "pest") and p["racct"] in accts[f]:
            hit = p["block_code"] and _k(p["division_code"], p["block_code"]) == obj
        elif f == "transport" and meta["unit_kind"] == "vehicle":
            hit = p["racct"] == "6203000" and p["vehicle_id"] == obj
        elif f == "transport":
            hit = p["racct"] == "6106000" and p["driver_id"] == obj
        elif f == "fleet":
            hit = p["racct"] in accts[f] and p["vehicle_id"] == obj
        elif f == "roads":
            hit = p["racct"] == "6302000" and p["segment_id"] == obj
        elif f == "workforce":
            hit = p["racct"] in accts[f] and p["crew_code"] == obj
        elif f == "stores":
            hit = p["racct"] in accts[f] and p["matnr"] == obj
        elif f == "overhead":
            hit = (p["racct"] in ("6401000", "6402000") and p["rcntr"] == f"EC-D{obj}") or \
                  (obj == "EC-GEN" and p["rcntr"] == "EC-GEN")
        if hit:
            out.append(p)
    out.sort(key=lambda p: (p["budat"], p["belnr"]), reverse=True)
    return [{k: p[k] for k in ("belnr", "budat", "racct", "rcntr", "aufnr", "vehicle_id", "driver_id",
                               "crew_code", "matnr", "lifnr", "wsl", "quantity", "unit", "src_type", "src_id")}
            | {"account": (b["gl"].get(p["racct"]) or {}).get("txt", p["racct"])}
            for p in out[:limit]], len(out)


def node(uid: str, month: str | None = None) -> dict:
    if not available():
        return _unavailable()
    st = _state()
    mo = _month(month)
    if uid.startswith("group:"):
        return _group_node(st, uid, mo)
    meta = st["meta"].get(uid)
    if not meta:
        return {"available": False, "reason": f"No unit {uid!r}."}
    r = st["rows"].get((uid, mo))
    trend = []
    for m in MONTHS:
        x = st["rows"].get((uid, m))
        trend.append({"month": m, "label": MONTH_SHORT[m], "leakage_idr": round(x["leakage"]) if x else 0,
                      "spend_idr": round(x["S"]) if x else 0,
                      "efficiency_pct": _eff(x["N"], x["S"] + x["L"]) if x and x["efficiency"] is not None else None})
    if not r:
        return {"available": True, "id": uid, **{k: meta[k] for k in ("function", "unit_kind", "object", "label", "division", "blocks")},
                "function_label": pb.FUNCTIONS[meta["function"]]["label"], "month": mo,
                "month_label": MONTH_LABEL[mo], "active": False, "trend": trend,
                "sentence": f"{meta['label']} has no postings in {MONTH_LABEL[mo]}."}
    docs, ndocs = _documents(_base(), meta, mo)
    drivers = [{"driver": d, "label": pb.driver(d)["label"], "idr": round(v),
                "kind": "loss" if d in ("loose_fruit", "late_rounds", "queue_ffa", "shrinkage") else "spend"}
               for d, v in sorted(r["drivers"].items(), key=lambda x: -x[1])]
    row = _row(st, uid, mo)
    sentence = (f"{meta['label']} leaked {rp(r['leakage'])} in {MONTH_LABEL[mo]} at "
                f"{row['efficiency_pct']:.0f}% efficiency. {r['why']}." if r["leakage"] > 0 and row["efficiency_pct"] is not None
                else f"{meta['label']} ran at or better than its benchmark in {MONTH_LABEL[mo]}.")
    return {
        "available": True, "active": True, **row, "month_label": MONTH_LABEL[mo],
        "sentence": sentence, "drivers": drivers, "facts": {k: (round(v, 2) if isinstance(v, float) else v)
                                                           for k, v in r["facts"].items()},
        "peer": r["peer"], "trend": trend, "documents": docs, "documents_total": ndocs,
        "actions": [dict(a) for a in pb.driver(r["top_driver"])["actions"]] if r["top_driver"] else [],
        "saving_idr_month": round(r["drivers"].get(r["top_driver"], 0.0)) if r["top_driver"] else 0,
    }


def _group_node(st, gid, mo) -> dict:
    groups, _ = _groups(st, mo)
    g = next((x for x in groups if x["id"] == gid), None)
    if not g:
        _, f, d, drv = gid.split(":")
        return {"available": True, "active": False, "id": gid, "kind": "group", "function": f,
                "function_label": pb.FUNCTIONS.get(f, {}).get("label", f), "label": f"Division {d}",
                "month": mo, "month_label": MONTH_LABEL[mo],
                "sentence": f"No division-wide {pb.driver(drv)['label'].lower()} in {MONTH_LABEL[mo]}.",
                "trend": []}
    drv_tot = defaultdict(float)
    for m in g["members"]:
        for d, v in st["rows"][(m["id"], mo)]["drivers"].items():
            drv_tot[d] += v
    trend = []
    _, f, d, drv = gid.split(":")
    for m in MONTHS:
        gs, _x = _groups(st, m)
        x = next((y for y in gs if y["id"] == gid), None)
        trend.append({"month": m, "label": MONTH_SHORT[m], "leakage_idr": x["leakage_idr"] if x else 0,
                      "efficiency_pct": x["efficiency_pct"] if x else None, "spend_idr": x["spend_idr"] if x else 0})
    return {**g, "available": True, "active": True, "month_label": MONTH_LABEL[mo],
            "sentence": (f"{g['label']}: {rp(g['leakage_idr'])} leaked in {MONTH_LABEL[mo]}, "
                         f"{g['driver_label'].lower()} the largest part. {g['why']}."),
            "drivers": [{"driver": k, "label": pb.driver(k)["label"], "idr": round(v),
                         "kind": "loss" if k in ("loose_fruit", "late_rounds", "queue_ffa", "shrinkage") else "spend"}
                        for k, v in sorted(drv_tot.items(), key=lambda x: -x[1])],
            "trend": trend, "facts": {}, "peer": None, "documents": [], "documents_total": 0,
            "actions": [dict(a) for a in pb.driver(drv)["actions"]],
            "saving_idr_month": g["driver_idr"]}


def ledger(account=None, cc=None, order=None, date_from=None, date_to=None, unit=None,
           limit=100, offset=0) -> dict:
    if not available():
        return _unavailable()
    b = _base()
    rows = b["postings"]
    if account:
        accts = set(account.split(","))
        rows = [p for p in rows if p["racct"] in accts]
    if cc:
        rows = [p for p in rows if p["rcntr"] == cc]
    if order:
        rows = [p for p in rows if p["aufnr"] == order]
    if date_from:
        rows = [p for p in rows if p["budat"] >= date_from]
    if date_to:
        rows = [p for p in rows if p["budat"] <= date_to]
    if unit:
        st = _state()
        meta = st["meta"].get(unit)
        if meta:
            rows = _unit_filter(b, meta, rows)
    rows = sorted(rows, key=lambda p: (p["budat"], p["belnr"]), reverse=True)
    page = rows[offset:offset + limit]
    return {"available": True, "total": len(rows), "offset": offset, "limit": limit,
            "total_idr": round(sum(p["wsl"] for p in rows)),
            "rows": [{**{k: p[k] for k in ("belnr", "budat", "racct", "rcntr", "aufnr", "division_code",
                                           "block_code", "vehicle_id", "driver_id", "segment_id", "crew_code",
                                           "matnr", "lifnr", "wsl", "quantity", "unit", "src_type", "src_id")},
                      "account": (b["gl"].get(p["racct"]) or {}).get("txt", p["racct"])} for p in page],
            "accounts": [{"racct": a, "txt": g["txt"], "group": g["group"]} for a, g in sorted(b["gl"].items())]}


def _unit_filter(b, meta, rows) -> list:
    keep = []
    for mo in MONTHS:
        docs, _ = _documents(b, meta, mo, 100_000)
        keep += [d["belnr"] for d in docs]
    ks = set(keep)
    return [p for p in rows if p["belnr"] in ks]


# ── budget variance: the client's current view ─────────────────────────────

def _budget_lines(b, mo=None) -> list:
    actual = defaultdict(float)
    for p in b["postings"]:
        g = (b["gl"].get(p["racct"]) or {}).get("group")
        if not g or g == "REVENUE":
            continue
        if mo and p["month"] != mo:
            continue
        obj = p["aufnr"] or p["rcntr"]
        if g == "ROADS":
            obj = p["rcntr"]                  # roads are budgeted per division
        actual[(p["month"], obj, g)] += p["wsl"]
    lines = []
    for r in b["budget"]:
        if mo and r["month"] != mo:
            continue
        obj = r["aufnr"] or r["rcntr"]
        a = actual.pop((r["month"], obj, r["group"]), 0.0)
        lines.append({"month": r["month"], "object": obj, "group": r["group"], "budget_idr": _f(r["budget_idr"]),
                      "actual_idr": a})
    for (m, obj, g), a in actual.items():
        lines.append({"month": m, "object": obj, "group": g, "budget_idr": 0.0, "actual_idr": a})
    return lines


def _flag(line, mat) -> bool:
    over = line["actual_idr"] - line["budget_idr"]
    return over >= mat and (line["budget_idr"] <= 0 or over / line["budget_idr"] > 0.10)


def budget(month: str | None = None) -> dict:
    if not available():
        return _unavailable()
    b = _base()
    mo = _month(month)
    mat = assumptions.get("leak_materiality_idr")
    lines = _budget_lines(b, mo)
    for l in lines:
        l["variance_idr"] = round(l["actual_idr"] - l["budget_idr"])
        l["variance_pct"] = round(100 * l["variance_idr"] / l["budget_idr"], 1) if l["budget_idr"] else None
        l["flagged"] = _flag(l, mat)
        l["budget_idr"] = round(l["budget_idr"])
        l["actual_idr"] = round(l["actual_idr"])
    by_group = defaultdict(lambda: {"budget_idr": 0, "actual_idr": 0, "flagged": 0, "lines": 0})
    for l in lines:
        g = by_group[l["group"]]
        g["budget_idr"] += l["budget_idr"]
        g["actual_idr"] += l["actual_idr"]
        g["flagged"] += 1 if l["flagged"] else 0
        g["lines"] += 1
    flagged = sorted((l for l in lines if l["flagged"]), key=lambda l: -l["variance_idr"])
    return {"available": True, "month": mo, "month_label": MONTH_LABEL[mo],
            "rule": "over budget by more than 10% and by at least the materiality threshold",
            "materiality_idr": mat,
            "groups": [{"group": g, **v, "variance_idr": v["actual_idr"] - v["budget_idr"]}
                       for g, v in sorted(by_group.items())],
            "flagged": flagged[:200], "flagged_total": len(flagged), "lines": len(lines)}


# ── the map ────────────────────────────────────────────────────────────────

def block_metrics(month: str | None = None) -> dict:
    """Per block: efficiency and leakage per hectare over the block functions, for the map."""
    if not available():
        return {}
    st = _state()
    b = _base()
    months = [month] if month in MONTHS else list(MONTHS)
    agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for (uid, mo), r in st["rows"].items():
        meta = st["meta"][uid]
        if mo not in months or meta["function"] not in ("harvesting", "fertiliser", "upkeep", "pest"):
            continue
        a = agg[meta["object"]]
        a[0] += r["N"]
        a[1] += r["S"] + r["L"]
        a[2] += r["leakage"]
    out = {}
    for k, (N, SL, leak, _) in agg.items():
        bl = b["blocks"].get(k)
        if not bl:
            continue
        out[bl["id"]] = {"efficiency_pct": _eff(N, SL),
                         "leakage_idr_per_ha": round(leak / bl["ha"]) if bl["ha"] else None}
    return out


# ── accuracy: the engine against the answer key and against budget variance ─

def _key_rows() -> list:
    return _read("ec_fi_leak_key.csv")


def _rank_in(st, function, uid, mo, unit_kind=None) -> int | None:
    rs = [(u, r) for (u, m), r in st["rows"].items() if m == mo and st["meta"][u]["function"] == function
          and (unit_kind is None or st["meta"][u]["unit_kind"] == unit_kind)]
    rs.sort(key=lambda x: -x[1]["leakage"])
    for i, (u, r) in enumerate(rs):
        if u == uid:
            return i + 1 if r["leakage"] > 0 else None
    return None


def _window_ranking(st, function) -> list:
    tot = {}
    for (u, m), r in st["rows"].items():
        if st["meta"][u]["function"] == function:
            tot.setdefault(u, None)
    ranked = [(u, _unit_window(st, u)) for u in tot]
    ranked.sort(key=lambda x: -x[1]["leakage"])
    return ranked


def _unit_window(st, uid) -> dict:
    out = {"S": 0.0, "L": 0.0, "N": 0.0, "leakage": 0.0, "drivers": defaultdict(float)}
    for m in MONTHS:
        r = st["rows"].get((uid, m))
        if r:
            for k in ("S", "L", "N", "leakage"):
                out[k] += r[k]
            for d, v in r["drivers"].items():
                out["drivers"][d] += v
    return out


def accuracy() -> dict:
    if not available():
        return _unavailable()
    st = _state()
    b = _base()
    key = _key_rows()
    mat = st["av"]["leak_materiality_idr"]
    top20 = {mo: top_leaks(st, mo, 20) for mo in MONTHS}
    checks = []
    found_engine = {}

    def unit_id(k):
        return {"vehicle": f"{k['function']}:vehicle:{k['unit_id']}",
                "driver": f"transport:driver:{k['unit_id']}", "crew": f"workforce:crew:{k['unit_id']}",
                "material": f"stores:material:{k['unit_id']}", "segment": f"roads:segment:{k['unit_id']}"}.get(k["unit_kind"])

    for k in key:
        if k["kind"] not in ("planted", "counterfactual") and k["leak_id"] != "shrink_d07":
            continue
        months = k["months"].split(";")
        ok_months, detail = 0, []
        if k["unit_kind"] == "division":
            gid = f"group:{k['function']}:{k['unit_id']}:{k['driver']}"
            others = 0
            for mo in months:
                gs, _ = _groups(st, mo)
                hit = any(g["id"] == gid for g in gs)
                others += sum(1 for g in gs if g["function"] == k["function"] and g["driver"] == k["driver"]
                              and g["id"] != gid)
                ok_months += hit
                detail.append({"month": mo, "group_found": hit})
            ok = ok_months >= max(1, len(months) - 1) and others == 0
            want = f"Division {k['unit_id']} grouped on {k['driver']} in its months, no other division"
            got = f"{ok_months}/{len(months)} months, {others} other division groups"
        elif k["function"] in ("fleet", "roads", "workforce") or k["kind"] == "counterfactual":
            # Repairs and road works are lumpy, and a payroll leak recurs across 67 crews whose
            # monthly ranks are noisy: these are judged over the window.
            uid = unit_id(k)
            ranked = _window_ranking(st, k["function"])
            rank = next((i + 1 for i, (u, _w) in enumerate(ranked) if u == uid), None)
            w = dict(ranked).get(uid) or {}
            top = max(((d, v) for d, v in (w.get("drivers") or {}).items() if v > 0),
                      key=lambda x: x[1], default=(None, 0))[0]
            # Counterfactuals exist to test the diagnosis; the engine must surface them with the
            # right driver, in the top ten of their function.
            limit = 10 if k["kind"] == "counterfactual" else 5 if k["function"] == "workforce" else 1
            ok = bool(rank and rank <= limit and top == k["driver"])
            detail.append({"month": "window", "rank": rank, "driver": top,
                           "leakage_idr": round(w.get("leakage", 0.0))})
            want = f"top {limit} in {pb.FUNCTIONS[k['function']]['label'].lower()} over the window, driver {k['driver']}"
            got = f"rank {rank}, driver {top}"
        else:
            uid = unit_id(k)
            limit = {"workforce": 5, "stores": 3}.get(k["function"], 3)
            if k["unit_kind"] == "driver":
                limit = 1
            for mo in months:
                r = st["rows"].get((uid, mo))
                rank = _rank_in(st, k["function"], uid, mo, st["meta"].get(uid, {}).get("unit_kind"))
                hit = bool(r and rank and rank <= limit and r["top_driver"] == k["driver"])
                ok_months += hit
                detail.append({"month": mo, "rank": rank, "driver": r["top_driver"] if r else None,
                               "leakage_idr": round(r["leakage"]) if r else 0})
            ok = ok_months >= max(1, len(months) - 1)
            want = f"top {limit} in {pb.FUNCTIONS[k['function']]['label'].lower()}, driver {k['driver']}"
            got = f"{ok_months}/{len(months)} months"
        found_engine[k["leak_id"]] = ok
        checks.append({"id": k["leak_id"], "kind": k["kind"], "function": k["function"],
                       "unit": k["unit_id"], "driver": k["driver"], "planted_idr": _f(k["planted_idr"]) or None,
                       "want": want, "got": got, "passes": ok, "months": detail})

    # Loose fruit and queue FFA: the existing modules' method, on this window.
    lf = sum(r["drivers"].get("loose_fruit", 0.0) for (u, m), r in st["rows"].items())
    qf = sum(r["drivers"].get("queue_ffa", 0.0) for (u, m), r in st["rows"].items())
    checks.append({"id": "loose_fruit", "kind": "existing", "function": "harvesting", "unit": "",
                   "driver": "loose_fruit", "want": "priced on the best-quarter method, over the window",
                   "got": rp(lf), "passes": lf > 0, "months": []})
    checks.append({"id": "queue_ffa", "kind": "existing", "function": "transport", "unit": "",
                   "driver": "queue_ffa", "want": "priced from the FFA panel's fitted slope",
                   "got": rp(qf), "passes": qf > 0, "months": []})

    decoys = _decoys(st, b, top20)
    checks += decoys["checks"]
    calib = _calibration(st)
    comparison = _comparison(st, b, key, found_engine, decoys, mat)
    return {"available": True, "checks": checks, "diagnosis_eval": _diagnosis_eval(), "ledger": _ledger_checks(),
            "passed": sum(1 for c in checks if c["passes"]), "total": len(checks),
            "calibration": calib, "comparison": comparison, "benchmarks": _benchmarks(st),
            "coverage": _coverage(st, b),
            "rules": ["Rupiah ranks; efficiency only reads.",
                      "Necessary cost is the best quarter of true peers, or a hard spec where one exists.",
                      "Spending less is not efficiency: overdue work is Deferred, never a better score.",
                      "Each loss has one owner; the store rolls up as its variance only.",
                      "The answer key is read here, to score the engine, and nowhere else."]}


def _ledger_checks() -> dict | None:
    """The generator's reconciliation (build_finance._checks), as written to the manifest."""
    import json
    from pathlib import Path
    p = Path(__file__).parent / "data" / "synthetic" / "manifest.json"
    try:
        c = json.loads(p.read_text(encoding="utf-8")).get("finance_checks")
    except (OSError, ValueError):
        return None
    if c:
        c = dict(c, all_pass=all(v for v in c.values() if isinstance(v, bool)))
    return c


def _diagnosis_eval() -> dict | None:
    """The latest diagnosis eval run (evals/run_leak_diagnosis.py), if one has been made."""
    import json
    from pathlib import Path
    p = Path(__file__).parent.parent / "evals" / "results" / "leak_diagnosis_latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _decoys(st, b, top20) -> dict:
    checks = []
    in_top = {r["id"] for rows in top20.values() for r in rows} | {
        m["id"] for rows in top20.values() for r in rows for m in r.get("members", [])}
    # Terrain: the hardest-ground tenth of blocks (sparse crop, steep, far) cost more per tonne
    # honestly. Peers must absorb that: they stay off the list as units, and their efficiency
    # sits near the estate's.
    singled = {r["id"] for rows in top20.values() for r in rows if r["kind"] == "unit"}
    hv = [u for u, m in st["meta"].items() if m["function"] == "harvesting"]
    hard = sorted(hv, key=lambda u: -((lambda p: p["slope"] + p["distance"] + (2 - p["density"]))(
        b["blocks"][st["meta"][u]["object"]]["peer"])))[:max(1, len(hv) // 10)]
    win = {u: _unit_window(st, u) for u in hv}

    def raw_rate(u):
        S = sum(st["rows"][(u, m)]["S"] - st["rows"][(u, m)]["facts"].get("premium_idr", 0.0)
                for m in MONTHS if (u, m) in st["rows"])
        t = sum(st["rows"][(u, m)]["facts"].get("tonnes", 0.0) for m in MONTHS if (u, m) in st["rows"])
        return S / t if t else None

    def wage_eff(u):
        # The cost side only: cutters' wages against the peer rate. Crop lost on far blocks
        # (late rounds) is a real loss, not terrain making the work dearer.
        S = sum(st["rows"][(u, m)]["S"] for m in MONTHS if (u, m) in st["rows"])
        gap = sum(max(0.0, st["rows"][(u, m)]["drivers"].get("rate_gap", 0.0)) for m in MONTHS
                  if (u, m) in st["rows"])
        return 100 * (S - gap) / S if S else None
    med_all = _q([wage_eff(u) for u in hv], 0.5) or 0.0
    med_hard = _q([wage_eff(u) for u in hard], 0.5) or 0.0
    raw_all = _q([raw_rate(u) for u in hv], 0.5) or 0.0
    raw_hard = _q([raw_rate(u) for u in hard], 0.5) or 0.0
    listed = [u for u in hard if u in singled]
    ok = len(listed) <= max(1, len(hard) // 10) and abs(med_hard - med_all) <= 5
    checks.append({"id": "decoy_terrain", "kind": "decoy", "function": "harvesting", "unit": "",
                   "driver": "",
                   "want": ("the hardest-ground tenth of blocks: not singled out in the top 20, and wage "
                            "efficiency within 5 points of the estate's"),
                   "got": (f"Rp {raw_hard:,.0f} a tonne raw against Rp {raw_all:,.0f}; {len(listed)} of {len(hard)} "
                           f"singled out; median wage efficiency {med_hard:.1f}% against {med_all:.1f}%"),
                   "passes": ok, "months": [], "units": hard[:5]})
    dear = hard
    terrain_flag = not ok
    # Prescriptions: weak blocks get more; the engine must not read that as a leak.
    fert = {u: r for (u, mo), r in st["rows"].items() if st["meta"][u]["function"] == "fertiliser"}
    presc = {u: r["facts"].get("kg_per_palm_prescribed", 0.0) for u, r in fert.items()}
    hi = set(sorted(presc, key=lambda u: -presc[u])[:max(1, len(presc) // 4)])

    def flagged(u):
        w = _unit_window(st, u)
        return w["drivers"].get("over_issue", 0.0) > 0.05 * max(w["S"], 1)
    hi_rate = sum(flagged(u) for u in hi) / max(len(hi), 1)
    lo = set(presc) - hi
    lo_rate = sum(flagged(u) for u in lo) / max(len(lo), 1)
    checks.append({"id": "decoy_prescribed", "kind": "decoy", "function": "fertiliser", "unit": "",
                   "driver": "", "want": "the most heavily prescribed blocks are not flagged more often",
                   "got": f"{hi_rate:.0%} of the top quarter flagged for over-issue against {lo_rate:.0%} of the rest",
                   "passes": hi_rate <= lo_rate + 0.10, "months": [], "units": sorted(hi)[:5]})
    # Lebaran: workforce leakage per man-day, unplanted crews, Mar-Apr against Jan-Feb.
    planted_units = {"workforce:crew:G3-02", "workforce:crew:U4-02"}

    def wf(months):
        s = md = 0.0
        for (u, mo), r in st["rows"].items():
            if mo in months and st["meta"][u]["function"] == "workforce" and u not in planted_units:
                s += r["leakage"]
                md += r["volume"]
        return s / md if md else 0.0
    a, z = wf(("2025-01", "2025-02")), wf(("2025-03", "2025-04"))
    leb_ok = a == 0 or (z - a) / a <= 0.30
    checks.append({"id": "decoy_lebaran", "kind": "decoy", "function": "workforce", "unit": "",
                   "driver": "", "want": "no Lebaran spike in workforce leakage per man-day (not more than 30% up)",
                   "got": f"Rp {a:,.0f} a man-day in Jan-Feb, Rp {z:,.0f} in Mar-Apr",
                   "passes": leb_ok, "months": []})
    # Skipped upkeep: the most overdue blocks carry Deferred, and skipping earns no better score.
    up = defaultdict(float)
    for (u, mo), r in st["rows"].items():
        if st["meta"][u]["function"] == "upkeep":
            up[u] += r["deferred"]
    worst = sorted(up, key=lambda u: -up[u])[:10]
    up_eff = {u: _eff(w["N"], w["S"] + w["L"]) for u, w in
              ((u, _unit_window(st, u)) for u in up)}
    med_up = _q([v for v in up_eff.values() if v is not None], 0.5) or 0.0
    med_worst = _q([up_eff[u] for u in worst if up_eff.get(u) is not None], 0.5) or 0.0
    lifted = med_worst > med_up + 5
    checks.append({"id": "decoy_skipped_upkeep", "kind": "decoy", "function": "upkeep", "unit": "",
                   "driver": "", "want": "the ten most overdue blocks show Deferred, and skipping earns no better score",
                   "got": (f"{sum(1 for u in worst if up[u] > 0)} of 10 with Deferred; their median efficiency "
                           f"{med_worst:.1f}% against {med_up:.1f}% for all upkeep blocks"),
                   "passes": all(up[u] > 0 for u in worst) and not lifted, "months": [], "units": worst[:5]})
    listed = [u for u in worst if lifted]
    return {"checks": checks, "dear_blocks": dear, "prescribed": sorted(hi), "overdue": worst,
            "engine_flags": {"decoy_terrain": terrain_flag,
                             "decoy_prescribed": hi_rate > lo_rate + 0.10,
                             "decoy_lebaran": not leb_ok,
                             "decoy_skipped_upkeep": bool(listed)}}


def _calibration(st) -> dict:
    effs = []
    for f in pb.SCORED:
        if f == "stores":
            continue
        for (u, mo), r in st["rows"].items():
            if st["meta"][u]["function"] == f and r["efficiency"] is not None and r["S"] + r["L"] > 0:
                effs.append(100 * r["efficiency"])
    med = _q(effs, 0.5)
    return {"median_unit_efficiency_pct": round(med, 1) if med is not None else None,
            "band": [80, 92], "in_band": med is not None and 80 <= med <= 92, "units": len(effs)}


def _benchmarks(st) -> list:
    out = []
    seen = set()
    for (u, mo), r in st["rows"].items():
        f = st["meta"][u]["function"]
        p = r.get("peer") or {}
        k = (f, p.get("basis"))
        if k in seen or not p:
            continue
        seen.add(k)
        dropped = sum(1 for (u2, m2), r2 in st["rows"].items()
                      if st["meta"][u2]["function"] == f and (r2.get("peer") or {}).get("dropped"))
        out.append({"function": f, "label": pb.FUNCTIONS[f]["label"], "basis": p.get("basis"),
                    "quantile": p.get("quantile"), "unit_months_with_dimension_dropped": dropped})
    return out


def _coverage(st, b) -> dict:
    total = sum(p["wsl"] for p in b["postings"] if p["racct"] != "4101000")
    assigned = 0.0
    for (u, mo), r in st["rows"].items():
        if st["meta"][u]["function"] != "stores":
            assigned += r["S"]
    stores_var = sum(p["wsl"] for p in b["postings"] if p["racct"] in ("6501000", "6502000"))
    spare_unlinked = sum(p["wsl"] for p in b["postings"] if p["racct"] == "6204000" and not p["vehicle_id"])
    return {"ledger_cost_idr": round(total), "on_nodes_idr": round(assigned + stores_var),
            "share_pct": round(100 * (assigned + stores_var) / total, 1) if total else None,
            "workshop_stock_unlinked_idr": round(spare_unlinked)}


def _comparison(st, b, key, found_engine, decoys, mat) -> dict:
    """The client's current method, budget variance, against the leak engine on the same world."""
    lines = _budget_lines(b)
    flagged = {(l["month"], l["object"], l["group"]) for l in lines if _flag(l, mat)}
    crews_blocks = defaultdict(set)
    for p in b["postings"]:
        if p["racct"] == "6101200" and p["crew_code"]:
            crews_blocks[p["crew_code"]].add(p["aufnr"])
    rot = defaultdict(set)
    for k, bl in b["blocks"].items():
        rot[bl["division"]].add(f"BLK-{k.replace('|', '-')}")
    road_div = {sid: f"EC-D{int(float(r['division_code']))}" for sid, r in b["roads"].items()}

    def objects(k):
        u, f = k["unit_id"], k["function"]
        if k["leak_id"] in ("fuel_tr04", "tank_tr06"):
            return [(f"VEH-{u}", "FUEL")]
        if k["leak_id"] in ("ot_g302", "ot_g501"):
            return [(o, "HARVEST") for o in crews_blocks[u]]
        if k["leak_id"] in ("pay_u402", "pay_u603"):
            return [(f"EC-D{b['crews'][u]['division_code'].split('.')[0]}", "LABOUR")]
        if k["leak_id"] == "fert_app_d5":
            return [(o, "FERTILISER") for o in rot[u]]
        if k["leak_id"] == "price_urea":
            return [("EC-STORE", "PRICE")]
        if k["leak_id"] == "fleet_du01":
            return [(f"VEH-{u}", "FLEET")]
        if k["leak_id"] == "culvert_repeat":
            return [(road_div.get(u, ""), "ROADS")]
        if k["leak_id"] == "grading_d6":
            return [(f"EC-D{u}", "ROADS")]
        return []                                   # losses are never posted: budgets cannot see them

    rows = []
    for k in key:
        if k["kind"] == "decoy" or k["leak_id"] in ("loose_fruit", "queue_ffa"):
            continue
        months = k["months"].split(";")
        objs = objects(k)
        hits = 0
        for mo in months:
            if objs and sum(1 for o, g in objs if (mo, o, g) in flagged) >= max(1, len(objs) // 2):
                hits += 1
        budget_found = bool(objs) and hits >= max(1, len(months) - 1)
        rows.append({"id": k["leak_id"], "function": k["function"], "unit": k["unit_id"],
                     "planted_idr": _f(k["planted_idr"]) or None,
                     "budget_variance": budget_found, "leak_engine": bool(found_engine.get(k["leak_id"]))})
    # Decoys: does each method raise a false alarm?
    dear_objs = [f"BLK-{st['meta'][u]['object'].replace('|', '-')}" for u in decoys["dear_blocks"]]
    terrain_budget = sum(1 for o in dear_objs if any((mo, o, "HARVEST") in flagged for mo in MONTHS))
    presc_objs = [f"BLK-{st['meta'][u]['object'].replace('|', '-')}" for u in decoys["prescribed"]]
    presc_budget = sum(1 for o in presc_objs if any((mo, o, "FERTILISER") in flagged for mo in MONTHS))
    leb = sum(1 for l in lines if l["month"] in ("2025-03", "2025-04") and l["group"] == "LABOUR" and _flag(l, mat))
    leb0 = sum(1 for l in lines if l["month"] in ("2025-01", "2025-02") and l["group"] == "LABOUR" and _flag(l, mat))
    decoy_rows = [
        {"id": "decoy_terrain", "budget_variance": terrain_budget > len(dear_objs) // 4,
         "leak_engine": decoys["engine_flags"]["decoy_terrain"],
         "note": f"{terrain_budget} of {len(dear_objs)} dearest blocks over budget"},
        {"id": "decoy_prescribed", "budget_variance": presc_budget > len(presc_objs) // 4,
         "leak_engine": decoys["engine_flags"]["decoy_prescribed"],
         "note": f"{presc_budget} of {len(presc_objs)} most-prescribed blocks over budget"},
        {"id": "decoy_lebaran", "budget_variance": leb > leb0 + 2, "leak_engine": decoys["engine_flags"]["decoy_lebaran"],
         "note": f"{leb} labour lines over budget in Mar-Apr, {leb0} in Jan-Feb"},
        {"id": "decoy_skipped_upkeep", "budget_variance": False,
         "leak_engine": decoys["engine_flags"]["decoy_skipped_upkeep"],
         "note": "under budget, so budget variance raises nothing; the engine shows it as Deferred"},
    ]
    located_engine = sum(r["planted_idr"] or 0 for r in rows if r["leak_engine"])
    located_budget = sum(r["planted_idr"] or 0 for r in rows if r["budget_variance"])
    total = sum(r["planted_idr"] or 0 for r in rows)
    flagged_lines = sum(1 for l in lines if _flag(l, mat))
    return {
        "planted": rows, "decoys": decoy_rows,
        "found": {"budget_variance": sum(r["budget_variance"] for r in rows),
                  "leak_engine": sum(r["leak_engine"] for r in rows), "of": len(rows)},
        "false_alarms": {"budget_variance": sum(d["budget_variance"] for d in decoy_rows),
                         "leak_engine": sum(d["leak_engine"] for d in decoy_rows), "of": len(decoy_rows)},
        "located_idr": {"budget_variance": round(located_budget), "leak_engine": round(located_engine),
                        "planted": round(total)},
        "budget_lines_flagged": flagged_lines, "budget_lines": len(lines),
        "rule": "budget variance: an object and cost-element group over budget by more than 10% and the "
                "materiality threshold, in most of the leak's months",
    }


# ── actions: the drafted document and the reading afterwards ───────────────

def action_document(uid: str, month: str | None = None, action_id: str | None = None) -> dict:
    """What a cost_action is drafted from: the same figures the window shows."""
    st = _state()
    mo = _month(month)
    if uid.startswith("group:"):
        g = _group_node(st, uid, mo)
        if not g.get("active"):
            return {"available": False, "reason": "No leak on this group this month."}
        unit = {"id": uid, "label": g["label"], "function": g["function"],
                "function_label": g["function_label"]}
        drv, leak, drv_idr, why = g["driver"], g["leakage_idr"], g["driver_idr"], g["why"]
    else:
        um = unit_month(uid, mo)
        if not um or not um["row"]["top_driver"]:
            return {"available": False, "reason": "No leak on this unit this month."}
        p = um["public"]
        unit = {"id": uid, "label": p["label"], "function": p["function"], "function_label": p["function_label"]}
        drv, leak, drv_idr, why = p["driver"], p["leakage_idr"], p["driver_idr"], p["why"]
    acts = pb.driver(drv)["actions"]
    act = next((a for a in acts if a["id"] == action_id), acts[0])
    cause = confirm = None
    try:
        from gis import diagnosis
        d = diagnosis.cached(uid, mo)
        if d and d.get("available"):
            cause = d["diagnosis"]["cause"]["label"]
            confirm = d["diagnosis"]["confirm"]
            hit = next((a for a in d["diagnosis"]["actions"] if a["id"] == act["id"]), None)
            if hit:
                act = {**act, "text": hit["text"]}
    except Exception:
        pass
    y, m = int(mo[:4]), int(mo[5:7])
    review = date(y + (m == 12), m % 12 + 1, 28).isoformat()
    return {"available": True, **unit, "month": mo, "month_label": MONTH_LABEL[mo], "driver": drv,
            "driver_label": pb.driver(drv)["label"], "leakage_idr": leak, "saving_idr_month": drv_idr,
            "why": why, "action_id": act["id"], "action": act["text"], "owner": act["owner"],
            "document": act.get("document"), "watch": act.get("watch"), "cause": cause, "confirm": confirm,
            "review_date": review}


def outcomes() -> dict:
    """Did it work: each accepted cost action's leakage on its driver, before and after.

    The decision is taken while looking at a month inside the window, so the
    reading replays the months after it, the way ops outcomes replay a plan."""
    from gis import decisions
    st = _state()
    rows = []
    for r in decisions.history(500)["decisions"]:
        if r.get("artifact_kind") != "cost_action" or r.get("action") != "accepted":
            continue
        e = r.get("expected_effect") or {}
        uid, drv, mo = e.get("unit_id"), e.get("driver"), e.get("month")
        if not uid or mo not in MONTHS:
            continue

        def reading(m):
            if uid.startswith("group:"):
                gs, _ = _groups(st, m)
                g = next((x for x in gs if x["id"] == uid), None)
                return g["driver_idr"] if g else 0
            x = st["rows"].get((uid, m))
            return round(max(0.0, x["drivers"].get(drv, 0.0))) if x else 0
        after = [{"month": m, "label": MONTH_SHORT[m], "driver_idr": reading(m)} for m in MONTHS if m > mo]
        before = reading(mo)
        if not after:
            verdict, words = "pending", "No month after it inside the window yet."
        elif after[-1]["driver_idr"] < 0.5 * before:
            verdict, words = "worked", f"Down from {rp(before)} to {rp(after[-1]['driver_idr'])} a month."
        else:
            verdict, words = "not_yet", f"Still {rp(after[-1]['driver_idr'])} a month against {rp(before)} when drafted."
        obs = r.get("observed_effect") or {}
        rows.append({"decision_id": r["id"], "created_at": r["created_at"], "title": r["title"],
                     "unit_id": uid, "unit": e.get("unit"), "driver": drv,
                     "driver_label": pb.driver(drv)["label"], "month": mo, "before_idr": before,
                     "after": after, "verdict": verdict, "words": words,
                     "closed": bool(obs.get("cause_found")), "cause_found": obs.get("cause_found"),
                     "causes": [{"id": c["id"], "label": c["label"]} for c in pb.driver(drv)["causes"]],
                     "reference": (r.get("artifact") or {}).get("reference")})
    counts = defaultdict(int)
    for x in rows:
        counts[x["verdict"]] += 1
    return {"available": True, "actions": rows, "counts": dict(counts),
            "note": ("Replayed inside the generated window: the planted leaks do not respond to a drafted "
                     "action, so a reading here shows the mechanics, not an effect.")}


def resolve(text: str, function: str | None = None) -> list:
    """Unit ids matching a phrase: a vehicle, driver, crew, material, segment, block or division."""
    if not text:
        return []
    st = _state()
    q = text.strip().lower()
    hits = []
    for uid, m in st["meta"].items():
        if function and m["function"] != function:
            continue
        obj = str(m["object"]).lower()
        lab = m["label"].lower()
        if q in (obj, lab) or (len(q) >= 4 and (q in lab or q == obj.replace("|", "-"))):
            hits.append(uid)
    if not hits and q.startswith("division"):
        d = q.split()[-1]
        hits = [u for u, m in st["meta"].items() if m["division"] == d and (not function or m["function"] == function)]
    return sorted(set(hits), key=lambda u: pb.FUNCTION_ORDER.index(st["meta"][u]["function"]))


# ── for the copilot and the diagnosis ──────────────────────────────────────

def unit_month(uid: str, month: str) -> dict | None:
    st = _state()
    if uid.startswith("group:"):
        g = _group_node(st, uid, _month(month))
        return g if g.get("active") else None
    meta = st["meta"].get(uid)
    r = st["rows"].get((uid, _month(month)))
    if not meta or not r:
        return None
    return {"meta": meta, "row": r, "public": _row(st, uid, _month(month))}


def state() -> dict:
    return _state()


def base() -> dict:
    return _base()
