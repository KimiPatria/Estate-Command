"""Synthetic SAP FI/CO records: what the estate spent, posted bottom-up from the work.

Run once:  python gis/build_finance.py
           (also run at the end of gis/build_synthetic.py)
Writes:    gis/data/synthetic/ec_fi_gl_accounts.csv     SKA1 / cost elements
           gis/data/synthetic/ec_co_cost_centres.csv    CSKS
           gis/data/synthetic/ec_co_orders.csv          AUFK: one order per block, vehicle, road segment
           gis/data/synthetic/ec_co_postings.csv        ACDOCA line items, each pointing at its source
           gis/data/synthetic/ec_co_budget.csv          CO plan version 0, by object and cost-element group
           gis/data/synthetic/ec_hr_wage_types.csv      payroll results per crew and month
           gis/data/synthetic/ec_fuel_log.csv           every fill: store tank or fuel card, per vehicle
           gis/data/synthetic/ec_fi_invoices.csv        supplier invoices against goods receipts (MIRO)
           gis/data/synthetic/ec_fert_application.csv   fertiliser application contract sheets
           gis/data/synthetic/ec_pm_services.csv        workshop service and repair jobs
           gis/data/synthetic/ec_road_orders.csv        road grading and culvert work orders
           gis/data/synthetic/ec_fi_leak_key.csv        ANSWER KEY: planted leaks and decoys
Rewrites:  gis/data/synthetic/ec_block_costs.csv       2025-01..2025-05 rows, from the ledger

What this is for
----------------
The cost-efficiency window (buildplan_finance.md) has to say where money leaks
on the estate, and the old cost feed cannot answer it: ec_block_costs.csv was
allocated top-down (rate x hectares x noise), so ranking blocks on it ranks the
random draw. This ledger is posted from the work instead. Every wage line is a
work order or a payroll day, every material line a goods issue, every diesel
line a fill, every repair a job card, and each carries the id of its source.

Four rules
----------
1. Posted, not allocated. Wages follow attendance (what payroll pays) onto the
   work orders the crew worked; what payroll paid beyond the orders lands on
   the division as unassigned labour. Materials post at standard price to the
   block or vehicle they were issued for; the gap to the supplier's invoice
   lands on the store as price variance. Nothing is spread by hectares except
   overhead, which is what overhead is.

2. Nothing existing moves. Own seed, runs after build_materials, reads every
   feed off disk, and hashes every CSV it does not own before and after.
   Planted leaks live only in the files this generator writes: a fuel card
   fill, a payroll line, a contract sheet, an invoice, a job card, a road
   order. None of them edits a work order, a trip or a material document.

3. Leaks have causes that leave traces. Each planted leak is planted through
   one mechanism, chosen so the candidate causes of its driver look different
   in the data (siphoning on one driver's card is not a leaking tank, which
   would lose fuel on days without trips too). ec_fi_leak_key.csv records the
   true cause; the engine and the model never read it.

4. The budget is set the way estates set it, so budget variance can be judged
   fairly against the leak engine: last year's tonnes at the average rate,
   hectares at the average rate, the fertiliser programme at a budget price,
   diesel by vehicle class with a generous allowance, and a 6% price
   escalation reserve on purchases.

What is planted
---------------
    fuel gap            TR-04, Feb-Apr: driver D-11's fuel card fills on days he drove TR-04
    standing overtime   gang G3-02, Feb-May: overtime on about 60% of its days, output flat
    pay over attendance upkeep crew U4-02, Mar-May: 4 man-days a day paid above the muster
    application labour  Division 5, the 2025-03 fertiliser round: a second contract sheet
                        5-9 days after each issue, 60% on top, on every block alike
    price variance      urea from V101, Feb-Apr: invoiced 5% above the PO price
    breakdowns          DU-01, Feb-May: the injector pump re-repaired every ~3 weeks
    repeat repairs      one culvert: its three repairs in the window, ~40 days apart
    billing over length Division 6 grading, Feb-May: 1.4x the segment length billed
    counterfactuals     TR-06 tank leak (every driver, every day, idle days too); G5-01 overtime
                        on heavy, short-handed days (rounds too big); U6-03 paid absentees on
                        days with four or more absent (muster written up late)
    NOT planted         high-cost terrain, prescriptions on weak blocks, the Lebaran
                        dip, skipped upkeep: decoys, which must not come back as leaks
"""

import csv
import hashlib
import json
import logging
import math
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis.build_synthetic import COST_IDR_HA_YR, FFB_PRICE_IDR_KG, OUT, SEED, _write
from gis.build_operations import WINDOW_END, WINDOW_START, _read

log = logging.getLogger("estate-command.finance")

# ── pricing the generator posts at ─────────────────────────────────────────
# The register's defaults on the day this was written. The engine prices
# losses off the live register; the ledger is what was paid, which a later
# register edit cannot change.
MAN_DAY_IDR = 150_000          # assumptions.man_day_cost_idr
WORK_DAY_H = 7                 # assumptions.work_day_hours
OVERTIME_MULT = 1.5            # assumptions.overtime_multiplier
PREMI_IDR_PER_BUNCH = 250      # assumptions.premi_idr_per_bunch
QUOTA_BUNCHES = 138            # assumptions.harvest_bunches_per_man_day
FERT_KG_PER_MD = 300           # assumptions.fert_kg_per_man_day
CONTRACT_IDR_PER_MD = 165_000  # application contract rate, both contractors
CARD_IDR_PER_L = 13_950        # retail B40 on the fleet card, against 13,500 at standard
GRADING_IDR_PER_KM = 12_000_000   # grading with a gravel top-up, per km per pass
DRIVER_IDR_PER_DAY = MAN_DAY_IDR

ROUND_IN_WINDOW = "2025-03"
MONTHS = ("2025-01", "2025-02", "2025-03", "2025-04", "2025-05")

# ── the planted leaks ──────────────────────────────────────────────────────
FUEL_VEHICLE, FUEL_DRIVER = "TR-04", "D-11"
FUEL_FROM, FUEL_TO = date(2025, 2, 1), date(2025, 4, 30)
FUEL_GAP_SHARE = 0.31
OT_GANG, OT_FROM = "G3-02", date(2025, 2, 1)
OT_DAY_SHARE = 0.6
PAD_CREW, PAD_FROM, PAD_MD = "U4-02", date(2025, 3, 1), 4
FERT_DIVISION, FERT_EXTRA = "5", 0.6
PRICE_MATNR, PRICE_LIFNR, PRICE_UPLIFT = "FE-002", "V101", 0.05
PRICE_FROM, PRICE_TO = date(2025, 2, 1), date(2025, 4, 30)
FLEET_VEHICLE, FLEET_FROM = "DU-01", date(2025, 2, 3)
GRADING_DIVISION, GRADING_FROM, GRADING_UPLIFT = "6", date(2025, 2, 1), 1.4

# Counterfactuals: the same drivers as three leaks above, with the less obvious
# cause. A model that names the cause from the driver alone gets these wrong;
# only the checks can tell them apart (buildplan_finance.md §5).
TANK_VEHICLE, TANK_FROM, TANK_SHARE = "TR-06", date(2025, 2, 1), 0.35
RUSH_GANG, RUSH_FROM = "G5-01", date(2025, 2, 1)
LATE_MUSTER_CREW, LATE_MUSTER_FROM = "U6-03", date(2025, 3, 1)

TANK_L = {"tractor-trailer": 200, "dump-truck": 250, "mini-truck": 80}
CLASS_SERVICE_IDR = {"tractor-trailer": 2_400_000, "dump-truck": 3_300_000, "mini-truck": 1_400_000}
GRADING_INTERVAL = {"good": 150, "fair": 100, "poor": 60}

FUEL_CARD_VENDOR = "V310"
WORKSHOP_VENDOR, LOCAL_WORKSHOP = "V402", "V403"
CONTRACTOR_BY_DIVISION = {"1": "V601", "2": "V601", "3": "V601", "4": "V601",
                          "5": "V602", "6": "V602", "7": "V602"}
GRADER_BY_DIVISION = {"1": "V701", "2": "V701", "3": "V701", "4": "V701",
                      "5": "V702", "6": "V702", "7": "V702"}

# ── chart of accounts ──────────────────────────────────────────────────────
GL = [
    # racct, text, group, kind
    ("4101000", "FFB sales", "REVENUE", "revenue"),
    ("6101000", "Harvest wages", "HARVEST", "wage"),
    ("6101100", "Harvest premium", "HARVEST", "wage"),
    ("6101200", "Overtime", "HARVEST", "wage"),
    ("6102000", "Upkeep wages", "UPKEEP", "wage"),
    ("6103000", "Pest and disease wages", "PEST", "wage"),
    ("6104000", "Fertiliser application, contract", "FERTILISER", "service"),
    ("6105000", "Unassigned labour", "LABOUR", "wage"),
    ("6106000", "Driver wages", "DRIVERS", "wage"),
    ("6201000", "Fertiliser", "FERTILISER", "material"),
    ("6202000", "Herbicide", "UPKEEP", "material"),
    ("6202100", "Pest chemicals", "PEST", "material"),
    ("6203000", "Diesel", "FUEL", "material"),
    ("6204000", "Spare parts", "FLEET", "material"),
    ("6301000", "Vehicle repair and service", "FLEET", "service"),
    ("6302000", "Road works", "ROADS", "service"),
    ("6401000", "Overhead: staff and administration", "OVERHEAD", "overhead"),
    ("6402000", "Overhead: housing and welfare", "OVERHEAD", "overhead"),
    ("6501000", "Purchase price variance", "PRICE", "variance"),
    ("6502000", "Inventory differences", "PRICE", "variance"),
]
GROUP_OF = {a: g for a, _, g, _ in GL}
WAGE_ACCOUNT = {"harvest": "6101000", "prune": "6102000", "weed": "6102000",
                "spray": "6102000", "pest": "6103000"}

OWNED = ("ec_fi_gl_accounts.csv", "ec_co_cost_centres.csv", "ec_co_orders.csv",
         "ec_co_postings.csv", "ec_co_budget.csv", "ec_hr_wage_types.csv", "ec_fuel_log.csv",
         "ec_fi_invoices.csv", "ec_fert_application.csv", "ec_pm_services.csv",
         "ec_road_orders.csv", "ec_fi_leak_key.csv")
REWRITES = ("ec_block_costs.csv",)

POSTING_HEADER = ["belnr", "buzei", "budat", "racct", "rcntr", "aufnr", "division_code",
                  "block_code", "vehicle_id", "driver_id", "segment_id", "crew_code", "matnr",
                  "lifnr", "wsl", "quantity", "unit", "src_type", "src_id"]


# ── helpers ────────────────────────────────────────────────────────────────

def _hashes() -> dict:
    out = {}
    for p in sorted(OUT.glob("*.csv")):
        if p.name in OWNED or p.name in REWRITES:
            continue
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _f(v, d=0.0):
    try:
        return float(v) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _div(v) -> str:
    return str(int(float(v)))


def _blk(v) -> str:
    return str(int(float(v)))


def _bord(div, block) -> str:
    return f"BLK-{_div(div)}-{_blk(block)}"


def _in_window(d) -> bool:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return WINDOW_START <= d <= WINDOW_END


def _days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _month_days(month: str) -> int:
    """Days of the month inside the window: May stops at the export's last day."""
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    last = min(nxt - timedelta(days=1), WINDOW_END)
    return max(0, (last - max(first, WINDOW_START)).days + 1)


def _full_month_days(month: str) -> int:
    y, m = int(month[:4]), int(month[5:7])
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - date(y, m, 1)).days


class Ledger:
    """Accumulates postings with document numbers and sequence."""

    def __init__(self):
        self.rows = []
        self._doc = 100000000

    def post(self, budat, racct, wsl, *, rcntr="", aufnr="", division="", block="",
             vehicle="", driver="", segment="", crew="", matnr="", lifnr="",
             quantity="", unit="", src_type="", src_id=""):
        if not wsl:
            return
        self._doc += 1
        self.rows.append({
            "belnr": f"{self._doc:010d}", "buzei": "001",
            "budat": budat if isinstance(budat, str) else budat.isoformat(),
            "racct": racct, "rcntr": rcntr, "aufnr": aufnr,
            "division_code": _div(division) if division != "" else "",
            "block_code": _blk(block) if block != "" else "",
            "vehicle_id": vehicle, "driver_id": driver, "segment_id": segment,
            "crew_code": crew, "matnr": matnr, "lifnr": lifnr,
            "wsl": round(wsl), "quantity": round(quantity, 3) if quantity != "" else "",
            "unit": unit, "src_type": src_type, "src_id": src_id,
        })


# ── inputs ─────────────────────────────────────────────────────────────────

def _inputs() -> dict:
    from gis import environment
    rain = environment.rainfall_by_day("EC") or {}
    crews = {r["crew_code"]: r for r in _read("ec_crews.csv")}
    vehicles = {r["vehicle_id"]: r for r in _read("ec_vehicles.csv")}
    materials = {r["matnr"]: r for r in _read("ec_mm_materials.csv")}
    pos = {(r["ebeln"], r["ebelp"]): r for r in _read("ec_mm_purchase_orders.csv")}
    abw = {(_div(r["division_code"]), _blk(r["block_code"])): _f(r["abw_kg"])
           for r in _read("ec_abw.csv")}
    orders = []
    for fn in ("ec_harvest_orders.csv", "ec_upkeep_orders.csv", "ec_pest_orders.csv"):
        orders += _read(fn)
    trips = [t for t in _read("ec_weighbridge.csv") if _in_window(t["date"])]
    roads = _read("ec_roads.csv")
    history = _read("ec_harvest_history.csv")
    ha = {}
    for r in history:
        if r["month"] == "2025-01":
            ha[(_div(r["division_code"]), _blk(r["block_code"]))] = _f(r["planted_ha"])
    return {"rain": rain, "crews": crews, "vehicles": vehicles, "materials": materials,
            "pos": pos, "abw": abw, "orders": orders, "trips": trips, "roads": roads,
            "history": history, "ha": ha,
            "attendance": _read("ec_attendance.csv"),
            "harvester_day": _read("ec_harvester_day.csv"),
            "movements": _read("ec_mm_movements.csv"),
            "pm_orders": _read("ec_pm_orders.csv"),
            "fertiliser": _read("ec_fertiliser.csv"),
            "block_costs": _read("ec_block_costs.csv")}


# ── masters ────────────────────────────────────────────────────────────────

def masters(inp) -> tuple[list, list, list]:
    gl = [{"racct": a, "txt": t, "group": g, "kind": k} for a, t, g, k in GL]
    divisions = sorted({d for (d, _) in inp["ha"]}, key=int)
    cc = [{"kostl": f"EC-D{d}", "name": f"Division {d}", "kind": "division", "division_code": d}
          for d in divisions]
    cc += [
        {"kostl": "EC-TP", "name": "Transport pool", "kind": "service", "division_code": ""},
        {"kostl": "EC-WS", "name": "Workshop", "kind": "service", "division_code": ""},
        {"kostl": "EC-STORE", "name": "Central store", "kind": "service", "division_code": ""},
        {"kostl": "EC-GEN", "name": "Generators", "kind": "service", "division_code": ""},
        {"kostl": "EC-OFF", "name": "Estate office", "kind": "overhead", "division_code": ""},
    ]
    orders = [{"aufnr": _bord(d, b), "kind": "block", "object": f"{d}|{b}", "kostl": f"EC-D{d}",
               "text": f"Block {d}-{b}"} for (d, b) in sorted(inp["ha"], key=lambda k: (int(k[0]), int(k[1])))]
    orders += [{"aufnr": f"VEH-{v}", "kind": "vehicle", "object": v, "kostl": "EC-TP",
                "text": f"{r['vehicle_class']} {v}"} for v, r in sorted(inp["vehicles"].items())]
    orders += [{"aufnr": f"RD-{r['segment_id']}", "kind": "road", "object": r["segment_id"],
                "kostl": f"EC-D{_div(r['division_code'])}",
                "text": f"Road segment {r['segment_id']}"} for r in inp["roads"]]
    return gl, cc, orders


# ── labour ─────────────────────────────────────────────────────────────────

def post_labour(inp, L: Ledger, rng) -> tuple[list, dict]:
    """Payroll per crew-day onto the crew's work orders; the rest to the division."""
    by_crew_day = defaultdict(list)
    for o in inp["orders"]:
        if _in_window(o["date"]) and _f(o["man_days_actual"]) > 0:
            by_crew_day[(o["crew_code"], o["date"])].append(o)

    # Harvest output per cutter man-day, for the overtime rule.
    out_per_md = {}
    for (crew, d), os_ in by_crew_day.items():
        if os_[0]["operation"] != "harvest":
            continue
        md = sum(_f(o["man_days_actual"]) for o in os_)
        out_per_md[(crew, d)] = sum(_f(o["actual_qty"]) for o in os_) / md if md else 0.0
    p80, p50 = {}, {}
    by_crew = defaultdict(list)
    for (crew, d), v in out_per_md.items():
        by_crew[crew].append(v)
    for crew, vs in by_crew.items():
        vs = sorted(vs)
        p80[crew] = vs[int(0.8 * (len(vs) - 1))] if vs else 0.0
        p50[crew] = vs[int(0.5 * (len(vs) - 1))] if vs else 0.0

    # Premium: bunches above the quota per harvester-day, onto the block-day's order.
    premium = defaultdict(float)
    premium_bunches = defaultdict(float)
    for r in inp["harvester_day"]:
        if not _in_window(r["date"]):
            continue
        md = _f(r["man_days"], 1.0)
        over = max(0.0, _f(r["bunches_cut"]) - QUOTA_BUNCHES * md)
        k = (r["date"], _div(r["division_code"]), _blk(r["block_code"]))
        premium[k] += over * PREMI_IDR_PER_BUNCH
        premium_bunches[k] += over

    wage_types = defaultdict(lambda: {"present_md": 0, "paid_md": 0.0, "order_md": 0.0,
                                      "basic_idr": 0.0, "premium_idr": 0.0,
                                      "overtime_h": 0.0, "overtime_idr": 0.0})
    ot_rate = MAN_DAY_IDR / WORK_DAY_H * OVERTIME_MULT
    planted = {"overtime_idr": 0.0, "padding_idr": 0.0, "ot_days": 0, "pad_days": 0}
    premium_posted = set()

    for a in inp["attendance"]:
        d = a["date"]
        if not _in_window(d):
            continue
        crew = a["crew_code"]
        div = _div(a["division_code"])
        present = int(a["present"])
        pad = PAD_MD if (crew == PAD_CREW and date.fromisoformat(d) >= PAD_FROM) else 0
        absent = int(a["absent"])
        if crew == LATE_MUSTER_CREW and date.fromisoformat(d) >= LATE_MUSTER_FROM and absent >= 4:
            pad = absent // 2                  # half the absentees paid as present when the muster is written late
            planted["late_muster_idr"] = planted.get("late_muster_idr", 0.0) + pad * MAN_DAY_IDR
        paid = present + pad
        os_ = by_crew_day.get((crew, d), [])
        order_md = sum(_f(o["man_days_actual"]) for o in os_)
        alloc = min(paid, order_md)
        scale = alloc / order_md if order_md else 0.0
        mo = d[:7]
        w = wage_types[(crew, mo)]
        w["present_md"] += present
        w["paid_md"] += paid
        w["order_md"] += alloc
        w["basic_idr"] += paid * MAN_DAY_IDR
        for o in os_:
            md = _f(o["man_days_actual"]) * scale
            if md <= 0:
                continue
            L.post(d, WAGE_ACCOUNT[o["operation"]], md * MAN_DAY_IDR, rcntr=f"EC-D{div}",
                   aufnr=_bord(o["division_code"], o["block_code"]), division=o["division_code"],
                   block=o["block_code"], crew=crew, quantity=md, unit="MD",
                   src_type="work_order", src_id=o["order_id"])
            if o["operation"] == "harvest":
                pk = (d, _div(o["division_code"]), _blk(o["block_code"]))
                if pk in premium and pk not in premium_posted:
                    premium_posted.add(pk)
                    L.post(d, "6101100", premium[pk], rcntr=f"EC-D{div}",
                           aufnr=_bord(o["division_code"], o["block_code"]),
                           division=o["division_code"], block=o["block_code"], crew=crew,
                           quantity=premium_bunches[pk], unit="BUNCH",
                           src_type="work_order", src_id=o["order_id"])
                    w["premium_idr"] += premium[pk]
        rest = paid - alloc
        if rest > 0:
            L.post(d, "6105000", rest * MAN_DAY_IDR, rcntr=f"EC-D{div}", division=div, crew=crew,
                   quantity=rest, unit="MD", src_type="payroll", src_id=f"{crew}:{d}")
        if pad and crew == PAD_CREW:
            planted["padding_idr"] += pad * MAN_DAY_IDR
            planted["pad_days"] += 1

        # Overtime, harvest gangs only.
        if os_ and os_[0]["operation"] == "harvest":
            heavy = out_per_md.get((crew, d), 0.0) > p80.get(crew, 1e9)
            hours = 0.0
            if crew == OT_GANG and date.fromisoformat(d) >= OT_FROM:
                # Standing overtime: the same dozen hands, whatever the day's crop or headcount.
                if rng.random() < OT_DAY_SHARE:
                    hours = min(alloc, 12) * 2.5
                    planted["ot_days"] += 1
            elif crew == RUSH_GANG and date.fromisoformat(d) >= RUSH_FROM:
                # Rounds too big: overtime on every heavy-crop day the gang is short-handed.
                short = present < 0.95 * float(inp["crews"][crew]["establishment"] or 1)
                if out_per_md.get((crew, d), 0.0) > p50.get(crew, 1e9) and short:
                    hours = round(0.9 * alloc) * 3.0
                    planted["rounds_ot_idr"] = planted.get("rounds_ot_idr", 0.0) + hours * ot_rate
            elif heavy:
                hours = round(0.3 * alloc) * 1.5
            if hours > 0:
                idr = hours * ot_rate
                if crew == OT_GANG and date.fromisoformat(d) >= OT_FROM:
                    planted["overtime_idr"] += idr
                tot_md = sum(_f(o["man_days_actual"]) for o in os_)
                for o in os_:
                    share = _f(o["man_days_actual"]) / tot_md
                    L.post(d, "6101200", idr * share, rcntr=f"EC-D{div}",
                           aufnr=_bord(o["division_code"], o["block_code"]),
                           division=o["division_code"], block=o["block_code"], crew=crew,
                           quantity=hours * share, unit="H", src_type="payroll",
                           src_id=f"{crew}:{d}")
                w["overtime_h"] += hours
                w["overtime_idr"] += idr

    # Drivers: one man-day per driver per day with a trip.
    driver_days = defaultdict(int)
    for t in inp["trips"]:
        driver_days[(t["driver_id"], t["date"])] += 1
    for (drv, d), n in sorted(driver_days.items()):
        L.post(d, "6106000", DRIVER_IDR_PER_DAY, rcntr="EC-TP", driver=drv, quantity=1,
               unit="MD", src_type="trip_day", src_id=f"{drv}:{d}")

    rows = []
    for (crew, mo), w in sorted(wage_types.items()):
        rows.append({"crew_code": crew, "crew_type": inp["crews"][crew]["crew_type"],
                     "division_code": _div(inp["crews"][crew]["division_code"]), "month": mo,
                     "present_md": w["present_md"], "paid_md": round(w["paid_md"], 2),
                     "order_md": round(w["order_md"], 2), "basic_idr": round(w["basic_idr"]),
                     "premium_idr": round(w["premium_idr"]), "overtime_h": round(w["overtime_h"], 1),
                     "overtime_idr": round(w["overtime_idr"])})
    return rows, planted


# ── materials ──────────────────────────────────────────────────────────────

def post_materials(inp, L: Ledger, rng) -> tuple[list, list, dict]:
    """Goods issues at standard; fuel to vehicles through the fill log; invoices and variances."""
    mats = inp["materials"]
    verpr = {m: _f(r["verpr"]) for m, r in mats.items()}
    planted = {"fuel_l": 0.0, "fuel_idr": 0.0, "price_idr": 0.0, "price_receipts": 0}

    # Trip diesel per (date, division, vehicle), the burn a fill log divides by.
    burn = defaultdict(lambda: defaultdict(float))
    first_driver = {}
    drove = defaultdict(set)          # (vehicle, date) -> drivers
    for t in sorted(inp["trips"], key=lambda t: (t["date"], t["depart_time"])):
        k = (t["date"], _div(t["division_code"]))
        burn[k][t["vehicle_id"]] += _f(t["diesel_l"])
        first_driver.setdefault((t["date"], _div(t["division_code"]), t["vehicle_id"]), t["driver_id"])
        drove[(t["vehicle_id"], t["date"])].add(t["driver_id"])

    fuel_log = []
    fill_seq = 0

    def fill(d, vehicle, driver, litres, source, kostl, price, src_id):
        nonlocal fill_seq
        fill_seq += 1
        fid = f"FL-{fill_seq:06d}"
        fuel_log.append({"fill_id": fid, "date": d, "vehicle_id": vehicle, "driver_id": driver,
                         "source": source, "litres": round(litres, 1), "kostl": kostl,
                         "price_idr_l": price, "src_id": src_id})
        return fid

    for mv in inp["movements"]:
        if not _in_window(mv["budat"]):
            continue
        bwart, matnr = mv["bwart"], mv["matnr"]
        qty = _f(mv["menge"])
        price = verpr.get(matnr, 0.0)
        kostl = mv["kostl"]
        div = kostl[4:] if kostl.startswith("EC-D") else ""
        d = mv["budat"]
        if bwart == "201":
            group = mats[matnr]["matkl"]
            if group == "FERT":
                L.post(d, "6201000", qty * price, rcntr=kostl, aufnr=_bord(div, mv["block_code"]),
                       division=div, block=mv["block_code"], matnr=matnr, quantity=qty,
                       unit=mv["meins"], src_type="matdoc", src_id=mv["mblnr"])
            elif group == "AGCH":
                acct = "6202000" if matnr in ("AC-001", "AC-002") else "6202100"
                if mv["block_code"]:
                    L.post(d, acct, qty * price, rcntr=kostl, aufnr=_bord(div, mv["block_code"]),
                           division=div, block=mv["block_code"], matnr=matnr, quantity=qty,
                           unit=mv["meins"], src_type="matdoc", src_id=mv["mblnr"])
                else:
                    L.post(d, acct, qty * price, rcntr=kostl, division=div, matnr=matnr,
                           quantity=qty, unit=mv["meins"], src_type="matdoc", src_id=mv["mblnr"])
            elif group == "FUEL":
                if kostl == "EC-GEN":
                    fid = fill(d, "", "", qty, "store", kostl, price, mv["mblnr"])
                    L.post(d, "6203000", qty * price, rcntr="EC-GEN", matnr=matnr, quantity=qty,
                           unit="L", src_type="fuel_fill", src_id=fid)
                    continue
                # Split the division's daily issue across the vehicles that burned it,
                # as the pump log at the division tank records it.
                shares = burn.get((d, div)) or {}
                tot = sum(shares.values())
                if not tot:
                    L.post(d, "6203000", qty * price, rcntr=kostl, division=div, matnr=matnr,
                           quantity=qty, unit="L", src_type="matdoc", src_id=mv["mblnr"])
                    continue
                vs = sorted(shares)
                w = [shares[v] * math.exp(rng.gauss(0, 0.03)) for v in vs]
                sw = sum(w)
                litres = [round(qty * x / sw, 1) for x in w]
                litres[-1] = round(qty - sum(litres[:-1]), 1)
                for v, l in zip(vs, litres):
                    if l <= 0:
                        continue
                    fid = fill(d, v, first_driver.get((d, div, v), ""), l, "store", kostl, price, mv["mblnr"])
                    L.post(d, "6203000", l * price, rcntr="EC-TP", aufnr=f"VEH-{v}", division=div,
                           vehicle=v, driver=first_driver.get((d, div, v), ""), matnr=matnr,
                           quantity=l, unit="L", src_type="fuel_fill", src_id=fid)
            elif group == "SPARE":
                L.post(d, "6204000", qty * price, rcntr="EC-WS", matnr=matnr, quantity=qty,
                       unit=mv["meins"], src_type="matdoc", src_id=mv["mblnr"])
        elif bwart == "261":
            pm = next((p for p in inp["pm_orders"] if p["order_id"] == mv["aufnr"]), None)
            veh = pm["vehicle_id"] if pm else ""
            L.post(d, "6204000", qty * price, rcntr="EC-WS", aufnr=f"VEH-{veh}" if veh else "",
                   vehicle=veh, matnr=matnr, quantity=qty, unit=mv["meins"],
                   src_type="matdoc", src_id=mv["mblnr"])
        elif bwart in ("551", "701", "702"):
            sign = 1.0 if mv["shkzg"] == "H" else -1.0
            L.post(d, "6502000", sign * qty * price, rcntr="EC-STORE", matnr=matnr,
                   quantity=sign * qty, unit=mv["meins"], src_type="matdoc", src_id=mv["mblnr"])

    # Fuel cards: drivers fill outside the estate for runs to town, now and then.
    month_burn = defaultdict(float)
    for (d, div), vs in burn.items():
        for v, l in vs.items():
            month_burn[(v, d[:7])] += l
    vdays = defaultdict(list)
    for (v, d) in drove:
        vdays[(v, d[:7])].append(d)
    for (v, mo), ds in sorted(vdays.items()):
        cls = inp["vehicles"][v]["vehicle_class"]
        ds = sorted(ds)
        n = 0
        lam = 1.1 * _month_days(mo) / _full_month_days(mo)
        # Poisson by inversion
        p, c, u = math.exp(-lam), math.exp(-lam), rng.random()
        while u > c:
            n += 1
            p *= lam / n
            c += p
        for _ in range(n):
            d = rng.choice(ds)
            drv = rng.choice(sorted(drove[(v, d)]))
            litres = rng.uniform(0.15, 0.3) * TANK_L[cls]
            fid = fill(d, v, drv, litres, "card", "EC-TP", CARD_IDR_PER_L, f"CARD-{v}-{d}")
            L.post(d, "6203000", litres * CARD_IDR_PER_L, rcntr="EC-TP", aufnr=f"VEH-{v}",
                   vehicle=v, driver=drv, matnr="FU-001", lifnr=FUEL_CARD_VENDOR, quantity=litres,
                   unit="L", src_type="fuel_fill", src_id=fid)

    # Planted: D-11's card on the days he drove TR-04, sized to the month's gap.
    cap = TANK_L[inp["vehicles"][FUEL_VEHICLE]["vehicle_class"]]
    for mo in MONTHS:
        y, m = int(mo[:4]), int(mo[5:7])
        if not (FUEL_FROM <= date(y, m, 1) <= FUEL_TO):
            continue
        days = sorted(d for d in vdays.get((FUEL_VEHICLE, mo), [])
                      if FUEL_DRIVER in drove[(FUEL_VEHICLE, d)])
        if not days:
            continue
        # A few near-full fills rather than many small ones: siphoning fills the tank on a
        # day the truck burned little, which is the trace that separates it from a leak.
        target = FUEL_GAP_SHARE * month_burn[(FUEL_VEHICLE, mo)]
        n = max(1, min(len(days), round(target / (0.9 * cap))))
        chosen = sorted(rng.sample(days, n))
        per = min(cap * 0.97, target / n)
        for d in chosen:
            litres = per * rng.uniform(0.94, 1.0)
            fid = fill(d, FUEL_VEHICLE, FUEL_DRIVER, litres, "card", "EC-TP", CARD_IDR_PER_L,
                       f"CARD-{FUEL_VEHICLE}-{d}")
            L.post(d, "6203000", litres * CARD_IDR_PER_L, rcntr="EC-TP",
                   aufnr=f"VEH-{FUEL_VEHICLE}", vehicle=FUEL_VEHICLE, driver=FUEL_DRIVER,
                   matnr="FU-001", lifnr=FUEL_CARD_VENDOR, quantity=litres, unit="L",
                   src_type="fuel_fill", src_id=fid)
            planted["fuel_l"] += litres
            planted["fuel_idr"] += litres * CARD_IDR_PER_L

    # Counterfactual: TR-06's tank leaks. Whoever drives it tops it up at an outside
    # station every working day, a bit over a third of what the trips burn, and the yard
    # tops it up on some idle days too: small fills, every driver, every day.
    tank_days = defaultdict(float)
    for (d, div), vs in burn.items():
        if TANK_VEHICLE in vs:
            tank_days[d] += vs[TANK_VEHICLE]
    for d in sorted(tank_days):
        if date.fromisoformat(d) < TANK_FROM:
            continue
        drv = rng.choice(sorted(drove[(TANK_VEHICLE, d)]))
        litres = tank_days[d] * TANK_SHARE * rng.uniform(0.85, 1.15)
        fid = fill(d, TANK_VEHICLE, drv, litres, "card", "EC-TP", CARD_IDR_PER_L, f"CARD-{TANK_VEHICLE}-{d}")
        L.post(d, "6203000", litres * CARD_IDR_PER_L, rcntr="EC-TP", aufnr=f"VEH-{TANK_VEHICLE}",
               vehicle=TANK_VEHICLE, driver=drv, matnr="FU-001", lifnr=FUEL_CARD_VENDOR, quantity=litres,
               unit="L", src_type="fuel_fill", src_id=fid)
        planted["tank_idr"] = planted.get("tank_idr", 0.0) + litres * CARD_IDR_PER_L
    for d in _days(TANK_FROM, WINDOW_END):
        ds = d.isoformat()
        if ds in tank_days or d.weekday() == 6 or rng.random() > 0.35:
            continue
        litres = rng.uniform(8, 18)
        fid = fill(ds, TANK_VEHICLE, "", litres, "card", "EC-TP", CARD_IDR_PER_L, f"YARD-{TANK_VEHICLE}-{ds}")
        L.post(ds, "6203000", litres * CARD_IDR_PER_L, rcntr="EC-TP", aufnr=f"VEH-{TANK_VEHICLE}",
               vehicle=TANK_VEHICLE, matnr="FU-001", lifnr=FUEL_CARD_VENDOR, quantity=litres, unit="L",
               src_type="fuel_fill", src_id=fid)
        planted["tank_idr"] = planted.get("tank_idr", 0.0) + litres * CARD_IDR_PER_L

    # Invoices against goods receipts; the gap to standard is price variance.
    invoices = []
    inv_seq = 5100000000
    for mv in inp["movements"]:
        if mv["bwart"] != "101" or not _in_window(mv["budat"]):
            continue
        po = inp["pos"].get((mv["ebeln"], mv["ebelp"]))
        if not po:
            continue
        qty = _f(mv["menge"])
        netpr = _f(po["netpr"])
        price = netpr
        d = date.fromisoformat(mv["budat"])
        uplift = (mv["matnr"] == PRICE_MATNR and po["lifnr"] == PRICE_LIFNR
                  and PRICE_FROM <= d <= PRICE_TO)
        if uplift:
            price = round(netpr * (1 + PRICE_UPLIFT), 2)
        inv_seq += 1
        amount = qty * price
        invoices.append({"belnr": str(inv_seq), "budat": mv["budat"], "lifnr": po["lifnr"],
                         "ebeln": mv["ebeln"], "ebelp": mv["ebelp"], "bsart": po["bsart"],
                         "matnr": mv["matnr"], "menge": qty, "meins": mv["meins"],
                         "po_price": netpr, "invoice_price": price, "amount_idr": round(amount),
                         "mblnr": mv["mblnr"]})
        ppv = qty * (price - verpr[mv["matnr"]])
        L.post(mv["budat"], "6501000", ppv, rcntr="EC-STORE", matnr=mv["matnr"],
               lifnr=po["lifnr"], quantity=qty, unit=mv["meins"], src_type="invoice",
               src_id=str(inv_seq))
        if uplift:
            planted["price_idr"] += qty * (price - netpr)
            planted["price_receipts"] += 1
    return fuel_log, invoices, planted


# ── services: fertiliser contracts, workshop jobs, roads ───────────────────

def post_fert_application(inp, L: Ledger, rng) -> tuple[list, dict]:
    sheets = []
    seq = 0
    planted = {"idr": 0.0, "blocks": 0}
    for r in inp["fertiliser"]:
        if r["window"] != ROUND_IN_WINDOW or not r["issue_date"]:
            continue
        d = date.fromisoformat(r["issue_date"])
        kg = _f(r["issued_kg"])
        if kg <= 0 or not _in_window(d):
            continue
        div = _div(r["division_code"])
        std = kg / FERT_KG_PER_MD
        md = std * math.exp(rng.gauss(0, 0.10))
        vendor = CONTRACTOR_BY_DIVISION[div]
        when = d + timedelta(days=rng.choice((0, 0, 1)))
        parts = [(when, md)]
        if div == FERT_DIVISION:
            extra = std * FERT_EXTRA
            late = d + timedelta(days=rng.randint(5, 9))
            if late <= WINDOW_END:
                parts.append((late, extra))
                planted["idr"] += extra * CONTRACT_IDR_PER_MD
                planted["blocks"] += 1
        for when_, md_ in parts:
            if when_ > WINDOW_END:
                continue
            seq += 1
            sid = f"FA-{seq:05d}"
            sheets.append({"sheet_id": sid, "date": when_.isoformat(), "division_code": div,
                           "block_code": _blk(r["block_code"]), "round": r["window"],
                           "material": r["material"], "issue_date": r["issue_date"],
                           "kg_applied": round(kg, 1), "man_days": round(md_, 2), "lifnr": vendor,
                           "rate_idr_per_md": CONTRACT_IDR_PER_MD,
                           "amount_idr": round(md_ * CONTRACT_IDR_PER_MD)})
            L.post(when_, "6104000", md_ * CONTRACT_IDR_PER_MD, rcntr=f"EC-D{div}",
                   aufnr=_bord(div, r["block_code"]), division=div, block=r["block_code"],
                   lifnr=vendor, quantity=md_, unit="MD", src_type="contract_sheet", src_id=sid)
    return sheets, planted


def post_fleet(inp, L: Ledger, rng) -> tuple[list, dict]:
    jobs = []
    planted = {"idr": 0.0, "jobs": 0}
    for p in inp["pm_orders"]:
        if not _in_window(p["raised_date"]):
            continue
        L.post(p["raised_date"], "6301000", _f(p["cost_idr"]), rcntr="EC-WS",
               aufnr=f"VEH-{p['vehicle_id']}", vehicle=p["vehicle_id"], lifnr=WORKSHOP_VENDOR,
               quantity=_f(p["downtime_h"]), unit="H", src_type="pm_order", src_id=p["order_id"])
    seq = 0
    for v, r in sorted(inp["vehicles"].items()):
        cls = r["vehicle_class"]
        d = WINDOW_START + timedelta(days=rng.randint(0, 30))
        while d <= WINDOW_END:
            seq += 1
            cost = CLASS_SERVICE_IDR[cls] * math.exp(rng.gauss(0, 0.12))
            jid = f"SV-{seq:05d}"
            jobs.append({"job_id": jid, "date": d.isoformat(), "vehicle_id": v, "vehicle_class": cls,
                         "kind": "service", "fault": "", "lifnr": WORKSHOP_VENDOR,
                         "cost_idr": round(cost), "downtime_h": round(rng.uniform(3, 6), 1)})
            d += timedelta(days=rng.randint(30, 45))
    # Planted: DU-01's injector pump, patched by a local workshop and failing again.
    d = FLEET_FROM
    while d <= WINDOW_END:
        seq += 1
        cost = rng.uniform(8_000_000, 11_000_000)
        jid = f"SV-{seq:05d}"
        jobs.append({"job_id": jid, "date": d.isoformat(), "vehicle_id": FLEET_VEHICLE,
                     "vehicle_class": inp["vehicles"][FLEET_VEHICLE]["vehicle_class"],
                     "kind": "repair", "fault": "injector pump", "lifnr": LOCAL_WORKSHOP,
                     "cost_idr": round(cost), "downtime_h": round(rng.uniform(12, 18), 1)})
        planted["idr"] += cost
        planted["jobs"] += 1
        d += timedelta(days=rng.randint(18, 24))
    jobs.sort(key=lambda j: (j["date"], j["vehicle_id"]))
    for j in jobs:
        L.post(j["date"], "6301000", j["cost_idr"], rcntr="EC-WS", aufnr=f"VEH-{j['vehicle_id']}",
               vehicle=j["vehicle_id"], lifnr=j["lifnr"], quantity=j["downtime_h"], unit="H",
               src_type="service_job", src_id=j["job_id"])
    return jobs, planted


def post_roads(inp, L: Ledger, rng) -> tuple[list, dict]:
    rows = []
    seq = 0
    planted = {"grading_idr": 0.0, "grading_orders": 0, "culvert_segment": None, "culvert_idr": 0.0}
    year_ago = WINDOW_END - timedelta(days=364)
    # The repeat culvert: the poor segment with three repairs and the longest run.
    poor3 = sorted((r for r in inp["roads"] if r["condition"] == "poor"
                    and int(r["culvert_repairs_12m"]) == 3), key=lambda r: -_f(r["length_km"]))
    culvert_seg = poor3[0]["segment_id"] if poor3 else None
    planted["culvert_segment"] = culvert_seg
    for r in inp["roads"]:
        seg, div = r["segment_id"], _div(r["division_code"])
        length = _f(r["length_km"])
        interval = GRADING_INTERVAL.get(r["condition"], 100)
        d = year_ago + timedelta(days=rng.randint(0, interval - 1))
        while d <= WINDOW_END:
            km = length * rng.uniform(0.98, 1.03)
            vendor = GRADER_BY_DIVISION[div]
            over = div == GRADING_DIVISION and d >= GRADING_FROM
            if over:
                km = length * GRADING_UPLIFT
            seq += 1
            oid = f"RO-{seq:05d}"
            cost = km * GRADING_IDR_PER_KM
            rows.append({"order_id": oid, "date": d.isoformat(), "segment_id": seg,
                         "division_code": div, "block_code": _blk(r["block_code"]),
                         "activity": "grading", "lifnr": vendor, "length_km": length,
                         "km_billed": round(km, 3), "cost_idr": round(cost),
                         "condition": r["condition"]})
            if over:
                planted["grading_idr"] += (km - length) * GRADING_IDR_PER_KM
                planted["grading_orders"] += 1
            d += timedelta(days=interval + rng.randint(-10, 10))
        n = int(r["culvert_repairs_12m"])
        if seg == culvert_seg:
            dates = [date(2025, 1, 21), date(2025, 3, 3), date(2025, 4, 14)]
            costs = [rng.uniform(18e6, 24e6) for _ in dates]
        else:
            dates = sorted(year_ago + timedelta(days=rng.randint(0, 363)) for _ in range(n))
            costs = [rng.uniform(8e6, 15e6) for _ in dates]
        for d, c in zip(dates, costs):
            seq += 1
            rows.append({"order_id": f"RO-{seq:05d}", "date": d.isoformat(), "segment_id": seg,
                         "division_code": div, "block_code": _blk(r["block_code"]),
                         "activity": "culvert", "lifnr": GRADER_BY_DIVISION[div],
                         "length_km": length, "km_billed": "", "cost_idr": round(c),
                         "condition": r["condition"]})
            if seg == culvert_seg:
                planted["culvert_idr"] += c
    rows.sort(key=lambda x: (x["date"], x["segment_id"]))
    for o in rows:
        if not _in_window(o["date"]):
            continue
        L.post(o["date"], "6302000", o["cost_idr"], rcntr=f"EC-D{o['division_code']}",
               aufnr=f"RD-{o['segment_id']}", division=o["division_code"], block=o["block_code"],
               segment=o["segment_id"], lifnr=o["lifnr"],
               quantity=_f(o["km_billed"]) if o["km_billed"] != "" else 1,
               unit="KM" if o["activity"] == "grading" else "EA", src_type="road_order",
               src_id=o["order_id"])
    return rows, planted


def post_overhead_and_revenue(inp, L: Ledger) -> None:
    div_ha = defaultdict(float)
    for (d, _), h in inp["ha"].items():
        div_ha[d] += h
    for mo in MONTHS:
        share = _month_days(mo) / _full_month_days(mo)
        day = f"{mo}-01"
        for d, h in sorted(div_ha.items(), key=lambda x: int(x[0])):
            base = h * COST_IDR_HA_YR["overhead"] / 12 * share
            L.post(day, "6401000", 0.6 * base, rcntr=f"EC-D{d}", division=d, src_type="accrual",
                   src_id=f"OH-{mo}-{d}")
            L.post(day, "6402000", 0.4 * base, rcntr=f"EC-D{d}", division=d, src_type="accrual",
                   src_id=f"OH-{mo}-{d}")
    # Revenue per block-month, from what the mill weighed.
    kg = defaultdict(float)
    for t in inp["trips"]:
        kg[(t["month"], _div(t["division_code"]), _blk(t["block_code"]))] += _f(t["net_kg"])
    for (mo, d, b), v in sorted(kg.items()):
        L.post(f"{mo}-{min(28, _month_days(mo)):02d}", "4101000", -v * FFB_PRICE_IDR_KG,
               rcntr=f"EC-D{d}", aufnr=_bord(d, b), division=d, block=b, quantity=v / 1000,
               unit="T", src_type="weighbridge", src_id=f"{mo}:{d}-{b}")


# ── the budget ─────────────────────────────────────────────────────────────

def budget(inp, postings) -> list:
    """CO plan version 0, by object, month and cost-element group, set as estates set it."""
    def obj(p):
        return p["aufnr"] or p["rcntr"]
    actual = defaultdict(float)
    for p in postings:
        g = GROUP_OF[p["racct"]]
        if g == "REVENUE":
            continue
        actual[(p["budat"][:7], obj(p), g)] += p["wsl"]
    rows = []
    vehicles = inp["vehicles"]
    ha = inp["ha"]
    abw = inp["abw"]

    # Harvest: last year's tonnes at the window's estate rate per tonne, plus 5%.
    tonnes = defaultdict(float)
    for t in inp["trips"]:
        tonnes[(t["month"], _div(t["division_code"]), _blk(t["block_code"]))] += _f(t["net_kg"]) / 1000
    harvest_idr = sum(v for (mo, o, g), v in actual.items() if g == "HARVEST")
    rate_t = harvest_idr / max(sum(tonnes.values()), 1)
    last_year = defaultdict(float)
    for r in inp["history"]:
        mo = r["month"]
        if mo.startswith("2024-"):
            k = (_div(r["division_code"]), _blk(r["block_code"]))
            last_year[("2025" + mo[4:], k)] += int(r["bunches"]) * abw.get(k, 8.0) / 1000
    for mo in MONTHS:
        share = _month_days(mo) / _full_month_days(mo)
        for (d, b), h in ha.items():
            rows.append({"month": mo, "rcntr": f"EC-D{d}", "aufnr": _bord(d, b), "group": "HARVEST",
                         "budget_idr": round(last_year[(mo, (d, b))] * rate_t * 1.05 * share)})

    # Hectare budgets: upkeep and pest at the estate's rate per hectare.
    tot_ha = sum(ha.values())
    for g, uplift in (("UPKEEP", 1.08), ("PEST", 1.10)):
        per_ha_month = sum(v for (mo, o, gg), v in actual.items() if gg == g) / tot_ha / (
            sum(_month_days(m) for m in MONTHS) / 30.4)
        for mo in MONTHS:
            share = _month_days(mo) / 30.4
            for (d, b), h in ha.items():
                rows.append({"month": mo, "rcntr": f"EC-D{d}", "aufnr": _bord(d, b), "group": g,
                             "budget_idr": round(h * per_ha_month * share * uplift)})

    # Fertiliser: the programme at a budget price 20% over standard, spread by the usual lag.
    verpr = {m: _f(r["verpr"]) for m, r in inp["materials"].items()}
    fmat = {"NPK 12-12-17-2": "FE-001", "Urea": "FE-002", "Kieserite": "FE-003", "Borate": "FE-004"}
    spread = {"2025-03": 0.5, "2025-04": 0.35, "2025-05": 0.15}
    prog = defaultdict(float)
    for r in inp["fertiliser"]:
        if r["window"] != ROUND_IN_WINDOW:
            continue
        kg = _f(r["target_kg"])
        prog[(_div(r["division_code"]), _blk(r["block_code"]))] += (
            kg * verpr[fmat[r["material"]]] * 1.20 + kg / FERT_KG_PER_MD * CONTRACT_IDR_PER_MD * 1.05)
    for mo in MONTHS:
        for (d, b) in ha:
            rows.append({"month": mo, "rcntr": f"EC-D{d}", "aufnr": _bord(d, b), "group": "FERTILISER",
                         "budget_idr": round(prog[(d, b)] * spread.get(mo, 0.0))})

    # Unassigned labour per division and drivers: the window's daily rate plus 5%.
    # Overhead is fixed and budgeted at what it is.
    window_days = sum(_month_days(m) for m in MONTHS)
    for g in ("LABOUR", "DRIVERS", "OVERHEAD"):
        for o in sorted({o for (mo, o, gg) in actual if gg == g}):
            per_day = sum(actual.get((mo, o, g), 0.0) for mo in MONTHS) / window_days
            for mo in MONTHS:
                b = (actual.get((mo, o, g), 0.0) if g == "OVERHEAD"
                     else per_day * _month_days(mo) * 1.05)
                rows.append({"month": mo, "rcntr": o, "aufnr": "", "group": g,
                             "budget_idr": round(b)})

    # Diesel and repairs per vehicle, by class: diesel generous at 35% over the class average.
    for g, uplift in (("FUEL", 1.35), ("FLEET", 1.10)):
        cls_tot = defaultdict(float)
        cls_n = defaultdict(int)
        for v, r in vehicles.items():
            cls_n[r["vehicle_class"]] += 1
            cls_tot[r["vehicle_class"]] += sum(actual.get((mo, f"VEH-{v}", g), 0.0) for mo in MONTHS)
        for v, r in sorted(vehicles.items()):
            avg = cls_tot[r["vehicle_class"]] / cls_n[r["vehicle_class"]] / sum(
                _month_days(m) for m in MONTHS)
            for mo in MONTHS:
                rows.append({"month": mo, "rcntr": "EC-TP", "aufnr": f"VEH-{v}", "group": g,
                             "budget_idr": round(avg * _month_days(mo) * uplift)})
    gen = sum(actual.get((mo, "EC-GEN", "FUEL"), 0.0) for mo in MONTHS) / len(MONTHS)
    for mo in MONTHS:
        rows.append({"month": mo, "rcntr": "EC-GEN", "aufnr": "", "group": "FUEL",
                     "budget_idr": round(gen * 1.05)})

    # Roads: per division, a year's grading plan and the culvert contingency, by month.
    seg_by_div = defaultdict(list)
    for r in inp["roads"]:
        seg_by_div[_div(r["division_code"])].append(r)
    for d, segs in seg_by_div.items():
        yearly = sum(_f(s["length_km"]) * GRADING_IDR_PER_KM * 365 / GRADING_INTERVAL.get(s["condition"], 100)
                     + int(s["culvert_repairs_12m"]) * 11_500_000 for s in segs)
        for mo in MONTHS:
            rows.append({"month": mo, "rcntr": f"EC-D{d}", "aufnr": "", "group": "ROADS",
                         "budget_idr": round(yearly / 365 * _month_days(mo) * 1.05)})

    # The store: a 6% price escalation reserve on receipts, and a small count allowance.
    receipts = defaultdict(float)
    for mv in inp["movements"]:
        if mv["bwart"] == "101" and _in_window(mv["budat"]):
            receipts[mv["budat"][:7]] += _f(mv["menge"]) * _f(inp["materials"][mv["matnr"]]["verpr"])
    for mo in MONTHS:
        rows.append({"month": mo, "rcntr": "EC-STORE", "aufnr": "", "group": "PRICE",
                     "budget_idr": round(receipts[mo] * 0.06 + 5_000_000)})
    return rows


# ── block cost rewrite ─────────────────────────────────────────────────────

def rewrite_block_costs(inp, postings) -> int:
    """ec_block_costs.csv for the window, rolled up from the ledger; forward months kept."""
    cat = {"6101000": "harvest_idr", "6101100": "harvest_idr", "6101200": "harvest_idr",
           "6102000": "upkeep_idr", "6202000": "upkeep_idr", "6103000": "upkeep_idr",
           "6202100": "upkeep_idr", "6201000": "fertilizer_idr", "6104000": "fertilizer_idr",
           "6302000": "infrastructure_idr"}
    acc = defaultdict(lambda: defaultdict(float))
    for p in postings:
        c = cat.get(p["racct"])
        if c and p["block_code"]:
            acc[(p["budat"][:7], p["division_code"], p["block_code"])][c] += p["wsl"]

    # Transport: each vehicle's diesel by the trip litres it burned on each block.
    veh_month = defaultdict(float)
    for p in postings:
        if p["racct"] == "6203000" and p["vehicle_id"]:
            veh_month[(p["budat"][:7], p["vehicle_id"])] += p["wsl"]
    burn = defaultdict(float)
    burn_v = defaultdict(float)
    for t in inp["trips"]:
        k = (t["month"], t["vehicle_id"])
        burn_v[k] += _f(t["diesel_l"])
        burn[(t["month"], t["vehicle_id"], _div(t["division_code"]), _blk(t["block_code"]))] += _f(t["diesel_l"])
    for (mo, v, d, b), l in burn.items():
        if burn_v[(mo, v)]:
            acc[(mo, d, b)]["transport_idr"] += veh_month[(mo, v)] * l / burn_v[(mo, v)]

    # Overhead: the division's overhead by hectare.
    div_oh = defaultdict(float)
    div_ha = defaultdict(float)
    for p in postings:
        if GROUP_OF[p["racct"]] == "OVERHEAD" and p["rcntr"].startswith("EC-D"):
            div_oh[(p["budat"][:7], p["rcntr"][4:])] += p["wsl"]
    for (d, b), h in inp["ha"].items():
        div_ha[d] += h
    for (d, b), h in inp["ha"].items():
        for mo in MONTHS:
            acc[(mo, d, b)]["overhead_idr"] += div_oh[(mo, d)] * h / div_ha[d] if div_ha[d] else 0.0

    old = inp["block_costs"]
    km = {(_div(r["division_code"]), _blk(r["block_code"])): r["km_to_mill"] for r in old}
    rows = []
    for r in old:
        if r["month"] in MONTHS:
            continue
        rows.append(r)
    fields = ["harvest_idr", "upkeep_idr", "fertilizer_idr", "transport_idr", "overhead_idr",
              "infrastructure_idr"]
    window = []
    for (d, b) in sorted(inp["ha"], key=lambda k: (int(k[0]), int(k[1]))):
        for mo in MONTHS:
            a = acc.get((mo, d, b), {})
            window.append({"division_code": d, "block_code": b, "month": mo,
                           **{f: int(round(a.get(f, 0.0))) for f in fields},
                           "km_to_mill": km.get((d, b), "")})
    rows = window + rows
    rows.sort(key=lambda r: (int(r["division_code"]), int(r["block_code"]), r["month"]))
    return _write("ec_block_costs.csv", rows,
                  ["division_code", "block_code", "month"] + fields + ["km_to_mill"],
                  "Per-block monthly cost in IDR. 2025-01..2025-05 are ROLLED UP from the CO "
                  "ledger (ec_co_postings.csv, build_finance.py): wages from work orders, "
                  "materials from goods issues, diesel by trip litres, overhead by hectare. "
                  "Forward months stay allocated top-down at ~30M IDR/ha/yr.")


# ── the answer key ─────────────────────────────────────────────────────────

def leak_key(planted: dict) -> list:
    k = []

    def row(leak_id, kind, function, unit_kind, unit_id, driver, months, idr, cause, mechanism):
        k.append({"leak_id": leak_id, "kind": kind, "function": function, "unit_kind": unit_kind,
                  "unit_id": unit_id, "driver": driver, "months": ";".join(months),
                  "planted_idr": round(idr) if idr is not None else "", "true_cause": cause,
                  "mechanism": mechanism})

    row("fuel_tr04", "planted", "transport", "vehicle", FUEL_VEHICLE, "fuel_gap",
        ["2025-02", "2025-03", "2025-04"], planted["fuel_idr"], "siphoning",
        f"{FUEL_DRIVER}'s fuel card fills, near tank size, only on days {FUEL_DRIVER} drove "
        f"{FUEL_VEHICLE}; no other driver's fills on it changed")
    row("ot_g302", "planted", "workforce", "crew", OT_GANG, "overtime",
        ["2025-02", "2025-03", "2025-04", "2025-05"], planted["overtime_idr"], "standing_overtime",
        "overtime on about 60% of the gang's days at random, unrelated to bunches cut, rain or headcount")
    row("pay_u402", "planted", "workforce", "crew", PAD_CREW, "pay_over_attendance",
        ["2025-03", "2025-04", "2025-05"], planted["padding_idr"], "ghost_names",
        f"{PAD_MD} man-days paid above the muster on every day the crew has a muster row, a constant gap")
    row("fert_app_d5", "planted", "fertiliser", "division", FERT_DIVISION, "application_labour",
        ["2025-03", "2025-04", "2025-05"], planted["fert_idr"], "padded_timesheets",
        "a second contract sheet 5-9 days after each issue, 60% of standard on top, on every "
        "block alike regardless of slope or distance; the same contractor elsewhere is normal")
    row("price_urea", "planted", "stores", "material", PRICE_MATNR, "price_variance",
        ["2025-02", "2025-03", "2025-04"], planted["price_idr"], "vendor_over_contract",
        f"{PRICE_LIFNR} invoices urea {PRICE_UPLIFT:.0%} above its own PO price; PO prices and "
        "rush buys are normal")
    row("fleet_du01", "planted", "fleet", "vehicle", FLEET_VEHICLE, "breakdowns",
        ["2025-02", "2025-03", "2025-04", "2025-05"], planted["fleet_idr"], "repeat_fault",
        "the injector pump re-repaired every 18-24 days by the same local workshop")
    row("culvert_repeat", "planted", "roads", "segment", planted["culvert_segment"] or "",
        "repeat_repairs", ["2025-01", "2025-03", "2025-04"], planted["culvert_idr"], "poor_repair",
        "three culvert repairs inside the window, about 40 days apart, each dearer than usual")
    row("grading_d6", "planted", "roads", "division", GRADING_DIVISION, "billing_over_length",
        ["2025-02", "2025-03", "2025-04", "2025-05"], planted["grading_idr"],
        "contractor_overbilling",
        f"grading in Division {GRADING_DIVISION} billed at {GRADING_UPLIFT}x the segment length "
        "from February; the same contractor's other divisions are normal")
    row("tank_tr06", "counterfactual", "transport", "vehicle", TANK_VEHICLE, "fuel_gap",
        ["2025-02", "2025-03", "2025-04", "2025-05"], planted.get("tank_idr", 0.0), "tank_leak",
        "card top-ups about a third over the trip burn on every working day by whoever drove, plus "
        "small yard top-ups on idle days")
    row("ot_g501", "counterfactual", "workforce", "crew", RUSH_GANG, "overtime",
        ["2025-02", "2025-03", "2025-04", "2025-05"], planted.get("rounds_ot_idr", 0.0), "rounds_too_big",
        "overtime only on above-median crop days when the gang mustered short of 95% of establishment")
    row("pay_u603", "counterfactual", "workforce", "crew", LATE_MUSTER_CREW, "pay_over_attendance",
        ["2025-03", "2025-04", "2025-05"], planted.get("late_muster_idr", 0.0), "muster_not_recorded",
        "half the absentees paid as present on days with four or more absent, nothing on fuller days")
    row("shrink_d07", "existing", "transport", "driver", "D-07", "shrinkage", ["2025-03"], None,
        "theft_in_transit",
        "build_synthetic plants 8.5-16% light trips on D-07's loads, 2025-03-10 to 2025-03-23")
    row("loose_fruit", "existing", "harvesting", "estate", "", "loose_fruit", list(MONTHS), None,
        "", "the client's own loose fruit counts against the best quarter of blocks")
    row("queue_ffa", "existing", "transport", "estate", "", "queue_ffa", list(MONTHS), None, "",
        "the generator lengthens the mill queue after noon")
    for did, fn, unit_kind, mech in (
            ("decoy_terrain", "harvesting", "block",
             "steep, sparse, far blocks cost more per tonne honestly"),
            ("decoy_prescribed", "fertiliser", "block",
             "weak blocks are prescribed more fertiliser (ec_fertiliser.csv)"),
            ("decoy_lebaran", "workforce", "estate",
             "attendance dips 2025-03-24 to 2025-04-09 estate-wide"),
            ("decoy_skipped_upkeep", "upkeep", "block",
             "overdue upkeep rounds: cheap this month, deferred cost later")):
        row(did, "decoy", fn, unit_kind, "", "", list(MONTHS), None, "within_norm", mech)
    return k


# ── build ──────────────────────────────────────────────────────────────────

def build() -> dict:
    before = _hashes()
    rng = random.Random(f"{SEED}:finance")
    inp = _inputs()
    L = Ledger()

    gl, cc, orders = masters(inp)
    wage_rows, p_lab = post_labour(inp, L, rng)
    fuel_log, invoices, p_mat = post_materials(inp, L, rng)
    sheets, p_fert = post_fert_application(inp, L, rng)
    jobs, p_fleet = post_fleet(inp, L, rng)
    road_rows, p_road = post_roads(inp, L, rng)
    post_overhead_and_revenue(inp, L)

    postings = sorted(L.rows, key=lambda p: (p["budat"], p["racct"], p["aufnr"], p["src_id"]))
    for i, p in enumerate(postings):
        p["belnr"] = f"{100000001 + i:010d}"
    budget_rows = budget(inp, postings)
    planted = {**p_lab, **p_mat, "fert_idr": p_fert["idr"], "fert_blocks": p_fert["blocks"],
               "fleet_idr": p_fleet["idr"], "fleet_jobs": p_fleet["jobs"], **p_road}
    key = leak_key(planted)

    _write("ec_fi_gl_accounts.csv", gl, ["racct", "txt", "group", "kind"],
           "Chart of accounts (SKA1 / cost elements). `group` is the cost-element group the "
           "budget is set by.")
    _write("ec_co_cost_centres.csv", cc, ["kostl", "name", "kind", "division_code"],
           "Cost centres (CSKS): seven divisions and the service centres.")
    _write("ec_co_orders.csv", orders, ["aufnr", "kind", "object", "kostl", "text"],
           "Internal orders (AUFK): one per block, vehicle and road segment, the way most SAP "
           "plantation setups carry a block. Confirm against the client's CO design.")
    _write("ec_co_postings.csv", postings, POSTING_HEADER,
           "CO line items (ACDOCA / KSB1-KOB1), 2025-01-01 to 2025-05-23, POSTED from the work: "
           "src_type and src_id name the work order, payroll day, goods issue, fuel fill, "
           "invoice, contract sheet, job card or road order behind each line. Revenue is "
           "negative. Built by gis/build_finance.py.")
    _write("ec_co_budget.csv", budget_rows, ["month", "rcntr", "aufnr", "group", "budget_idr"],
           "CO plan version 0 by object, month and cost-element group, set the way estates set "
           "it: last year's tonnes at the average rate, hectares at the average rate, the "
           "fertiliser programme at a budget price, diesel by class with a generous allowance.")
    _write("ec_hr_wage_types.csv", wage_rows,
           ["crew_code", "crew_type", "division_code", "month", "present_md", "paid_md",
            "order_md", "basic_idr", "premium_idr", "overtime_h", "overtime_idr"],
           "Payroll results by wage type per crew and month. present_md is the muster "
           "(ec_attendance.csv); paid_md is what payroll paid; order_md what reached a work order.")
    _write("ec_fuel_log.csv", fuel_log,
           ["fill_id", "date", "vehicle_id", "driver_id", "source", "litres", "kostl",
            "price_idr_l", "src_id"],
           "Every diesel fill. source=store splits the division tank's daily issue (MB51 201 "
           "on FU-001) across the vehicles by the litres their trips burned; source=card is a "
           "fleet-card fill at an outside station, invoiced by the card vendor.")
    _write("ec_fi_invoices.csv", invoices,
           ["belnr", "budat", "lifnr", "ebeln", "ebelp", "bsart", "matnr", "menge", "meins",
            "po_price", "invoice_price", "amount_idr", "mblnr"],
           "Supplier invoices (MIRO, RBKP/RSEG) against each goods receipt in the window. "
           "(invoice_price - standard) x quantity posts to purchase price variance.")
    _write("ec_fert_application.csv", sheets,
           ["sheet_id", "date", "division_code", "block_code", "round", "material", "issue_date",
            "kg_applied", "man_days", "lifnr", "rate_idr_per_md", "amount_idr"],
           "Fertiliser application contract sheets (borongan), one or more per block issue in "
           "the 2025-03 round. Paid per man-day at the contract rate.")
    _write("ec_pm_services.csv", jobs,
           ["job_id", "date", "vehicle_id", "vehicle_class", "kind", "fault", "lifnr",
            "cost_idr", "downtime_h"],
           "Workshop jobs beside the breakdown orders (ec_pm_orders.csv): routine services and "
           "outside repairs, with cost and downtime.")
    _write("ec_road_orders.csv", road_rows,
           ["order_id", "date", "segment_id", "division_code", "block_code", "activity", "lifnr",
            "length_km", "km_billed", "cost_idr", "condition"],
           "Road grading and culvert orders for the 12 months to 2025-05-23. Culvert counts "
           "match culvert_repairs_12m in ec_roads.csv. Only window rows post to the ledger.")
    _write("ec_fi_leak_key.csv", key,
           ["leak_id", "kind", "function", "unit_kind", "unit_id", "driver", "months",
            "planted_idr", "true_cause", "mechanism"],
           "ANSWER KEY. What build_finance.py planted, and the decoys that must not come back. "
           "The engine and the model never read this file; only the recovery checks and the "
           "diagnosis eval do.")
    rewrite_block_costs(inp, postings)

    checks = _checks(inp, postings, wage_rows, fuel_log, planted)
    checks["other_feeds_unchanged"] = before == _hashes()
    log.info("[finance] %d postings, checks: %s", len(postings),
             {k: v for k, v in checks.items() if isinstance(v, bool)})
    mpath = OUT / "manifest.json"
    if mpath.exists():
        try:
            man = json.loads(mpath.read_text(encoding="utf-8"))
            man["finance_checks"] = checks
            mpath.write_text(json.dumps(man, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            pass
    return {"postings": len(postings), "planted": {k: (round(v) if isinstance(v, float) else v)
                                                   for k, v in planted.items()}, "checks": checks}


def _checks(inp, postings, wage_rows, fuel_log, planted) -> dict:
    """Reconciliation: the ledger agrees with every feed it was posted from."""
    out = {}
    # Wages in man-days: posted = payroll paid.
    md_posted = sum(_f(p["quantity"]) for p in postings
                    if p["racct"] in ("6101000", "6102000", "6103000", "6105000"))
    md_paid = sum(r["paid_md"] for r in wage_rows)
    out["wage_md_posted"] = round(md_posted, 1)
    out["wage_md_paid"] = round(md_paid, 1)
    out["wages_reconcile"] = abs(md_posted - md_paid) < 1.0
    # Materials at standard = MM issues x verpr.
    verpr = {m: _f(r["verpr"]) for m, r in inp["materials"].items()}
    mm = sum(_f(mv["menge"]) * verpr[mv["matnr"]] for mv in inp["movements"]
             if mv["bwart"] in ("201", "261") and _in_window(mv["budat"]))
    led = sum(p["wsl"] for p in postings if p["src_type"] in ("matdoc",)
              and p["racct"] in ("6201000", "6202000", "6202100", "6203000", "6204000"))
    led += sum(p["wsl"] for p in postings if p["src_type"] == "fuel_fill" and not p["lifnr"])
    out["materials_mm_idr"] = round(mm)
    out["materials_ledger_idr"] = round(led)
    out["materials_reconcile"] = abs(mm - led) < max(1000, 1e-6 * mm)
    # Fuel: store fills per division-day = the MM issue.
    mm_fuel = defaultdict(float)
    for mv in inp["movements"]:
        if mv["bwart"] == "201" and mv["matnr"] == "FU-001" and _in_window(mv["budat"]):
            mm_fuel[(mv["budat"], mv["kostl"])] += _f(mv["menge"])
    log_fuel = defaultdict(float)
    for f in fuel_log:
        if f["source"] == "store":
            log_fuel[(f["date"], f["kostl"])] += f["litres"]
    out["fuel_store_reconcile"] = all(abs(mm_fuel[k] - log_fuel.get(k, 0.0)) < 0.5 for k in mm_fuel)
    # Ledger total per hectare-year, unplanted part.
    tot_ha = sum(inp["ha"].values())
    days = (WINDOW_END - WINDOW_START).days + 1
    cost = sum(p["wsl"] for p in postings if p["racct"] != "4101000")
    out["cost_idr_ha_yr"] = round(cost / tot_ha * 365 / days)
    out["planted_ha"] = round(tot_ha, 1)
    out["cost_in_band"] = 20e6 <= out["cost_idr_ha_yr"] <= 40e6
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = build()
    print(json.dumps(res, indent=2))
