"""Synthetic SAP MM records: the estate store, its suppliers and 24 months of stock.

Run once:  python gis/build_materials.py
           (also run at the end of gis/build_synthetic.py)
Writes:    gis/data/synthetic/ec_mm_materials.csv        MARA / MARC / MBEW
           gis/data/synthetic/ec_mm_vendors.csv          LFA1
           gis/data/synthetic/ec_mm_purchase_orders.csv  EKKO / EKPO / EKET
           gis/data/synthetic/ec_mm_movements.csv        MATDOC
           gis/data/synthetic/ec_mm_reservations.csv     RESB
           gis/data/synthetic/ec_mm_stock.csv            MARD, on 2025-05-23
Rewrites:  gis/data/synthetic/ec_fertiliser_stock.csv
           so the Nutrition panel's stock agrees with the store's.

What this is for
----------------
The EC export carries no MM data. Every input the palms cannot grow -
fertiliser, herbicide, pest chemicals, diesel, spare parts - moves through a
store that SAP records and the export never saw. The stores window
(buildplan_stores.md) needs that record: what went out, what came in, when it
was ordered and when it actually arrived.

Four rules, each of which is easy to get wrong
-----------------------------------------------
1. Use reconciles to the operations ledger. From 2025-01-01 every issue is
   read off a work order, a trip or a breakdown: fertiliser from
   ec_fertiliser.csv to the kilogram, glyphosate from the spraying orders,
   diesel from the weighbridge trips, one part per PM order. Before 2025 the
   ledger does not exist, and issues come from the same drivers at monthly
   grain. Those rows carry no order number, which is how to tell them apart.

2. The store runs on SAP's own settings. Reorder points, safety stocks and
   planned delivery times are set the way a store sets them (two weeks of
   average use; the supplier's quoted lead time), and the history is
   simulated under them. That makes the settings a fair baseline: the policy
   the recorded stock actually came from.

3. A stockout is a rush order. The operations ledger was generated without
   regard to stock, so an issue cannot be refused. When the store cannot meet
   one, it buys the shortfall locally at a premium, as estates do. The need is
   therefore always recorded, and the premium is what running out cost.

4. This generator moves nothing else. It draws from its own seed, runs after
   build_operations, and reads every feed off disk. Its checks hash what it
   read before and after, so a change to any other feed fails the build.

What is planted (and must be recovered, see gis/models/leadtime.py)
-------------------------------------------------------------------
    quoted vs real      each supplier's true median is its quote x 1.15-1.55
    wet season          POs due December-March take x1.25 by sea, x1.4 by road
    missed vessel       Pupuk Kaltim: 12% of POs arrive 21-35 days late
    split deliveries    Meroke Tetap Jaya delivers 30% of POs in two receipts
    storage loss        Urea 0.6% a month, Kieserite 0.2%, found at counts
    handling loss       herbicide issued = sprayed ha x dose x 1.08
    NOT planted         order size and PO weekday have no effect on lead time

`delay_cause` on the PO file is the answer key. The loader strips it.
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

from gis.build_synthetic import OUT, SEED, _write
from gis.build_operations import WINDOW_END, WINDOW_START, _read

log = logging.getLogger("estate-command.materials")

MM_START = date(2023, 6, 1)
PLANT = "EC"
SLOC = "CS01"

# ── suppliers ──────────────────────────────────────────────────────────────
# The first three names are already in the stock feed. The rest are invented.
VENDORS = [
    {"lifnr": "V101", "name": "PT Pupuk Kaltim", "city": "Bontang", "route": "sea", "quoted_days": 30},
    {"lifnr": "V102", "name": "PT Petrokimia Gresik", "city": "Gresik", "route": "sea", "quoted_days": 35},
    {"lifnr": "V103", "name": "PT Meroke Tetap Jaya", "city": "Surabaya", "route": "sea", "quoted_days": 28},
    {"lifnr": "V201", "name": "PT Agro Sarana Timur", "city": "Surabaya", "route": "sea", "quoted_days": 21},
    {"lifnr": "V301", "name": "CV Depot Minyak Merauke", "city": "Merauke", "route": "road", "quoted_days": 5},
    {"lifnr": "V401", "name": "PT Suku Cadang Jayapura", "city": "Jayapura", "route": "sea", "quoted_days": 10},
    {"lifnr": "V402", "name": "PT Teknik Mesin Surabaya", "city": "Surabaya", "route": "sea", "quoted_days": 21},
    {"lifnr": "V900", "name": "Local traders, Merauke (rush buys)", "city": "Merauke", "route": "road",
     "quoted_days": 2},
]
VENDOR = {v["lifnr"]: v for v in VENDORS}
RUSH_LIFNR = "V900"

# The planted truth about lead times. Nothing below is written to a feed.
PLANTED = {
    "V101": {"ratio": 1.40, "tail": 0.12},
    "V102": {"ratio": 1.25},
    "V103": {"ratio": 1.55, "split": 0.30},
    "V201": {"ratio": 1.30},
    "V301": {"ratio": 1.20},
    "V401": {"ratio": 1.20},
    "V402": {"ratio": 1.50},
}
WET_MONTHS = (12, 1, 2, 3)
SEASON = {"sea": 1.25, "road": 1.40}
SPREAD = {"sea": 0.18, "road": 0.25}
TAIL_DAYS = (21, 35)
SPLIT_SHARE = (0.55, 0.75)

# Rush buys: premium over the moving average price, by material group.
RUSH_PREMIUM = {"FERT": 0.30, "AGCH": 0.25, "FUEL": 0.12, "SPARE": 0.40}
RUSH_DAYS = (1, 3)

# Storage loss per month of stock held, found and scrapped at quarterly counts.
STORAGE_LOSS = {"FE-002": 0.006, "FE-003": 0.002}

# ── materials ──────────────────────────────────────────────────────────────
# lot: the fixed order quantity the store uses today (a vessel lot, a tanker).
# rush_lot: a rush buy comes by the truck or the carton, not by the bag.
# lot_days: where no fixed lot exists, the lot covers this many days of use.
MATERIALS = [
    {"matnr": "FE-001", "maktx": "NPK 12-12-17-2", "matkl": "FERT", "meins": "KG", "lifnr": "V101",
     "verpr": 8_500, "bstrf": 50, "lot": 250_000, "rush_lot": 10_000},
    {"matnr": "FE-002", "maktx": "Urea", "matkl": "FERT", "meins": "KG", "lifnr": "V101",
     "verpr": 7_500, "bstrf": 50, "lot": 100_000, "rush_lot": 10_000},
    {"matnr": "FE-003", "maktx": "Kieserite", "matkl": "FERT", "meins": "KG", "lifnr": "V103",
     "verpr": 4_500, "bstrf": 50, "lot": 100_000, "rush_lot": 10_000},
    {"matnr": "FE-004", "maktx": "Borate", "matkl": "FERT", "meins": "KG", "lifnr": "V102",
     "verpr": 22_000, "bstrf": 25, "lot": 15_000, "rush_lot": 1_000},
    {"matnr": "AC-001", "maktx": "Glyphosate 480 SL", "matkl": "AGCH", "meins": "L", "lifnr": "V201",
     "verpr": 70_000, "bstrf": 20, "lot_days": 40, "rush_lot": 200},
    {"matnr": "AC-002", "maktx": "Metsulfuron-methyl 20 WG", "matkl": "AGCH", "meins": "KG", "lifnr": "V201",
     "verpr": 600_000, "bstrf": 1, "lot_days": 45, "rush_lot": 5},
    {"matnr": "AC-003", "maktx": "Hexaconazole 5 SC", "matkl": "AGCH", "meins": "L", "lifnr": "V201",
     "verpr": 90_000, "bstrf": 5, "lot_days": 45, "rush_lot": 20},
    {"matnr": "AC-004", "maktx": "Rodenticide bait", "matkl": "AGCH", "meins": "KG", "lifnr": "V201",
     "verpr": 45_000, "bstrf": 5, "lot_days": 90, "rush_lot": 10},
    {"matnr": "FU-001", "maktx": "Diesel (B40)", "matkl": "FUEL", "meins": "L", "lifnr": "V301",
     "verpr": 13_500, "bstrf": 1_000, "lot": 8_000, "max_stock": 40_000, "rush_lot": 5_000},
    {"matnr": "SP-001", "maktx": "Alternator", "matkl": "SPARE", "meins": "EA", "lifnr": "V401",
     "verpr": 3_500_000, "bstrf": 1, "lot_days": 30, "fault": "alternator"},
    {"matnr": "SP-002", "maktx": "Injector pump", "matkl": "SPARE", "meins": "EA", "lifnr": "V402",
     "verpr": 9_000_000, "bstrf": 1, "lot_days": 30, "fault": "injector pump"},
    {"matnr": "SP-003", "maktx": "Radiator", "matkl": "SPARE", "meins": "EA", "lifnr": "V401",
     "verpr": 4_000_000, "bstrf": 1, "lot_days": 30, "fault": "radiator"},
    {"matnr": "SP-004", "maktx": "Wheel bearing", "matkl": "SPARE", "meins": "EA", "lifnr": "V402",
     "verpr": 650_000, "bstrf": 1, "lot_days": 30, "fault": "wheel bearing"},
    {"matnr": "SP-005", "maktx": "Hydraulic hose", "matkl": "SPARE", "meins": "EA", "lifnr": "V401",
     "verpr": 450_000, "bstrf": 1, "lot_days": 30, "fault": "hydraulic hose"},
    {"matnr": "SP-006", "maktx": "Clutch plate", "matkl": "SPARE", "meins": "EA", "lifnr": "V402",
     "verpr": 1_200_000, "bstrf": 1, "lot_days": 30, "fault": "clutch plate"},
    {"matnr": "SP-007", "maktx": "Brake shoe", "matkl": "SPARE", "meins": "EA", "lifnr": "V401",
     "verpr": 800_000, "bstrf": 1, "lot_days": 30, "fault": "brake shoe"},
    {"matnr": "SP-008", "maktx": "Tyre", "matkl": "SPARE", "meins": "EA", "lifnr": "V402",
     "verpr": 2_800_000, "bstrf": 1, "lot_days": 30, "fault": "tyre carcass"},
]
MATERIAL = {m["matnr"]: m for m in MATERIALS}
FERT_MATNR = {"NPK 12-12-17-2": "FE-001", "Urea": "FE-002", "Kieserite": "FE-003", "Borate": "FE-004"}

# Doses. The same figures sit in the assumption register, where the estate's
# agronomist can correct them; the store is generated at these values.
GLYPHOSATE_L_PER_HA = 1.0
METSULFURON_KG_PER_HA = 0.04
METSULFURON_SHARE = 0.35          # of spray rounds that add a broadleaf herbicide
HEXACONAZOLE_L_PER_PALM = 0.02
BAIT_KG_PER_PALM = 0.015
HANDLING_LOSS = 1.08              # planted: issued over dose, for herbicide
NON_HAUL_DIESEL_L = (650, 60)     # generators and crew transport, every day

# Fertiliser rounds. The two before 2024-10 are generated at block grain under
# gen_fertiliser's rules; 2024-10 and 2025-03 are ec_fertiliser.csv; 2025-10 is
# the programme on the books on 2025-05-23.
ROUNDS = [("2023-10", date(2023, 10, 15)), ("2024-03", date(2024, 3, 15)),
          ("2024-10", date(2024, 10, 15)), ("2025-03", date(2025, 3, 15)),
          ("2025-10", date(2025, 10, 15))]
PD_BUFFER_DAYS = 28               # the store orders a round this long before quote + target
PD_LOT_SPACING_DAYS = 3

SAFETY_DAYS = 14                  # SAP settings: safety stock is two weeks of average use
PD_SAFETY_SHARE = 0.02            # programme materials: 2% of the annual programme

COUNT_DAYS = ((3, 31), (6, 30), (9, 30), (12, 31))

MM_FILES = ("ec_mm_materials.csv", "ec_mm_vendors.csv", "ec_mm_purchase_orders.csv",
            "ec_mm_movements.csv", "ec_mm_reservations.csv", "ec_mm_stock.csv")
REWRITES = ("ec_fertiliser_stock.csv",)


# ── helpers ────────────────────────────────────────────────────────────────

def _days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _round_up(q: float, step: float) -> float:
    return math.ceil(q / step - 1e-9) * step


def _kostl(division) -> str:
    return f"EC-D{int(float(division))}"


def _hashes() -> dict:
    out = {}
    for p in sorted(OUT.glob("*.csv")):
        if p.name in MM_FILES or p.name in REWRITES:
            continue
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _qty(m: dict, q: float) -> float:
    return round(q, 0 if m["meins"] == "EA" else 2)


# ── drivers ────────────────────────────────────────────────────────────────

def _harvest_tonnes() -> dict:
    """Tonnes by (month, division), from the harvest history at the calibrated bunch weight."""
    abw = {(r["division_code"], r["block_code"]): float(r["abw_kg"]) for r in _read("ec_abw.csv")}
    mean_abw = sum(abw.values()) / len(abw)
    out: dict = defaultdict(float)
    for r in _read("ec_harvest_history.csv"):
        k = (r["division_code"], r["block_code"])
        out[(r["month"], r["division_code"])] += int(r["bunches"]) * abw.get(k, mean_abw) / 1000.0
    return out


def _working_days(month: str) -> list[date]:
    y, mo = int(month[:4]), int(month[5:7])
    d = date(y, mo, 1)
    out = []
    while d.month == mo:
        if d.weekday() != 6 and MM_START <= d <= WINDOW_END:
            out.append(d)
        d += timedelta(days=1)
    return out


# ── use ────────────────────────────────────────────────────────────────────

def _issue(d, qty, kostl, bwart="201", aufnr="", block="", rsnum=""):
    return {"date": d, "qty": qty, "kostl": kostl, "bwart": bwart, "aufnr": aufnr,
            "block_code": block, "rsnum": rsnum}


def gen_use(rng) -> tuple[dict, list, dict]:
    """Every issue the store had to meet, per material, and the reservations behind fertiliser."""
    from gis import environment
    rain = environment.rainfall_by_day("EC") or {}
    use: dict[str, list] = defaultdict(list)
    tonnes = _harvest_tonnes()
    divisions = sorted({d for (_, d) in tonnes})

    # Fertiliser: reservations per round, and the issues against them.
    fert = _read("ec_fertiliser.csv")
    reservations = []
    by_round = defaultdict(list)
    for r in fert:
        by_round[r["window"]].append(r)
    programme = by_round["2025-03"]           # the programme per block, which does not change
    rs_seq = 0
    for window, target in ROUNDS:
        if window in ("2024-10", "2025-03"):
            rows = by_round[window]
        else:
            rows = programme
        for r in rows:
            rs_seq += 1
            rsnum = f"{rs_seq:010d}"
            matnr = FERT_MATNR[r["material"]]
            bdmng = float(r["target_kg"])
            if window in ("2024-10", "2025-03"):
                issued = float(r["issued_kg"])
                when = date.fromisoformat(r["issue_date"])
            elif window == "2025-10":
                issued, when = 0.0, None
            else:
                # gen_fertiliser's rules, on this generator's own draws.
                fulfil = min(1.12, max(0.35, rng.gauss(0.88, 0.17)))
                issued = round(bdmng * fulfil, 1)
                when = target + timedelta(days=int(max(0, rng.gauss(16, 26))))
            posted = when is not None and when <= WINDOW_END
            reservations.append({
                "rsnum": rsnum, "matnr": matnr, "werks": PLANT, "bdter": target.isoformat(),
                "bdmng": round(bdmng, 1), "enmng": round(issued, 1) if posted else 0.0,
                "meins": "KG", "kzear": "X" if posted else "", "kostl": _kostl(r["division_code"]),
                "block_code": r["block_code"], "window": window,
            })
            if posted and issued > 0:
                use[matnr].append(_issue(when, issued, _kostl(r["division_code"]),
                                         block=r["block_code"], rsnum=rsnum))

    # Herbicide, 2025: every spraying order that got done.
    spray_2025 = 0.0
    for r in _read("ec_upkeep_orders.csv"):
        if r["activity"] != "spraying" or float(r["actual_qty"] or 0) <= 0:
            continue
        d = date.fromisoformat(r["date"])
        ha = float(r["actual_qty"])
        spray_2025 += ha
        k = _kostl(r["division_code"])
        use["AC-001"].append(_issue(d, round(ha * GLYPHOSATE_L_PER_HA * HANDLING_LOSS
                                             * math.exp(rng.gauss(0, 0.05)), 1),
                                    k, aufnr=r["order_id"], block=r["block_code"]))
        if rng.random() < METSULFURON_SHARE:
            use["AC-002"].append(_issue(d, round(ha * METSULFURON_KG_PER_HA * HANDLING_LOSS
                                                 * math.exp(rng.gauss(0, 0.05)), 2),
                                        k, aufnr=r["order_id"], block=r["block_code"]))

    # Herbicide, before 2025: the same spray days (off on Sundays and at 15 mm),
    # at the 2025 hectares per sprayable day.
    ok_2025 = sum(1 for d in _days(WINDOW_START, WINDOW_END)
                  if d.weekday() != 6 and rain.get(d.isoformat(), 0.0) < 15.0)
    ha_per_day = spray_2025 / max(ok_2025, 1)
    div_ha = defaultdict(float)
    for r in _read("ec_harvest_history.csv"):
        if r["month"] == "2025-01":
            div_ha[r["division_code"]] += float(r["planted_ha"])
    div_list = sorted(div_ha)
    div_w = [div_ha[d] for d in div_list]
    for d in _days(MM_START, WINDOW_START - timedelta(days=1)):
        if d.weekday() == 6 or rain.get(d.isoformat(), 0.0) >= 15.0:
            continue
        ha = ha_per_day * math.exp(rng.gauss(0, 0.25))
        k = _kostl(rng.choices(div_list, div_w)[0])
        use["AC-001"].append(_issue(d, round(ha * GLYPHOSATE_L_PER_HA * HANDLING_LOSS
                                             * math.exp(rng.gauss(0, 0.05)), 1), k))
        if rng.random() < METSULFURON_SHARE:
            use["AC-002"].append(_issue(d, round(ha * METSULFURON_KG_PER_HA * HANDLING_LOSS
                                                 * math.exp(rng.gauss(0, 0.05)), 2), k))

    # Pest chemicals, 2025: treatments and follow-ups, by the pest the block was treated for.
    treat = defaultdict(list)
    for t in _read("ec_pest_treatment.csv"):
        treat[(t["division_code"], t["block_code"])].append(t)

    def pest_of(r):
        cands = treat.get((r["division_code"], r["block_code"])) or []
        if not cands:
            return None
        d = date.fromisoformat(r["date"])
        key = "treated_date" if r["activity"] == "treatment" else "followup_done"

        def gap(t):
            v = t.get(key) or t.get("followup_due") or t["treated_date"]
            return abs((date.fromisoformat(v) - d).days)
        return min(cands, key=gap)["pest"]

    events_2025 = defaultdict(list)
    for r in _read("ec_pest_orders.csv"):
        if r["activity"] not in ("treatment", "followup") or int(r["actual_qty"] or 0) <= 0:
            continue
        pest = pest_of(r)
        palms = int(r["actual_qty"])
        d = date.fromisoformat(r["date"])
        k = _kostl(r["division_code"])
        if pest == "ganoderma":
            q = round(palms * HEXACONAZOLE_L_PER_PALM * math.exp(rng.gauss(0, 0.05)), 2)
            use["AC-003"].append(_issue(d, q, k, aufnr=r["order_id"], block=r["block_code"]))
            events_2025["AC-003"].append(q)
        elif pest == "rat":
            q = round(palms * BAIT_KG_PER_PALM * math.exp(rng.gauss(0, 0.05)), 2)
            use["AC-004"].append(_issue(d, q, k, aufnr=r["order_id"], block=r["block_code"]))
            events_2025["AC-004"].append(q)
        # Rhinoceros beetle is trapped with pheromone, which the store does not stock.

    # Pest chemicals, before 2025: the 2025 rate of jobs, each drawing a job size seen in 2025.
    window_days = (WINDOW_END - WINDOW_START).days + 1
    for matnr, sizes in events_2025.items():
        rate = len(sizes) / window_days
        for d in _days(MM_START, WINDOW_START - timedelta(days=1)):
            if d.weekday() == 6 or rng.random() >= rate * 7 / 6:
                continue
            use[matnr].append(_issue(d, rng.choice(sizes), _kostl(rng.choice(divisions))))

    # Diesel, 2025: every trip, by division, plus non-haul use.
    haul = defaultdict(float)
    for r in _read("ec_weighbridge.csv"):
        haul[(r["date"], r["division_code"])] += float(r["diesel_l"])
    haul_2025 = sum(haul.values())
    for (ds, div), litres in sorted(haul.items()):
        use["FU-001"].append(_issue(date.fromisoformat(ds), round(litres, 1), _kostl(div)))
    t_2025 = sum(v for (mo, _), v in tonnes.items() if "2025-01" <= mo <= "2025-05")
    l_per_t = haul_2025 / t_2025 if t_2025 else 0.0

    # Diesel, before 2025: the harvest history's tonnes at the 2025 litres per tonne.
    months = sorted({mo for (mo, _) in tonnes if MM_START.isoformat()[:7] <= mo < "2025-01"})
    for mo in months:
        wd = _working_days(mo)
        if not wd:
            continue
        for div in divisions:
            t = tonnes.get((mo, div), 0.0)
            for d in wd:
                litres = t / len(wd) * l_per_t * math.exp(rng.gauss(0, 0.12))
                if litres > 0:
                    use["FU-001"].append(_issue(d, round(litres, 1), _kostl(div)))
    for d in _days(MM_START, WINDOW_END):
        use["FU-001"].append(_issue(d, round(max(0.0, rng.gauss(*NON_HAUL_DIESEL_L)), 1), "EC-GEN"))

    # Spare parts, 2025: one part per breakdown order.
    part_of = {m["fault"]: m["matnr"] for m in MATERIALS if m.get("fault")}
    count_2025 = defaultdict(int)
    for r in _read("ec_pm_orders.csv"):
        matnr = part_of.get(r["fault"])
        if not matnr:
            continue
        use[matnr].append(_issue(date.fromisoformat(r["raised_date"]), 1, "EC-WS", bwart="261",
                                 aufnr=r["order_id"]))
        count_2025[matnr] += 1

    # Spare parts, before 2025: the 2025 breakdowns per tonne hauled, on the history's tonnes.
    for matnr, n in count_2025.items():
        per_t = n / t_2025 if t_2025 else 0.0
        for mo in months:
            wd = _working_days(mo)
            if not wd:
                continue
            lam = sum(tonnes.get((mo, dv), 0.0) for dv in divisions) * per_t / len(wd)
            for d in wd:
                # Poisson by inversion; the daily rate is well under one.
                k, p, u = 0, math.exp(-lam), rng.random()
                c = p
                while u > c:
                    k += 1
                    p *= lam / k
                    c += p
                for _ in range(k):
                    use[matnr].append(_issue(d, 1, "EC-WS"))

    for v in use.values():
        v.sort(key=lambda e: (e["date"], e["kostl"], e["aufnr"], e["rsnum"]))
    drivers = {"spray_ha_2025": round(spray_2025, 1), "ha_per_sprayable_day": round(ha_per_day, 2),
               "haul_diesel_l_2025": round(haul_2025), "harvest_t_2025": round(t_2025),
               "haul_l_per_t": round(l_per_t, 3)}
    return use, reservations, drivers


# ── SAP's settings ─────────────────────────────────────────────────────────

def settings(use: dict) -> dict:
    """Material master as the store set it: two weeks of average use, the quoted lead time."""
    days = (WINDOW_END - MM_START).days + 1
    out = {}
    for m in MATERIALS:
        v = VENDOR[m["lifnr"]]
        total = sum(e["qty"] for e in use.get(m["matnr"], []))
        avg = total / days
        if m["matkl"] == "FERT":
            dismm = "PD"
            eisbe = _round_up(avg * 365 * PD_SAFETY_SHARE, m["bstrf"])
            minbe = 0.0
            lot = m["lot"]
        else:
            dismm = "VB"
            eisbe = _round_up(avg * SAFETY_DAYS, m["bstrf"])
            minbe = _round_up(avg * v["quoted_days"] + eisbe, m["bstrf"])
            lot = m.get("lot") or max(m["bstrf"], _round_up(avg * m["lot_days"], m["bstrf"]))
        out[m["matnr"]] = {**m, "dismm": dismm, "eisbe": eisbe, "minbe": minbe, "plifz": v["quoted_days"],
                           "lot": lot, "avg_daily": avg}
    return out


# ── the store, day by day ──────────────────────────────────────────────────

def _lead(lifnr: str, bedat: date, rng) -> dict:
    v, p = VENDOR[lifnr], PLANTED[lifnr]
    eindt = bedat + timedelta(days=v["quoted_days"])
    f = SEASON[v["route"]] if eindt.month in WET_MONTHS else 1.0
    days = v["quoted_days"] * p["ratio"] * f * math.exp(rng.gauss(0, SPREAD[v["route"]]))
    cause = "wet_season" if f > 1.0 else ""
    if rng.random() < p.get("tail", 0.0):
        days += rng.uniform(*TAIL_DAYS)
        cause = "vessel_missed"
    lead = max(1, int(round(days)))
    split = None
    if rng.random() < p.get("split", 0.0):
        split = (max(1, int(round(lead * 0.75))), rng.uniform(*SPLIT_SHARE))
    return {"eindt": eindt, "lead": lead, "cause": cause, "split": split}


def simulate(s: dict, events: list, reservations: list, rng) -> dict:
    """One material through 24 months under its SAP settings."""
    m, lifnr = s, s["lifnr"]
    by_day = defaultdict(list)
    for e in events:
        by_day[e["date"]].append(e)
    pos, receipts, issues, counts = [], defaultdict(list), [], []
    on_hand = s["eisbe"] if s["dismm"] == "PD" else s["minbe"] + s["lot"] / 2
    on_hand = _round_up(on_hand, m["bstrf"])
    opening = on_hand
    open_qty = 0.0
    loss_acc = 0.0
    loss_rate = STORAGE_LOSS.get(m["matnr"], 0.0)

    # Programme materials: each round's order plan, fixed on its trigger day.
    triggers = {}
    if s["dismm"] == "PD":
        for window, target in ROUNDS:
            trig = target - timedelta(days=s["plifz"] + PD_BUFFER_DAYS)
            if MM_START <= trig <= WINDOW_END:
                triggers[trig] = sum(r["bdmng"] for r in reservations
                                     if r["matnr"] == m["matnr"] and r["window"] == window)
    planned_lots = defaultdict(list)

    def place(d, qty, rush=False):
        nonlocal open_qty
        if rush:
            k = rng.randint(*RUSH_DAYS)
            po = {"bedat": d - timedelta(days=k), "lifnr": RUSH_LIFNR, "bsart": "RUSH",
                  "menge": qty, "eindt": d - timedelta(days=k) + timedelta(days=VENDOR[RUSH_LIFNR]["quoted_days"]),
                  "netpr": round(m["verpr"] * (1 + RUSH_PREMIUM[m["matkl"]]) * rng.uniform(0.97, 1.03)),
                  "delay_cause": "", "receipts": [(d, qty)]}
            pos.append(po)
            return po
        lt = _lead(lifnr, d, rng)
        po = {"bedat": d, "lifnr": lifnr, "bsart": "NB", "menge": qty, "eindt": lt["eindt"],
              "netpr": round(m["verpr"] * rng.uniform(0.97, 1.03)), "delay_cause": lt["cause"],
              "receipts": []}
        if lt["split"]:
            first_days, share = lt["split"]
            q1 = _qty(m, _round_up(qty * share, m["bstrf"]))
            po["receipts"] = [(d + timedelta(days=first_days), q1), (d + timedelta(days=lt["lead"]), qty - q1)]
        else:
            po["receipts"] = [(d + timedelta(days=lt["lead"]), qty)]
        for when, q in po["receipts"]:
            receipts[when].append((po, q))
        open_qty += qty
        pos.append(po)
        return po

    for d in _days(MM_START, WINDOW_END):
        for po, q in receipts.pop(d, []):
            on_hand += q
            open_qty -= q
            issues.append({"date": d, "bwart": "101", "qty": q, "po": po})
        for e in by_day.get(d, []):
            if on_hand + 1e-9 < e["qty"]:
                short = _round_up(e["qty"] - on_hand, m.get("rush_lot") or m["bstrf"])
                po = place(d, short, rush=True)
                on_hand += short
                issues.append({"date": d, "bwart": "101", "qty": short, "po": po})
            on_hand -= e["qty"]
            issues.append({"date": d, "bwart": e["bwart"], "qty": e["qty"], "event": e})
        loss_acc += on_hand * loss_rate / 30.4375
        if (d.month, d.day) in COUNT_DAYS:
            if loss_rate and loss_acc >= m["bstrf"] * 0.02:
                q = _qty(m, min(on_hand, loss_acc))
                if q > 0:
                    on_hand -= q
                    issues.append({"date": d, "bwart": "551", "qty": q})
                loss_acc = 0.0
            elif not loss_rate and on_hand > 0 and rng.random() < 0.25:
                q = _qty(m, on_hand * rng.uniform(0.0005, 0.003))
                if q > 0:
                    gain = rng.random() < 0.4
                    on_hand += q if gain else -q
                    issues.append({"date": d, "bwart": "701" if gain else "702", "qty": q})
            counts.append(d)
        if s["dismm"] == "PD":
            if d in triggers:
                need = triggers[d] - on_hand - open_qty + s["eisbe"]
                if need > 0:
                    n = max(1, math.ceil(need / s["lot"]))
                    each = _round_up(need / n, m["bstrf"])
                    for i in range(n):
                        planned_lots[d + timedelta(days=PD_LOT_SPACING_DAYS * i)].append(each)
            for q in planned_lots.pop(d, []):
                place(d, q)
        else:
            cap = s.get("max_stock")
            n = 0
            while on_hand + open_qty <= s["minbe"] + 1e-9 and n < 3:
                if cap and on_hand + open_qty + s["lot"] > cap:
                    break
                place(d, s["lot"])
                n += 1
    return {"pos": pos, "moves": issues, "on_hand": on_hand, "opening": opening, "counts": counts}


# ── entry point ────────────────────────────────────────────────────────────

def build() -> dict:
    before = _hashes()
    rng = random.Random(f"{SEED}:materials")
    use, reservations, drivers = gen_use(rng)
    master = settings(use)

    runs = {}
    for m in MATERIALS:
        runs[m["matnr"]] = simulate(master[m["matnr"]], use.get(m["matnr"], []), reservations, rng)

    # Number POs and material documents in date order, as SAP would have.
    all_pos = [(po, matnr) for matnr, r in runs.items() for po in r["pos"]]
    all_pos.sort(key=lambda x: (x[0]["bedat"], x[1], x[0]["bsart"]))
    po_rows = []
    for i, (po, matnr) in enumerate(all_pos, 1):
        po["ebeln"] = f"45{i:08d}"
        received = sum(q for when, q in po["receipts"] if when <= WINDOW_END)
        status = ("closed" if received >= po["menge"] - 1e-9
                  else "partial" if received > 0 else "open")
        po_rows.append({
            "ebeln": po["ebeln"], "ebelp": "00010", "bedat": po["bedat"].isoformat(), "bsart": po["bsart"],
            "lifnr": po["lifnr"], "matnr": matnr, "werks": PLANT, "menge": _qty(MATERIAL[matnr], po["menge"]),
            "meins": MATERIAL[matnr]["meins"], "netpr": po["netpr"], "eindt": po["eindt"].isoformat(),
            "status": status, "delay_cause": po["delay_cause"],
        })

    moves = []
    for matnr, r in runs.items():
        m = MATERIAL[matnr]
        for mv in r["moves"]:
            e = mv.get("event") or {}
            po = mv.get("po")
            moves.append({
                "budat": mv["date"].isoformat(), "bwart": mv["bwart"], "matnr": matnr, "werks": PLANT,
                "lgort": SLOC, "menge": _qty(m, mv["qty"]), "meins": m["meins"],
                "shkzg": "S" if mv["bwart"] in ("101", "701") else "H",
                "kostl": e.get("kostl", "EC-STORE" if mv["bwart"] in ("551", "701", "702") else ""),
                "aufnr": e.get("aufnr", ""), "block_code": e.get("block_code", ""),
                "rsnum": e.get("rsnum", ""), "ebeln": po["ebeln"] if po else "",
                "ebelp": "00010" if po else "",
            })
    order = {"101": 0, "201": 1, "261": 1, "551": 2, "701": 3, "702": 3}
    moves.sort(key=lambda x: (x["budat"], x["matnr"], order[x["bwart"]]))
    for i, mv in enumerate(moves, 1):
        mv["mblnr"] = f"49{i:08d}"
        mv["zeile"] = "0001"

    # Stock on the last day, from the movements themselves.
    labst = defaultdict(float)
    for mv in moves:
        labst[mv["matnr"]] += mv["menge"] if mv["shkzg"] == "S" else -mv["menge"]
    last_count = max(d for r in runs.values() for d in r["counts"])
    stock_rows = [{"matnr": m["matnr"], "maktx": m["maktx"], "werks": PLANT, "lgort": SLOC,
                   "labst": _qty(m, labst[m["matnr"]] + runs[m["matnr"]]["opening"]),
                   "meins": m["meins"], "value_idr": round((labst[m["matnr"]] + runs[m["matnr"]]["opening"])
                                                           * m["verpr"]),
                   "last_count": last_count.isoformat()} for m in MATERIALS]

    mat_rows = [{
        "matnr": s["matnr"], "maktx": s["maktx"], "matkl": s["matkl"], "meins": s["meins"], "werks": PLANT,
        "lgort": SLOC, "dismm": s["dismm"], "minbe": _qty(s, s["minbe"]), "eisbe": _qty(s, s["eisbe"]),
        "plifz": s["plifz"], "bstrf": s["bstrf"], "bstfe": _qty(s, s["lot"]), "verpr": s["verpr"],
        "max_stock": s.get("max_stock") or "", "primary_lifnr": s["lifnr"],
        "opening_stock": _qty(s, runs[s["matnr"]]["opening"]),
    } for s in master.values()]

    vendor_rows = [{k: v[k] for k in ("lifnr", "name", "city", "route", "quoted_days")} for v in VENDORS]

    _write("ec_mm_materials.csv", mat_rows, list(mat_rows[0]),
           "Material master for plant EC, one central store: MARA, MARC, MBEW. dismm VB is manual "
           "reorder point, PD is planned against the fertiliser programme. minbe, eisbe and plifz are "
           "SAP's settings as a store sets them - two weeks of average use as safety stock, the "
           "supplier's quote as lead time - and the history below was generated under them. "
           "opening_stock is the stock on 2023-06-01, before the first movement.")
    _write("ec_mm_vendors.csv", vendor_rows, list(vendor_rows[0]),
           "Input suppliers: LFA1, with the route and quoted lead time. Pupuk Kaltim, Petrokimia "
           "Gresik and Meroke Tetap Jaya are the names already in the stock feed; the rest are invented.")
    _write("ec_mm_purchase_orders.csv", po_rows, list(po_rows[0]),
           "Purchase order lines: EKKO, EKPO, EKET. bsart RUSH is a local buy made because the store "
           "could not meet an issue. delay_cause is the generator's ANSWER KEY - a real extract has no "
           "such column, and no model may read it.")
    _write("ec_mm_movements.csv", moves,
           ["mblnr", "zeile", "budat", "bwart", "matnr", "werks", "lgort", "menge", "meins", "shkzg",
            "kostl", "aufnr", "block_code", "rsnum", "ebeln", "ebelp"],
           "Material documents: MATDOC. 101 goods receipt, 201 issue to cost centre, 261 issue to a PM "
           "order, 551 scrapped at a count, 701/702 count differences. From 2025-01-01 every issue "
           "carries the work order, trip day or PM order it came from; earlier issues were generated "
           "from the same drivers and carry none.")
    _write("ec_mm_reservations.csv", reservations, list(reservations[0]),
           "Fertiliser reservations: RESB, one per block, material and round. kzear X is a final issue. "
           "The 2025-03 round's issues dated after 2025-05-23 in ec_fertiliser.csv are open here, "
           "which is what they were on that day; 2025-10 is the programme on the books.")
    _write("ec_mm_stock.csv", stock_rows, list(stock_rows[0]),
           "Unrestricted stock on 2025-05-23: MARD. Equal to the opening stock plus every movement.")

    # The Nutrition panel's stock, rewritten from the store.
    name_of = {v: k for k, v in FERT_MATNR.items()}
    old = {r["material"]: r for r in _read("ec_fertiliser_stock.csv")}
    fs = []
    for m in MATERIALS:
        if m["matkl"] != "FERT":
            continue
        o = old.get(name_of[m["matnr"]], {})
        fs.append({"material": name_of[m["matnr"]],
                   "annual_programme_kg": o.get("annual_programme_kg", ""),
                   "issued_kg": o.get("issued_kg", ""),
                   "stock_on_hand_kg": round(next(r["labst"] for r in stock_rows if r["matnr"] == m["matnr"])),
                   "lead_time_days": master[m["matnr"]]["plifz"],
                   "supplier": VENDOR[m["lifnr"]]["name"]})
    _write("ec_fertiliser_stock.csv", fs,
           ["material", "annual_programme_kg", "issued_kg", "stock_on_hand_kg", "lead_time_days", "supplier"],
           "Warehouse stock and supplier lead times. SAP MM holds these. Rewritten by build_materials.py: "
           "stock is ec_mm_stock.csv on 2025-05-23, lead time is the supplier's quote (MARC-PLIFZ).")

    checks = _checks(moves, po_rows, stock_rows, reservations, runs, use)
    checks["other_feeds_unchanged"] = before == _hashes()
    checks["drivers"] = drivers
    counts = {"mm_materials": len(mat_rows), "mm_vendors": len(vendor_rows), "mm_purchase_orders": len(po_rows),
              "mm_movements": len(moves), "mm_reservations": len(reservations)}
    for k, v in counts.items():
        log.info("[materials] %s %d", k, v)
    return {**counts, "checks": checks}


def _checks(moves, po_rows, stock_rows, reservations, runs, use) -> dict:
    """The reconciliation constraints, measured rather than asserted."""
    net = defaultdict(float)
    for mv in moves:
        net[mv["matnr"]] += mv["menge"] if mv["shkzg"] == "S" else -mv["menge"]
    opening = {r["matnr"]: r["opening"] for r in [{"matnr": k, "opening": v["opening"]} for k, v in runs.items()]}
    stock_ok = all(abs(opening[r["matnr"]] + net[r["matnr"]] - r["labst"]) < 0.05 for r in stock_rows)
    # Never below zero on any day.
    neg = 0
    by_mat = defaultdict(list)
    for mv in moves:
        by_mat[mv["matnr"]].append(mv)
    for matnr, rows in by_mat.items():
        level = opening[matnr]
        for mv in rows:
            level += mv["menge"] if mv["shkzg"] == "S" else -mv["menge"]
            if level < -0.05:
                neg += 1
    fert = _read("ec_fertiliser.csv")
    fert_ledger = defaultdict(float)
    for r in fert:
        if r["issue_date"] <= WINDOW_END.isoformat():
            fert_ledger[FERT_MATNR[r["material"]]] += float(r["issued_kg"])
    fert_mm = defaultdict(float)
    for mv in moves:
        if mv["bwart"] == "201" and mv["rsnum"] and mv["budat"] >= "2024-10-01":
            fert_mm[mv["matnr"]] += mv["menge"]
    late = [r for r in fert if r["issue_date"] > WINDOW_END.isoformat()]
    pm = _read("ec_pm_orders.csv")
    spray = [r for r in _read("ec_upkeep_orders.csv")
             if r["activity"] == "spraying" and float(r["actual_qty"] or 0) > 0]
    trips = sum(float(r["diesel_l"]) for r in _read("ec_weighbridge.csv"))
    diesel_2025_haul = sum(mv["menge"] for mv in moves if mv["matnr"] == "FU-001" and mv["bwart"] == "201"
                           and mv["budat"] >= WINDOW_START.isoformat() and mv["kostl"] != "EC-GEN")
    rush = defaultdict(int)
    normal = defaultdict(int)
    for p in po_rows:
        g = MATERIAL[p["matnr"]]["matkl"]
        (rush if p["bsart"] == "RUSH" else normal)[g] += 1
    open_res = [r for r in reservations if not r["kzear"]]
    return {
        "stock_reconciles": stock_ok,
        "negative_stock_days": neg,
        "fertiliser_reconciles": all(abs(fert_ledger[k] - fert_mm[k]) < 1.0 for k in FERT_MATNR.values()),
        "fertiliser_issued_kg_in_window": round(sum(fert_ledger.values())),
        "fertiliser_late_rows_now_reserved": len(late),
        "fertiliser_late_kg_now_reserved": round(sum(float(r["issued_kg"]) for r in late)),
        "open_reservations": len(open_res),
        "pm_orders_reconcile": sum(1 for mv in moves if mv["bwart"] == "261") == len(pm),
        "spray_orders_reconcile": sum(1 for mv in moves if mv["matnr"] == "AC-001" and mv["aufnr"]) == len(spray),
        "diesel_trips_reconcile": abs(diesel_2025_haul - trips) < 5.0,
        "normal_pos_by_group": dict(normal),
        "rush_pos_by_group": dict(rush),
        "open_pos": sum(1 for p in po_rows if p["status"] != "closed"),
        "movements_by_type": {t: sum(1 for mv in moves if mv["bwart"] == t)
                              for t in ("101", "201", "261", "551", "701", "702")},
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = build()
    mpath = OUT / "manifest.json"
    if mpath.exists():
        man = json.loads(mpath.read_text(encoding="utf-8"))
        man["files"].update({k: v for k, v in out.items() if k != "checks"})
        man["files"]["materials_checks"] = out["checks"]
        mpath.write_text(json.dumps(man, indent=2), encoding="utf-8")
    for k, v in out.items():
        if k != "checks":
            print(f"  {k:20s} {v}")
    print("  checks:")
    for k, v in out["checks"].items():
        print(f"    {k:34s} {v}")
    print(f"  -> {OUT}")
