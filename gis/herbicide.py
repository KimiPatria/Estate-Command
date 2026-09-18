"""Weeding and herbicide programme adherence: is the spraying round being
kept, and at what rate?

The join nothing else makes. gis/ops.py holds the spraying work orders (EPMS
t_workplan, generated) and gis/stores.py holds the SAP MM goods issues of
herbicide; each answers its own question and neither reads the other. Put
together on the work order number, they say three things a manager wants
before anything else:

    the round     how long a block really waits between sprays against the
                  100-day standard, and how many blocks are past it today
    the misses    hectares planned and not sprayed, split into the days real
                  rainfall washed out and everything else
    the rate      litres of glyphosate and grams of metsulfuron drawn from the
                  store per hectare actually sprayed, against the dose in the
                  assumption register

Every order figure comes through gis/ops.py (adherence, _round_stretch, the
enriched order rows); nothing here re-derives adherence. Provenance, on every
payload: the rainfall that separates a rained-off day from any other miss is
REAL (Open-Meteo); block identity and area are REAL; the orders, the issues
and the material prices are SYNTHETIC. The generator issued herbicide at dose
x 1.08 (an 8% handling loss); the join has to find that factor back, and the
payload says whether it did.

Computed once per estate and cached; the register doses are applied per call
so an edit in the assumption panel re-rates the next answer.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from threading import Lock

from gis import assumptions, layers, ops
from gis.build_materials import HANDLING_LOSS as PLANTED_HANDLING
from gis.build_operations import SPRAY_RAIN_MM, WINDOW_END, WINDOW_START

log = logging.getLogger("estate-command.herbicide")

_CACHE: dict = {}
_LOCK = Lock()

OPERATION = "spray"
ACTIVITY = "spraying"
ISSUE_TO_COST_CENTRE = "201"
ANCHOR = WINDOW_END                     # 2025-05-23: the rotation state's as-at
WINDOW_DAYS = (WINDOW_END - WINDOW_START).days + 1

# The two herbicides in the material master, the register key each is dosed
# by, and the unit the rate is quoted in. Grams for metsulfuron because 40 g
# a hectare reads and 0.04 kg does not.
HERBICIDES = {
    "glyphosate": {"match": "glyphosate", "dose_key": "glyphosate_l_per_ha",
                   "issue_unit": "L", "rate_unit": "L/ha", "scale": 1.0, "dp": 1},
    "metsulfuron": {"match": "metsulfuron", "dose_key": "metsulfuron_kg_per_ha",
                    "issue_unit": "kg", "rate_unit": "g/ha", "scale": 1000.0, "dp": 2},
}
# How close the recovered factor has to sit to the planted one to count as
# found. Each issue carries 5% lognormal noise; 600 of them average well inside this.
RECOVERY_TOLERANCE = 0.03


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    return vals[len(vals) // 2] if vals else None


def _pct(a, b, dp=1):
    return round(100.0 * a / b, dp) if b else None


def _month_days() -> list[tuple[str, int]]:
    """Each month inside the ledger window and how many of its days the window holds."""
    out, d = [], WINDOW_START
    cur, n = d.strftime("%Y-%m"), 0
    while d <= WINDOW_END:
        m = d.strftime("%Y-%m")
        if m != cur:
            out.append((cur, n))
            cur, n = m, 0
        n += 1
        d += timedelta(days=1)
    out.append((cur, n))
    return out


def _materials() -> dict:
    """The herbicide rows of the material master, found by name, not by code."""
    out = {}
    for r in layers._read("ec_mm_materials.csv"):
        if (r.get("matkl") or "").upper() != "AGCH":
            continue
        name = (r.get("maktx") or "").lower()
        for key, h in HERBICIDES.items():
            if h["match"] in name and key not in out:
                out[key] = {"matnr": r["matnr"], "maktx": r["maktx"], "meins": r["meins"],
                            "price_idr": _f(r.get("verpr"))}
    return out


def _bucket() -> dict:
    return {"orders": 0, "planned_ha": 0.0, "sprayed_ha": 0.0, "rained_off_ha": 0.0,
            "short_ha": 0.0, "rained_off_orders": 0, "completed": 0, "partial": 0,
            "not_started": 0,
            "issues": {n: {"qty": 0.0, "ha": 0.0, "rows": 0, "unsprayed_qty": 0.0,
                           "unsprayed_rows": 0} for n in HERBICIDES}}


def _fold(b: dict, r: dict, got: dict) -> None:
    planned, actual = r["planned_qty"], r["actual_qty"]
    b["orders"] += 1
    b["planned_ha"] += planned
    b["sprayed_ha"] += actual
    if r["status"] == "weathered_off":
        b["rained_off_ha"] += planned
        b["rained_off_orders"] += 1
    else:
        b["short_ha"] += max(0.0, planned - actual)
    if r["status"] in ("completed", "partial", "not_started"):
        b[r["status"]] += 1
    for name, qty in got.items():
        i = b["issues"][name]
        i["rows"] += 1
        if actual > 0:
            i["qty"] += qty
            i["ha"] += actual
        else:
            # Chemical drawn against an order that recorded no hectares: the
            # one line in this join a store auditor reads first.
            i["unsprayed_qty"] += qty
            i["unsprayed_rows"] += 1


def _base(estate: str) -> dict:
    """The join, computed once: orders x issues, folded to block, month and estate."""
    key = estate.upper()
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]

        st = ops._state()
        rows = [r for r in (st["orders"].get(OPERATION) or []) if r["activity"] == ACTIVITY]
        if not rows:
            return {}
        blocks = st["blocks"]
        rain = st["rain"]
        upkeep = layers._state()["upkeep"]
        mats = _materials()
        by_matnr = {m["matnr"]: name for name, m in mats.items()}
        order_by_id = {r["order_id"]: r for r in rows}

        # Every herbicide issue, joined to the order in its AUFNR or counted
        # as unjoinable: before the window (the generator wrote no order
        # reference) or inside it with no order behind it.
        joined: dict = defaultdict(lambda: defaultdict(float))
        unjoined = {side: {"rows": 0, "qty": {n: 0.0 for n in HERBICIDES}}
                    for side in ("before_window", "in_window")}
        for mv in layers._read("ec_mm_movements.csv"):
            if mv["bwart"] != ISSUE_TO_COST_CENTRE:
                continue
            name = by_matnr.get(mv["matnr"])
            if not name:
                continue
            qty = _f(mv["menge"], 0.0)
            o = order_by_id.get(mv["aufnr"])
            if o is None:
                side = "before_window" if mv["budat"] < WINDOW_START.isoformat() else "in_window"
                unjoined[side]["rows"] += 1
                unjoined[side]["qty"][name] += qty
                continue
            joined[o["order_id"]][name] += qty

        per_block: dict = defaultdict(_bucket)
        per_month: dict = defaultdict(_bucket)
        estate_b = _bucket()
        completions: dict = defaultdict(list)
        last_order: dict = {}
        for r in rows:
            got = joined.get(r["order_id"], {})
            for b in (per_block[r["block_key"]], per_month[r["month"]], estate_b):
                _fold(b, r, got)
            if r["status"] == "completed":
                completions[r["block_key"]].append(r["date"])
            last_order[r["block_key"]] = r          # rows are date-sorted

        # The round per block: the state file's as-at reading, the closed
        # interval where the ledger holds two completions, and the attempts
        # rain took.
        state = {}
        for k, blk in blocks.items():
            up = (upkeep.get(k) or {}).get(ACTIVITY) or {}
            done = sorted(set(completions.get(k, [])))
            closed = ((date.fromisoformat(done[-1]) - date.fromisoformat(done[-2])).days
                      if len(done) >= 2 else None)
            lo = last_order.get(k)
            state[k] = {
                "interval_days": up.get("interval"),
                "last_sprayed": up.get("last_done"),
                "days_since": up.get("days_since"),
                "days_overdue": up.get("days_overdue"),
                "closed_interval_days": closed,
                "completions": len(done),
                "last_status": lo["status"] if lo else None,
                "last_order_date": lo["date"] if lo else None,
            }

        # Hectares due each month: every block's area spread over its own
        # round, so a 100-day round owes 31% of the estate in a 31-day month.
        months = []
        for m, ndays in _month_days():
            due = sum((blk["ha"] or 0) * ndays / (state[k]["interval_days"] or 100)
                      for k, blk in blocks.items())
            days = [date.fromisoformat(f"{m}-01") + timedelta(days=i) for i in range(31)]
            days = [d for d in days if d.strftime("%Y-%m") == m and WINDOW_START <= d <= WINDOW_END]
            wet = [d for d in days if (rain.get(d.isoformat()) or 0.0) >= SPRAY_RAIN_MM]
            months.append({"month": m, "days": ndays, "due_ha": due,
                           "rain_days": len(wet),
                           "rain_mm": sum(rain.get(d.isoformat()) or 0.0 for d in days),
                           "workable_days": sum(1 for d in days
                                                if d.weekday() != 6 and d not in wet)})

        base = {
            "orders": len(rows), "blocks": blocks, "state": state,
            "per_block": dict(per_block), "per_month": dict(per_month), "estate": estate_b,
            "months": months, "unjoined": unjoined, "materials": mats,
            "planted_ha": sum(blk["ha"] or 0 for blk in blocks.values()),
            "round": ops._round_stretch(OPERATION, rows, st),
            "rain_days_window": sum(m["rain_days"] for m in months),
        }
        _CACHE[key] = base
        log.info("[herbicide] joined %d issues to %d spraying orders on %d blocks",
                 sum(len(v) for v in joined.values()), len(rows), len(per_block))
        return base


def reload_herbicide() -> None:
    with _LOCK:
        _CACHE.clear()


# ── the rate ───────────────────────────────────────────────────────────────

def _rate(issue: dict, name: str, dose: float) -> dict:
    h = HERBICIDES[name]
    per_ha = issue["qty"] / issue["ha"] if issue["ha"] else None
    return {
        "issued": round(issue["qty"], h["dp"]),
        "issued_unit": h["issue_unit"],
        "sprayed_ha": round(issue["ha"], 1),
        "issues": issue["rows"],
        "per_ha": round(per_ha * h["scale"], 2) if per_ha is not None else None,
        "dose_per_ha": round(dose * h["scale"], 2),
        "unit": h["rate_unit"],
        "factor": round(per_ha / dose, 3) if (per_ha is not None and dose) else None,
    }


def _verdict(past_pct, implied_days, target) -> str:
    if past_pct is None or implied_days is None:
        return "cannot be read"
    if past_pct > 33 or implied_days > target * 1.15:
        return "not being kept"
    if implied_days > target * 1.05 or past_pct > 20:
        return "slipping"
    return "being kept"


def position(estate: str = "EC", top: int = 12) -> dict:
    """Spraying round adherence and the herbicide issued against it."""
    b = _base(estate)
    if not b:
        return {"available": False, "estate": estate.upper(),
                "reason": ("No spraying ledger. Run python gis/build_operations.py, "
                           "then gis/build_materials.py.")}
    top = max(1, min(int(top or 12), 50))
    doses = assumptions.values([h["dose_key"] for h in HERBICIDES.values()])
    dose_of = {n: doses[h["dose_key"]] for n, h in HERBICIDES.items()}
    blocks, state, est = b["blocks"], b["state"], b["estate"]
    adh = ops.adherence(OPERATION, top)
    tot = adh["totals"]
    target = _median(s["interval_days"] for s in state.values()) or 100

    # ── the round ──────────────────────────────────────────────────────
    past = [k for k, s in state.items() if (s["days_overdue"] or 0) > 0]
    past_ha = sum(blocks[k]["ha"] or 0 for k in past)
    pace_ha_day = est["sprayed_ha"] / WINDOW_DAYS if WINDOW_DAYS else 0
    implied = round(b["planted_ha"] / pace_ha_day) if pace_ha_day else None
    closed = [s["closed_interval_days"] for s in state.values() if s["closed_interval_days"]]
    past_pct = _pct(len(past), len(state))
    round_ = {
        "target_days": target,
        "anchor": ANCHOR.isoformat(),
        "past_round": {"blocks": len(past), "of": len(state), "pct": past_pct,
                       "ha": round(past_ha, 1),
                       "worst_days_overdue": max((state[k]["days_overdue"] or 0 for k in past),
                                                 default=0),
                       # Why a block is late is two different conversations:
                       # never put on a plan, or planned and rained off.
                       "never_ordered": sum(1 for k in past
                                            if (b["per_block"].get(k) or _bucket())["orders"] == 0),
                       "last_attempt_rained_off": sum(1 for k in past
                                                      if state[k]["last_status"] == "weathered_off")},
        "median_days_since_sprayed": _median(s["days_since"] for s in state.values()),
        "closed_intervals": {"blocks": len(closed), "median_days": _median(closed),
                             "reading": (b["round"] or {}).get("reading")},
        "implied_days_at_pace": implied,
        "pace_ha_per_day": round(pace_ha_day, 1),
        "verdict": _verdict(past_pct, implied, target),
    }

    # ── the misses ─────────────────────────────────────────────────────
    planned = est["planned_ha"]
    dry_planned = planned - est["rained_off_ha"]
    misses = {
        "planned_ha": round(planned, 1),
        "sprayed_ha": round(est["sprayed_ha"], 1),
        "adherence_pct": tot["adherence_pct"],
        "rained_off_ha": round(est["rained_off_ha"], 1),
        "rained_off_pct_of_plan": _pct(est["rained_off_ha"], planned),
        "other_short_ha": round(est["short_ha"], 1),
        "other_short_pct_of_plan": _pct(est["short_ha"], planned),
        "dry_day_adherence_pct": _pct(est["sprayed_ha"], dry_planned),
        "rained_off_orders": est["rained_off_orders"],
        "rain_days_over_threshold": b["rain_days_window"],
        "rain_threshold_mm": SPRAY_RAIN_MM,
        "orders": est["orders"], "completed": est["completed"], "partial": est["partial"],
        "carried_forward": tot["carried_forward"],
    }

    # ── the rate ───────────────────────────────────────────────────────
    rate = {n: _rate(est["issues"][n], n, dose_of[n]) for n in HERBICIDES}
    gly, met = rate["glyphosate"], rate["metsulfuron"]
    recovered = gly["factor"]
    mats = b["materials"]
    excess_idr = 0.0
    excess = {}
    for n, r in rate.items():
        over = est["issues"][n]["qty"] - est["issues"][n]["ha"] * dose_of[n]
        price = (mats.get(n) or {}).get("price_idr") or 0
        excess[n] = {"over_dose_qty": round(over, HERBICIDES[n]["dp"]),
                     "unit": HERBICIDES[n]["issue_unit"],
                     "price_idr_per_unit": price, "idr": round(over * price)}
        excess_idr += over * price
    unsprayed = {n: {"qty": round(est["issues"][n]["unsprayed_qty"], HERBICIDES[n]["dp"]),
                     "rows": est["issues"][n]["unsprayed_rows"]} for n in HERBICIDES}
    rate_block = {
        "materials": {n: {**mats.get(n, {}), **rate[n],
                          "dose_key": HERBICIDES[n]["dose_key"]} for n in HERBICIDES},
        "handling": {
            "recovered": recovered,
            "recovered_metsulfuron": met["factor"],
            "planted": PLANTED_HANDLING,
            "gap_pct": (round(100 * (recovered - PLANTED_HANDLING) / PLANTED_HANDLING, 1)
                        if recovered else None),
            "found": (recovered is not None
                      and abs(recovered - PLANTED_HANDLING) <= RECOVERY_TOLERANCE),
            "reading": (f"issued {recovered:.3f}x the register dose per hectare sprayed "
                        f"({met['factor']:.3f}x for metsulfuron) against a planted "
                        f"{PLANTED_HANDLING:.2f}x handling loss."
                        if recovered and met["factor"] else "no rated issues"),
        },
        "excess": {**excess, "idr_total": round(excess_idr),
                   "note": ("Litres and grams above dose x hectares sprayed, priced at the "
                            "material master's moving average. Handling loss, or leakage; "
                            "the per-block spread says which.")},
        "issued_with_no_hectares": unsprayed,
        "unjoined_issues": {
            side: {"rows": v["rows"],
                   **{f"{n}_{HERBICIDES[n]['issue_unit']}": round(q, 1)
                      for n, q in v["qty"].items()}}
            for side, v in b["unjoined"].items()},
    }

    # ── by month ───────────────────────────────────────────────────────
    by_month = []
    for m in b["months"]:
        pm = b["per_month"].get(m["month"]) or _bucket()
        g = _rate(pm["issues"]["glyphosate"], "glyphosate", dose_of["glyphosate"])
        mt = _rate(pm["issues"]["metsulfuron"], "metsulfuron", dose_of["metsulfuron"])
        by_month.append({
            "month": m["month"], "days": m["days"],
            "due_ha": round(m["due_ha"], 1),
            "planned_ha": round(pm["planned_ha"], 1),
            "sprayed_ha": round(pm["sprayed_ha"], 1),
            "rained_off_ha": round(pm["rained_off_ha"], 1),
            "other_short_ha": round(pm["short_ha"], 1),
            "cover_pct_of_due": _pct(pm["sprayed_ha"], m["due_ha"]),
            "adherence_pct": _pct(pm["sprayed_ha"], pm["planned_ha"]),
            "orders": pm["orders"], "rained_off_orders": pm["rained_off_orders"],
            "rain_days": m["rain_days"], "rain_mm": round(m["rain_mm"]),
            "workable_days": m["workable_days"],
            "glyphosate_l_per_ha": g["per_ha"], "glyphosate_factor": g["factor"],
            "metsulfuron_g_per_ha": mt["per_ha"], "metsulfuron_factor": mt["factor"],
        })

    # ── by division ────────────────────────────────────────────────────
    div: dict = defaultdict(lambda: {"blocks": 0, "planted_ha": 0.0, "past_round": 0,
                                     "planned_ha": 0.0, "sprayed_ha": 0.0,
                                     "rained_off_ha": 0.0, "gly_l": 0.0, "gly_ha": 0.0})
    for k, blk in blocks.items():
        d = div[blk["division"]]
        pb = b["per_block"].get(k) or _bucket()
        d["blocks"] += 1
        d["planted_ha"] += blk["ha"] or 0
        d["past_round"] += 1 if (state[k]["days_overdue"] or 0) > 0 else 0
        d["planned_ha"] += pb["planned_ha"]
        d["sprayed_ha"] += pb["sprayed_ha"]
        d["rained_off_ha"] += pb["rained_off_ha"]
        d["gly_l"] += pb["issues"]["glyphosate"]["qty"]
        d["gly_ha"] += pb["issues"]["glyphosate"]["ha"]
    by_division = []
    for code in sorted(div, key=lambda c: int(c)):
        d = div[code]
        lha = d["gly_l"] / d["gly_ha"] if d["gly_ha"] else None
        by_division.append({
            "division_code": code, "blocks": d["blocks"], "planted_ha": round(d["planted_ha"], 1),
            "past_round": d["past_round"], "past_round_pct": _pct(d["past_round"], d["blocks"]),
            "sprayed_ha": round(d["sprayed_ha"], 1),
            "adherence_pct": _pct(d["sprayed_ha"], d["planned_ha"]),
            "rained_off_pct_of_plan": _pct(d["rained_off_ha"], d["planned_ha"]),
            "glyphosate_l_per_ha": round(lha, 2) if lha else None,
            "dose_factor": round(lha / dose_of["glyphosate"], 3) if lha else None,
        })

    # ── the block lists ────────────────────────────────────────────────
    def row(k):
        blk = blocks[k]
        return {"block_label": blk["label"], "block_id": blk["id"],
                "division_code": blk["division"], "planted_ha": round(blk["ha"] or 0, 1)}

    overdue = sorted(past, key=lambda k: -(state[k]["days_overdue"] or 0))
    blocks_past_round = []
    for k in overdue[:top]:
        s, pb = state[k], b["per_block"].get(k) or _bucket()
        blocks_past_round.append({
            **row(k), "interval_days": s["interval_days"], "last_sprayed": s["last_sprayed"],
            "days_since": s["days_since"], "days_overdue": s["days_overdue"],
            "orders": pb["orders"], "rained_off_attempts": pb["rained_off_orders"],
            "sprayed_ha": round(pb["sprayed_ha"], 1), "last_status": s["last_status"],
            "last_order_date": s["last_order_date"],
        })

    rated = []
    for k, pb in b["per_block"].items():
        i = pb["issues"]["glyphosate"]
        if i["ha"] > 0 and i["rows"] > 0 and k in blocks:
            lha = i["qty"] / i["ha"]
            rated.append({**row(k), "sprayed_ha": round(i["ha"], 1),
                          "glyphosate_l": round(i["qty"], 1), "l_per_ha": round(lha, 2),
                          "dose_factor": round(lha / dose_of["glyphosate"], 3),
                          "issues": i["rows"], "days_overdue": state[k]["days_overdue"]})
    rated.sort(key=lambda r: -r["dose_factor"])
    factors = [r["dose_factor"] for r in rated]
    spread = {"blocks_rated": len(rated), "median": _median(factors),
              "p10": (sorted(factors)[int(len(factors) * 0.1)] if factors else None),
              "p90": (sorted(factors)[int(len(factors) * 0.9)] if factors else None),
              "over_1_20": sum(1 for f in factors if f > 1.20),
              "under_1_00": sum(1 for f in factors if f < 1.00)}

    # ── the sentence ───────────────────────────────────────────────────
    n_months = len(by_month)
    late_days = (implied - target) if implied else None
    summary = (
        f"The spray round is {round_['verdict']}: {len(past)} of {len(state)} blocks "
        f"({past_pct}%) are past their {target}-day round today, and at the pace of the "
        f"last {n_months} months the estate is covered once every {implied} days"
        + (f", {late_days} days late" if late_days and late_days > 0 else "") + ". "
        f"{tot['adherence_pct']}% of the planned hectares were sprayed; rain on the day "
        f"took {misses['rained_off_pct_of_plan']}% of the plan and dry-day shortfalls "
        f"another {misses['other_short_pct_of_plan']}%. "
        + (f"The store issued {gly['per_ha']} L of glyphosate per hectare sprayed against "
           f"a {gly['dose_per_ha']} L dose, {recovered:.2f}x."
           if gly["per_ha"] is not None and recovered else
           "No herbicide issue could be joined to a spraying order.")
    )

    before = b["unjoined"]["before_window"]
    return {
        "available": True,
        "estate": estate.upper(),
        "summary": summary,
        "anchor": ANCHOR.isoformat(),
        "window": {"from": WINDOW_START.isoformat(), "to": WINDOW_END.isoformat(),
                   "days": WINDOW_DAYS, "months": n_months},
        "totals": {
            "blocks": len(state), "planted_ha": round(b["planted_ha"], 1),
            "orders": est["orders"], "issues_joined": sum(i["rows"] for i in est["issues"].values()),
            "due_ha": round(sum(m["due_ha"] for m in b["months"]), 1),
            "planned_ha": misses["planned_ha"], "sprayed_ha": misses["sprayed_ha"],
            "cover_pct_of_due": _pct(est["sprayed_ha"], sum(m["due_ha"] for m in b["months"])),
            "glyphosate_l": gly["issued"], "metsulfuron_kg": met["issued"],
            "excess_idr": round(excess_idr),
        },
        "round": round_,
        "misses": misses,
        "rate": rate_block,
        "dose_spread": spread,
        "by_month": by_month,
        "by_division": by_division,
        "blocks_past_round": blocks_past_round,
        "blocks_over_dose": rated[:top],
        "blocks_under_dose": list(reversed(rated[-top:])) if rated else [],
        "worst_adherence_blocks": [
            {"block_label": r["block_label"], "division_code": r["division_code"],
             "orders": r["orders"], "planned_qty": r["planned_qty"], "actual_qty": r["actual_qty"],
             "adherence_pct": r["adherence_pct"], "weathered_off": r["weathered_off"]}
            for r in adh["worst_blocks"]],
        "drivers_rain": adh["drivers"]["rain"],
        "assumptions_used": assumptions.used([h["dose_key"] for h in HERBICIDES.values()]),
        "learn": (
            "From the client's own EPMS work orders and SAP MM issues, joined on the "
            "order number, this panel would say for every block whether its spray "
            "round is being kept, whether the misses are weather or capacity, and "
            "whether the litres drawn per hectare match the agreed dose. A store that "
            "issues a steady 8% over dose on every round is handling loss; a block or a "
            "team that runs 20% over for three rounds is the one to walk."),
        "provenance": (
            "synthetic ledger and synthetic MM issues, joined on the work order in the "
            "issue's AUFNR field. The daily rainfall that separates a rained-off day from "
            "any other miss is REAL (Open-Meteo); block identity and area are REAL. The "
            "doses come from the assumption register; the excess is priced at the "
            "generated material master's moving average."),
        "note": (
            f"A spray day is rained off at {SPRAY_RAIN_MM:.0f} mm or more on the day, the "
            f"threshold the ledger was generated at; {misses['rained_off_orders']} orders sit "
            f"on such days and every one of them recorded nothing. Hectares due each month "
            f"spread every block's area over its own {target}-day round. Adherence and the "
            f"closed-interval median are the operations layer's own figures, unchanged. "
            f"The generator issued herbicide at {PLANTED_HANDLING:.2f}x dose; the join "
            f"recovers {recovered}x"
            + (", found." if rate_block["handling"]["found"] else ", not found.")),
        "caveat": (
            f"Only {len(closed)} blocks completed two rounds inside the {WINDOW_DAYS}-day "
            f"window, so the closed-interval median rests on few blocks; the share past "
            f"round and the pace figure are the estate-wide readings. Per-block dose "
            f"factors rest on one or two issues each, so the over and under lists show a "
            f"5% issue-to-issue spread, not a leak. {before['rows']} issues before "
            f"{WINDOW_START.isoformat()} ({before['qty']['glyphosate']:,.0f} L glyphosate) "
            f"carry no order number and are not rated; the client's MM would carry one on "
            f"every issue if the store posts against the work order, which is the first "
            f"thing to check in their extract."),
    }
