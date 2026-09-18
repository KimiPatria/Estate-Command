"""Stores: what to order, when, and why, packaged for the people who run the estate.

The models live in gis/models (mm, leadtime, consumption, safety_stock). This
module never fits anything. It turns their output into what a storekeeper or
estate manager can act on (buildplan_stores.md):

    a sentence first     "Order by 31 July: 250 t"
    a range, not a point "1 in 10 orders from Pupuk Kaltim takes 66 days or more"
    why the buffer       "743 t because deliveries vary, none because use varies"
    how far to trust it  one word from the backtest or the replay, and the comparison behind it
    what makes it real   the five SAP reports that replace the generated history

Every figure is computed server-side and returned beside its sentence, so the
window, the decision log and the copilot quote the same numbers.
"""

import logging
import threading
from datetime import date

from gis.build_operations import TOMORROW, WINDOW_END
from gis.models import consumption, leadtime, learn, mm, safety_stock

log = logging.getLogger("estate-command.stores")

GROUP_ORDER = ("FERT", "AGCH", "FUEL", "SPARE")
STATUS_WORDS = {"order_now": "Order now", "this_week": "Order this week", "covered": "Covered",
                "overstocked": "Overstocked"}

EXPLAIN = {
    "leadtime": {
        "title": "Supplier lead times",
        "question": "How long does each supplier really take, against what SAP says?",
        "what": ("For every supplier: the time it quotes (what SAP plans on), the time its orders usually take, "
                 "the time 1 order in 10 exceeds, and the chance of an order arriving very late."),
        "how": [
            "Every purchase order is measured from the day it was placed to the delivery that completed it.",
            "A supplier's usual time starts at its route's usual time and moves toward its own record as orders build up.",
            ("Months are compared too, without being told which are wet: orders due December to March take longer "
             "by sea and much longer by road."),
            ("An order not yet delivered counts as 'at least this long', never as missing, so a supplier that "
             "misses sailings does not look fast because its latest orders are still at sea."),
        ],
        "read": [
            "'Usually 44 days, 1 in 10 takes 66 or more': plan on 44, and keep enough stock for the 66.",
            "A very-late chance of 18% means roughly one order in five or six misses its sailing.",
        ],
        "real": ("Learned from generated purchase orders. ME80FN with the EKBE receipt history for 24 months makes it "
                 "real, and the delivery note date beside the posting date would take out the store's posting delay."),
        "technical": ("Log lead time over quote; supplier effect as a Kaplan-Meier median shrunk toward the route "
                      "(k orders); calendar-month effect per route by due date, shrunk toward none and anchored on the "
                      "median month; very late = more than 18 days past the usual time, shrunk the same way; rolling "
                      "monthly backtest against the quote."),
    },
    "consumption": {
        "title": "Use over the lead time",
        "question": "How much of each material goes out before an order placed today arrives?",
        "what": ("The use expected over each material's lead time: fertiliser from the programme on the books, "
                 "herbicide and diesel from recent use and the season, parts and pest chemicals from how often "
                 "the job comes up."),
        "how": [
            ("Fertiliser: the programme's open reservations, times the share past rounds actually issued, spread "
             "over the weeks by how late past rounds went out."),
            "Herbicide and diesel: the last eight weeks' daily use, taken out of its season and put back into the coming one.",
            "Parts: how often each part has been needed per tonne hauled, pooled a little toward the other parts.",
        ],
        "read": [
            "'Off by 21% on average, against 28% for SAP's average': both miss; the forecast misses less.",
            "Fertiliser beats SAP's average by far because the average cannot see a round coming.",
        ],
        "real": ("Learned from generated goods issues. MB51 movements for 24 months, and RESB reservations, make it "
                 "real. Real stockouts hide demand, which the reservations recover."),
        "technical": ("Programme: open RESB x mature-round fulfilment x empirical issue-lag hazard; seasonal: 56-day "
                      "trailing mean deseasonalised by a shrunk calendar-month index; jobs: 182-day rate, parts per "
                      "tonne with an empirical Bayes pool; Croston-SBA reported as a challenger; weekly rolling "
                      "backtest against the trailing 91-day mean."),
    },
    "safety_stock": {
        "title": "Reorder points and safety stock",
        "question": "When should each material be reordered, how much is buffer, and what does it cost?",
        "what": ("The stock level that should trigger an order, how much of it is safety stock and why, the date "
                 "the order is due, and the cost of the service level against the alternatives."),
        "how": [
            "Take 2,000 possible lead times for an order placed that day, from the supplier's own record.",
            "For each, add up the use over that many days, with the forecast's own past misses on top.",
            ("The reorder point covers the service level's share of those: at 95%, 19 in 20. Safety stock is "
             "the part above the average."),
            ("Before any of it is used, twelve months are replayed against what the estate actually used: once on "
             "SAP's settings, once on these. A material switches only if its replay costs less."),
        ],
        "read": [
            "'Order by 31 July' is the last day stock on hand plus stock on order stays above the reorder point.",
            ("'743 t because deliveries vary' is the part of the buffer that exists because the supplier is "
             "unreliable. Fix the supplier and that part goes."),
        ],
        "real": ("Replayed on generated history. The five SAP reports below make every figure the estate's own."),
        "technical": ("Monte Carlo demand during lead time (2,000 draws); ROP at the service quantile; order-up-to "
                      "ROP plus cover weeks, rounded to BSTRF and capped at tank capacity; programme materials use "
                      "the spread of past rounds' totals; walk-forward replay with monthly refits, receipts taken "
                      "from the nearest recorded order on the same lane, rush buys at the recorded premium."),
    },
}

REAL_DATA = [
    {"report": "MB51", "gives": "Movements 101, 201, 261, 551, 701/702 for plant EC, 24 months",
     "replaces": "ec_mm_movements.csv"},
    {"report": "ME80FN, with EKBE history", "gives": "Purchase order lines, schedule lines and every goods receipt",
     "replaces": "ec_mm_purchase_orders.csv"},
    {"report": "MM60, plus MARC fields", "gives": "Reorder point, safety stock, planned delivery time, rounding",
     "replaces": "ec_mm_materials.csv"},
    {"report": "MD04, or RESB", "gives": "Reservations, open and closed", "replaces": "ec_mm_reservations.csv"},
    {"report": "MB52", "gives": "Stock on the extract date", "replaces": "ec_mm_stock.csv"},
]


def _rp(v) -> str:
    return safety_stock._rp(v)


def _d(on) -> date:
    if isinstance(on, date):
        return on
    return date.fromisoformat(on) if on else TOMORROW


def unavailable() -> dict | None:
    if not mm.available():
        return {"available": False, "reason": "No MM records. Run python gis/build_materials.py."}
    return None


# ── trust ──────────────────────────────────────────────────────────────────

def trust() -> list[dict]:
    lb, cb, rp = leadtime.backtest(), consumption.backtest(), safety_stock.replay()
    out = []
    if lb.get("available"):
        out.append({"model": "leadtime", "title": EXPLAIN["leadtime"]["title"], "grade": lb["grade"],
                    "trained_on": "synthetic", "headline": lb["plain"]["headline"], "detail": lb["plain"]["coverage"]})
    if cb.get("available"):
        out.append({"model": "consumption", "title": EXPLAIN["consumption"]["title"], "grade": cb["grade"],
                    "trained_on": "synthetic", "headline": cb["plain"]["headline"], "detail": cb["plain"]["by_kind"]})
    if rp.get("available"):
        out.append({"model": "safety_stock", "title": EXPLAIN["safety_stock"]["title"], "grade": rp["grade"],
                    "trained_on": "synthetic", "headline": rp["plain"]["headline"], "detail": rp["plain"]["check"]})
    return out


# ── views ──────────────────────────────────────────────────────────────────

def overview(group: str | None = None, on=None) -> dict:
    """Every material: what to do, by when, and the store's position at a glance."""
    na = unavailable()
    if na:
        return na
    d = _d(on)
    ov = safety_stock.overview(group, d)
    st = mm.state()
    rep = safety_stock.replay()
    s = safety_stock.settings()
    rows = ov["materials"]
    for r in rows:
        r["status_label"] = STATUS_WORDS[r["status"]]
        r["group_label"] = mm.GROUPS[r["group"]]
    # Rush buys in the last 90 days, from the record.
    since = mm.date_of(mm.N_DAYS - 90)
    rush90 = [po for po in st["pos"] if po["bsart"] == "RUSH" and po["bedat"] >= since
              and (not group or st["materials"][po["matnr"]]["matkl"] == group)]
    rush90_cost = sum(po["menge"] * (po["netpr"] - st["materials"][po["matnr"]]["verpr"]) for po in rush90)
    # Reorder-point materials only: the average a reorder-point store carries is its
    # safety stock plus half an order. Fertiliser follows the programme, where the
    # buffer is time bought ahead of a round, not stock carried all year.
    rec_value = held_rop = 0.0
    for r in rows:
        m = st["materials"][r["matnr"]]
        if m["dismm"] != "VB":
            continue
        view = safety_stock.material(r["matnr"], d)
        lot = view["order_qty"] or m["bstfe"]
        rec_value += (view["safety_stock"] + lot / 2) * m["verpr"]
        held_rop += r["value_idr"]
    to_order = [r for r in rows if r["status"] in ("order_now", "this_week")]
    summary = []
    if to_order:
        summary.append(f"{len(to_order)} material{'s' if len(to_order) != 1 else ''} to order this week: "
                       + ", ".join(f"{r['maktx']} ({r['plain']['headline'].rstrip('.')})" for r in to_order[:4])
                       + ("…" if len(to_order) > 4 else "."))
    over = [r for r in rows if r["status"] == "overstocked"]
    if over:
        summary.append(f"Overstocked: {', '.join(r['maktx'] for r in over)}.")
    nxt = next((r for r in rows if r["status"] == "covered" and r["order_by"]), None)
    if nxt:
        summary.append(f"Next due: {nxt['maktx']}, by "
                       f"{date.fromisoformat(nxt['order_by']).strftime('%d %B').lstrip('0')}.")
    summary.append(f"{len(rush90)} rush buys in the last 90 days cost {_rp(rush90_cost)} over the normal price.")
    if rep.get("available"):
        summary.append(rep["plain"]["headline"])
    groups = []
    for g in GROUP_ORDER:
        gr = [r for r in rows if r["group"] == g]
        if not gr:
            continue
        groups.append({"group": g, "key": mm.GROUP_KEYS[g], "label": mm.GROUPS[g], "materials": len(gr),
                       "to_order": sum(1 for r in gr if r["status"] in ("order_now", "this_week")),
                       "value_idr": sum(r["value_idr"] for r in gr),
                       "service_level_pct": s["service"][g],
                       "replay": (rep.get("groups") or {}).get(g)})
    return {
        "available": True, "date": d.isoformat(), "date_label": d.strftime("%A %d %B %Y").replace(" 0", " "),
        "window": {"from": mm.MM_START.isoformat(), "to": WINDOW_END.isoformat(), "tomorrow": TOMORROW.isoformat()},
        "materials": rows, "groups": groups, "counts": ov["counts"], "summary": summary,
        "to_order": len(to_order),
        "stock_value_idr": ov["stock_value_idr"],
        "reorder_point_stock": {"held_idr": round(held_rop), "recommended_idr": round(rec_value)},
        "rush_90d": {"orders": len(rush90), "premium_idr": round(rush90_cost)},
        "switch": s["switch"], "trust": trust(),
        "note": ("Every record here is generated, shaped as the SAP MM extract that would replace it. The lead-time "
                 "and replay checks show the method works on data shaped like this estate's store, not yet what "
                 "its suppliers really do."),
    }


def material_view(matnr: str, on=None) -> dict:
    na = unavailable()
    if na:
        return na
    st = mm.state()
    if matnr not in st["materials"]:
        return {"available": False, "reason": f"No material {matnr!r}.",
                "materials": sorted(st["materials"])}
    d = _d(on)
    v = safety_stock.material(matnr, d)
    m = st["materials"][matnr]
    moves = [mv for mv in st["moves"] if mv["matnr"] == matnr][-25:][::-1]
    cb = (consumption.backtest().get("materials") or {}).get(matnr)
    lt_row = next((r for r in leadtime.table(d).get("suppliers") or [] if r["lifnr"] == m["primary_lifnr"]), None)
    uses = next((r for r in consumption.use_per_unit()["rows"] if r["matnr"] == matnr), None)
    out = {**v, "status_label": STATUS_WORDS[v["status"]],
           "movements": [{k: mv[k] for k in ("mblnr", "budat", "bwart", "menge", "shkzg", "kostl", "aufnr",
                                             "block_code", "ebeln")} for mv in moves],
           "consumption": cb, "supplier_row": lt_row, "use_per_unit": uses,
           "storage_loss": mm.storage_loss_rate(matnr),
           "rush_premium_pct": round(100 * mm.rush_premium(m["matkl"]), 1),
           "master": {k: m[k] for k in ("matnr", "maktx", "matkl", "meins", "dismm", "minbe", "eisbe", "plifz",
                                        "bstrf", "bstfe", "verpr", "max_stock", "primary_lifnr")},
           "explain": EXPLAIN["safety_stock"]}
    if matnr == "FU-001":
        out["diesel_check"] = consumption.diesel_check(d)
    return out


def lead_times(on=None) -> dict:
    na = unavailable()
    if na:
        return na
    t = leadtime.table(_d(on))
    return {**t, "recovery": leadtime.recovery(), "explain": EXPLAIN["leadtime"]}


def ledger(matnr: str | None = None, date_from: str | None = None, date_to: str | None = None,
           bwart: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    """The material documents, newest first, MB51-style."""
    na = unavailable()
    if na:
        return na
    st = mm.state()
    rows = st["moves"]
    if matnr:
        rows = [r for r in rows if r["matnr"] == matnr]
    if date_from:
        rows = [r for r in rows if r["budat"] >= date_from]
    if date_to:
        rows = [r for r in rows if r["budat"] <= date_to]
    if bwart:
        types = set(bwart.split(","))
        rows = [r for r in rows if r["bwart"] in types]
    total = len(rows)
    page = rows[::-1][offset:offset + limit]
    names = {k: v["maktx"] for k, v in st["materials"].items()}
    return {"available": True, "total": total, "offset": offset, "limit": limit,
            "rows": [{**r, "maktx": names[r["matnr"]]} for r in page],
            "movement_types": {"101": "Goods receipt against a PO", "201": "Issue to a cost centre",
                               "261": "Issue to a PM order", "551": "Scrapped at a count",
                               "701": "Count difference, found", "702": "Count difference, missing"}}


def purchase_orders(matnr: str | None = None, kind: str | None = None, limit: int = 100) -> dict:
    na = unavailable()
    if na:
        return na
    st = mm.state()
    rows = st["pos"]
    if matnr:
        rows = [p for p in rows if p["matnr"] == matnr]
    if kind == "rush":
        rows = [p for p in rows if p["bsart"] == "RUSH"]
    elif kind == "open":
        rows = [p for p in rows if p["status"] != "closed"]
    elif kind == "normal":
        rows = [p for p in rows if p["bsart"] == "NB"]
    rows = sorted(rows, key=lambda p: p["bedat"], reverse=True)
    out = []
    for p in rows[:limit]:
        m = st["materials"][p["matnr"]]
        v = st["vendors"][p["lifnr"]]
        out.append({"ebeln": p["ebeln"], "bedat": p["bedat"].isoformat(), "bsart": p["bsart"], "lifnr": p["lifnr"],
                    "supplier": v["name"], "matnr": p["matnr"], "maktx": m["maktx"], "menge": p["menge"],
                    "meins": m["meins"], "netpr": p["netpr"], "eindt": p["eindt"].isoformat(), "status": p["status"],
                    "received": p["received"], "done": p["done"].isoformat() if p["done"] else None,
                    "lead_days": p["lead"], "quoted_days": v["quoted_days"],
                    "late_days": (p["lead"] - v["quoted_days"]) if p["lead"] is not None else None,
                    "premium_pct": round(100 * (p["netpr"] / m["verpr"] - 1), 1)})
    return {"available": True, "total": len(rows), "rows": out}


def accuracy() -> dict:
    na = unavailable()
    if na:
        return na
    return {
        "trust": trust(),
        "leadtime": {"backtest": leadtime.backtest(), "recovery": leadtime.recovery()},
        "consumption": {"backtest": consumption.backtest(), "recovery": consumption.recovery(),
                        "use_per_unit": consumption.use_per_unit()},
        "replay": safety_stock.replay(),
        "ledger": mm.reconcile(),
        "explain": EXPLAIN, "real_data": REAL_DATA,
        "rules": [
            "The old way is SAP's own settings, and the store's history was generated under them.",
            "The reorder points are judged on a replay of recorded use, not on the model's own simulation.",
            "An order still at sea counts as at least that late, never as missing.",
            "A material keeps SAP's settings unless its own replay costs less at the service level.",
        ],
    }


# ── documents ──────────────────────────────────────────────────────────────

def requisition_payload(matnr: str, on=None) -> dict:
    """The lines of a purchase requisition, from the same figures the window shows."""
    v = safety_stock.material(matnr, _d(on))
    return {"matnr": matnr, "maktx": v["maktx"], "qty": v["order_qty"], "unit": v["unit"],
            "supplier": v["supplier"]["name"], "lifnr": v["supplier"]["lifnr"],
            "order_by": v["order_by"], "needed_within_days": round(v["supplier"]["median"]),
            "reorder_point": v["reorder_point"], "safety_stock": v["safety_stock"],
            "on_hand": v["on_hand"], "on_order": v["on_order"], "headline": v["plain"]["headline"]}


def settings_change_payload(matnr: str, on=None) -> dict:
    v = safety_stock.material(matnr, _d(on))
    m = mm.state()["materials"][matnr]
    lines = []
    if m["dismm"] == "VB":
        lines.append({"field": "MINBE (reorder point)", "current": m["minbe"], "proposed": round(v["reorder_point"])})
        lines.append({"field": "EISBE (safety stock)", "current": m["eisbe"], "proposed": round(v["safety_stock"])})
    lines.append({"field": "PLIFZ (planned delivery days)", "current": m["plifz"],
                  "proposed": round(v["supplier"]["usual"])})
    return {"matnr": matnr, "maktx": v["maktx"], "unit": v["unit"], "lines": lines,
            "evidence": [v["plain"]["supplier"], v["plain"]["buffer"]]}


# ── lifecycle ──────────────────────────────────────────────────────────────

def warm() -> None:
    """Load the store, score the models and run the replay once, in the background."""
    def run():
        try:
            if not mm.available():
                return
            leadtime.backtest()
            leadtime.recovery()
            consumption.backtest()
            consumption.recovery()
            safety_stock.replay()
            safety_stock.overview()
            log.info("[stores] models warm")
        except Exception as exc:
            log.warning("[stores] warm-up failed: %s", exc)
    threading.Thread(target=run, name="stores-warm", daemon=True).start()


def reload() -> None:
    for m in (mm, leadtime, consumption, safety_stock):
        m.reload()


def assumption_changed(key: str | None) -> None:
    """Drop what a register edit invalidates. Service levels and costs are keyed into the replay's cache."""
    if key is None or key == "leadtime_prior_orders":
        leadtime.reload()
        safety_stock.reload()
    elif key.startswith(("service_level_", "holding_cost", "order_cover", "use_stock_model")):
        safety_stock.reload()
