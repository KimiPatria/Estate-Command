"""The SAP MM records, read once, in the shapes the stores models need.

Six generated files stand in for a standard extract (buildplan_stores.md §2):
material master, suppliers, purchase orders, material documents,
reservations and closing stock. This module is the only reader. It turns them
into day-indexed arrays from 2023-06-01 to 2025-05-23, joins every goods
receipt to its purchase order, and strips the answer key before anything
leaves it: `delay_cause` says why an order ran late, which no real extract
carries and no model may use.

A lead time is PO date to the receipt that completes 95% of the quantity. A
supplier that splits a delivery has not delivered until the second truck.
"""

import csv
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

import numpy as np

from gis.build_operations import TOMORROW, WINDOW_END
from gis.models import learn

log = logging.getLogger("estate-command.models.mm")

_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"
_CACHE: dict = {}
_LOCK = Lock()

MM_START = date(2023, 6, 1)
N_DAYS = (WINDOW_END - MM_START).days + 1
GROUPS = {"FERT": "Fertiliser", "AGCH": "Agrochemicals", "FUEL": "Fuel", "SPARE": "Spare parts"}
GROUP_KEYS = {"FERT": "fertiliser", "AGCH": "agrochemical", "FUEL": "fuel", "SPARE": "parts"}
COMPLETE_SHARE = 0.95


def _read(name: str) -> list[dict]:
    path = _DIR / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(ln for ln in fh if not ln.startswith("#")))


def day(d: date) -> int:
    return (d - MM_START).days


def date_of(i: int) -> date:
    return MM_START + timedelta(days=int(i))


def available() -> bool:
    return (_DIR / "ec_mm_movements.csv").exists()


def state() -> dict:
    with _LOCK:
        if "state" in _CACHE:
            return _CACHE["state"]
    if not available():
        return {"available": False,
                "reason": "No MM records. Run python gis/build_materials.py."}

    vendors = {}
    for r in _read("ec_mm_vendors.csv"):
        vendors[r["lifnr"]] = {**r, "quoted_days": int(r["quoted_days"])}

    materials = {}
    for r in _read("ec_mm_materials.csv"):
        materials[r["matnr"]] = {
            **r, "minbe": float(r["minbe"]), "eisbe": float(r["eisbe"]), "plifz": int(r["plifz"]),
            "bstrf": float(r["bstrf"]), "bstfe": float(r["bstfe"]), "verpr": float(r["verpr"]),
            "max_stock": float(r["max_stock"]) if r["max_stock"] else None,
            "opening_stock": float(r["opening_stock"]), "group_label": GROUPS[r["matkl"]],
        }

    moves = _read("ec_mm_movements.csv")
    use = {m: np.zeros(N_DAYS) for m in materials}
    receipts = {m: np.zeros(N_DAYS) for m in materials}
    loss = {m: np.zeros(N_DAYS) for m in materials}
    diff = {m: np.zeros(N_DAYS) for m in materials}
    gr = defaultdict(list)
    first_issue = {}
    for mv in moves:
        mv["menge"] = float(mv["menge"])
        i = day(date.fromisoformat(mv["budat"]))
        m, q, t = mv["matnr"], mv["menge"], mv["bwart"]
        if t in ("201", "261"):
            use[m][i] += q
            if mv["rsnum"] and mv["rsnum"] not in first_issue:
                first_issue[mv["rsnum"]] = date.fromisoformat(mv["budat"])
        elif t == "101":
            receipts[m][i] += q
            gr[mv["ebeln"]].append((date.fromisoformat(mv["budat"]), q))
        elif t == "551":
            loss[m][i] += q
        elif t == "701":
            diff[m][i] += q
        elif t == "702":
            diff[m][i] -= q
    on_hand = {m: materials[m]["opening_stock"] + np.cumsum(receipts[m] - use[m] - loss[m] + diff[m])
               for m in materials}

    pos = []
    for r in _read("ec_mm_purchase_orders.csv"):
        r = learn.strip(r)
        po = {**r, "bedat": date.fromisoformat(r["bedat"]), "eindt": date.fromisoformat(r["eindt"]),
              "menge": float(r["menge"]), "netpr": float(r["netpr"]),
              "receipts": sorted(gr.get(r["ebeln"], []))}
        got, done = 0.0, None
        for when, q in po["receipts"]:
            got += q
            if done is None and got >= COMPLETE_SHARE * po["menge"] - 1e-9:
                done = when
        po["received"] = got
        po["done"] = done
        po["lead"] = (done - po["bedat"]).days if done else None
        pos.append(po)
    learn.assert_clean(pos[0].keys() if pos else [])

    reservations = []
    for r in _read("ec_mm_reservations.csv"):
        reservations.append({**r, "bdter": date.fromisoformat(r["bdter"]), "bdmng": float(r["bdmng"]),
                             "enmng": float(r["enmng"]), "kzear": r["kzear"] == "X",
                             "issued_on": first_issue.get(r["rsnum"])})

    stock = {r["matnr"]: float(r["labst"]) for r in _read("ec_mm_stock.csv")}

    # What running out cost, per group: the rush price over the moving average.
    prem = defaultdict(list)
    rush_min = {}
    for po in pos:
        if po["bsart"] == "RUSH":
            m = materials[po["matnr"]]
            prem[m["matkl"]].append(po["netpr"] / m["verpr"] - 1.0)
            rush_min[po["matnr"]] = min(rush_min.get(po["matnr"], po["menge"]), po["menge"])
    for m, v in materials.items():
        v["rush_lot"] = rush_min.get(m, v["bstrf"])

    out = {
        "available": True, "materials": materials, "vendors": vendors, "pos": pos, "moves": moves,
        "use": use, "receipts": receipts, "loss": loss, "diff": diff, "on_hand": on_hand,
        "reservations": reservations, "stock": stock,
        "rush_premium": {g: float(np.mean(v)) for g, v in prem.items()},
        "rush_orders": {g: len(v) for g, v in prem.items()},
        "dates": {"from": MM_START.isoformat(), "to": WINDOW_END.isoformat(), "tomorrow": TOMORROW.isoformat()},
    }
    with _LOCK:
        _CACHE["state"] = out
    return out


def rush_premium(matkl: str) -> float:
    st = state()
    return st["rush_premium"].get(matkl, float(np.mean(list(st["rush_premium"].values()) or [0.3])))


def storage_loss_rate(matnr: str, before: date | None = None) -> dict:
    """Monthly share of stock lost in storage, from what the counts scrapped (551)."""
    st = state()
    end = day(before) if before else N_DAYS
    loss = float(st["loss"][matnr][:end].sum())
    stock_days = float(np.clip(st["on_hand"][matnr][:end], 0, None).sum())
    rate = loss / (stock_days / 30.4375) if stock_days > 0 else 0.0
    return {"rate_per_month": rate, "scrapped": loss, "counts": int((st["loss"][matnr][:end] > 0).sum())}


def reconcile() -> dict:
    """The record checked against itself and against the operations ledger it was drawn from."""
    st = state()
    if not st.get("available"):
        return {"available": False}
    end = WINDOW_END.isoformat()
    stock_ok = all(abs(float(st["on_hand"][m][-1]) - st["stock"][m]) < 0.05 for m in st["materials"])
    fert_file = defaultdict(float)
    names = {v["maktx"]: k for k, v in st["materials"].items() if v["matkl"] == "FERT"}
    for r in _read("ec_fertiliser.csv"):
        if r["issue_date"] <= end:
            fert_file[names[r["material"]]] += float(r["issued_kg"])
    fert_mm = defaultdict(float)
    for mv in st["moves"]:
        if mv["bwart"] == "201" and mv["rsnum"] and mv["budat"] >= "2024-10-01":
            fert_mm[mv["matnr"]] += mv["menge"]
    pm = len(_read("ec_pm_orders.csv"))
    issued_261 = sum(1 for mv in st["moves"] if mv["bwart"] == "261")
    lowest = min(float(v.min()) for v in st["on_hand"].values())
    checks = {
        "stock_equals_movements": stock_ok,
        "fertiliser_issues_match_ledger": all(abs(fert_file[k] - fert_mm[k]) < 1.0 for k in names.values()),
        "one_part_per_breakdown_order": issued_261 == pm,
        "never_below_zero": lowest > -0.05,
    }
    return {"available": True, "checks": checks, "all_pass": all(checks.values()),
            "plain": (f"Stock on {end} equals the opening stock plus every movement; fertiliser issues match "
                      f"ec_fertiliser.csv to the kilogram; {issued_261} parts issued against {pm} breakdown orders; "
                      "no material ever goes below zero.")}


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
