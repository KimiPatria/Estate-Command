"""What a crew can actually do in a man-day, learned from what it did.

The plan used to size every pruning, weeding and spraying crew at the
textbook rate in the assumption register, and every harvest block at the
productivity model's target. Neither ever moved. A crew that does a fifth
more than the book was planned as if it did the book.

The method: start from the book, move with the evidence
-------------------------------------------------------
Empirical Bayes shrinkage, one formula a supervisor can check:

    learned speed = the book, moved toward what the crew did,
                    by  man-days of evidence / (man-days + k)

k is `rate_prior_man_days` in the register. With 20, a crew with 20 man-days
of records sits halfway between the book and its own record; with 200 it is
almost all record. Speeds are stated relative to the average crew doing the
same work ("7% faster"), because the register sets the level and learning
only says who is faster or slower than it.

Units: a crew per operation for pruning, weeding and spraying; a block for
harvesting, where gangs are sized to the ripe crop and the block, not the
gang, is what makes a day hard.

Two traps
---------
1. A crew that finished its job stopped because the work ran out, not the
   day, so its rate is a lower bound. Completed upkeep orders are left out,
   and the count left out is shown.
2. Rain belongs to the work-done model. Where rain slows the rate itself
   (upkeep: fewer hectares an hour in the wet), the observed rate is divided
   by the work-done model's condition factor for that day, so the speed
   learned is the ordinary-day speed and rain is not counted twice. Harvest
   bunches per man-day do not move with rain in this ledger, so they are not
   divided; the check is reported.

Guardrail: a learned speed moves at most `rate_max_weekly_change_pct` a week.

Pest and transport are not learned: pest man-days are set by method (palms
per man-day by treatment), and a vehicle's loads come from its turnaround.

The answer key: ec_crews.csv carries each crew's true speed in `skill`. The
learning never reads it; `recovery()` does, to show the learning finds it.
"""

import csv
import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

import numpy as np

from gis import assumptions, ops
from gis.build_operations import TOMORROW, WINDOW_END, WINDOW_START
from gis.models import learn

log = logging.getLogger("estate-command.models.rates")

_CACHE: dict = {}
_LOCK = Lock()

RATE_OPS = ("harvest", "prune", "weed", "spray")
K_GRID = (5, 10, 20, 40, 80)
RATE_KEY = {"pruning": "prune_palms_per_man_day", "circle_weeding": "circle_weed_ha_per_man_day",
            "path_upkeep": "path_upkeep_ha_per_man_day", "spraying": "spray_ha_per_man_day"}
UNIT_WORDS = {"harvest": "block", "prune": "crew", "weed": "crew", "spray": "team"}
WORK_WORDS = {"harvest": "harvesting", "prune": "pruning", "weed": "weeding", "spray": "spraying"}


# ── observations ───────────────────────────────────────────────────────────

def _observations() -> dict:
    """Every order that tells us something about speed, with its book rate
    and its condition factor, built once."""
    with _LOCK:
        if "obs" in _CACHE:
            return _CACHE["obs"]
    from gis.models import slippage
    st = ops._state()
    av = assumptions.values()
    harvest_rates = ops._harvest_rates()
    T = slippage._table()
    # Condition factor per order: the work-done model's expected share on the
    # day's recorded conditions, with turnout and the crew's own record taken
    # out (a man-day already counts who came, and the crew's record is what
    # is being learned).
    fit = slippage._fit(WINDOW_END + timedelta(days=1))
    X = T["X"].copy()
    X[:, slippage.CI["turnout"]] = 0.0
    X[:, slippage.CI["crew_history"]] = 0.0
    cond = slippage._expect(fit, X, T["group"])["share"] if fit else np.ones(len(T["op"]))
    # Map order ids to their table row for the condition factor.
    ids = []
    for op in slippage.OPS:
        for r in st["orders"].get(op) or []:
            if r["planned_qty"] > 0:
                ids.append((r["date"], r["order_id"]))
    ids.sort()
    row_of = {oid: i for i, (_, oid) in enumerate(ids)}

    out = {op: [] for op in RATE_OPS}
    excluded = defaultdict(int)
    for op in RATE_OPS:
        for r in st["orders"].get(op) or []:
            if r["actual_qty"] <= 0 or r["man_days_actual"] <= 0:
                continue
            if op != "harvest" and r["status"] == "completed":
                excluded[op] += 1
                continue
            b = st["blocks"].get(r["block_key"]) or {}
            if op == "harvest":
                prior = harvest_rates.get(f"{b.get('division')}-{b.get('block_code')}") \
                    or av["harvest_bunches_per_man_day"]
                unit = r["block_key"]
            else:
                prior = av[RATE_KEY[r["activity"]]]
                unit = r["crew_code"]
            i = row_of.get(r["order_id"])
            c = float(cond[i]) if i is not None else 1.0
            out[op].append({
                "date": r["date"], "ord": date.fromisoformat(r["date"]).toordinal(),
                "unit": unit, "md": r["man_days_actual"],
                "observed": r["actual_qty"] / r["man_days_actual"], "prior": prior,
                "cond": max(c, 0.05), "activity": r["activity"],
            })
    res = {"rows": out, "excluded": dict(excluded)}
    # Does rain slow the rate itself? Correlate log(observed / book) with the
    # log condition factor; only then divide it out.
    divide = {}
    for op, rows in out.items():
        if len(rows) < 50:
            divide[op] = False
            continue
        y = np.log([x["observed"] / x["prior"] for x in rows])
        c = np.log([x["cond"] for x in rows])
        corr = float(np.corrcoef(y, c)[0, 1]) if np.std(c) > 0 else 0.0
        divide[op] = corr > 0.1
        res.setdefault("rain_corr", {})[op] = round(corr, 3)
    res["divide"] = divide
    for op, rows in out.items():
        for x in rows:
            x["log_ratio"] = math.log(x["observed"] / (x["prior"] * (x["cond"] if divide[op] else 1.0)))
    with _LOCK:
        _CACHE["obs"] = res
    return res


# ── learning ───────────────────────────────────────────────────────────────

def _raw_factors(rows: list[dict], cutoff_ord: int, k: float) -> tuple[dict, dict, float]:
    """Unit speed relative to the average unit, from orders before `cutoff`."""
    sums: dict = defaultdict(lambda: [0.0, 0.0])
    tot = [0.0, 0.0]
    for x in rows:
        if x["ord"] >= cutoff_ord:
            continue
        s = sums[x["unit"]]
        s[0] += x["md"] * x["log_ratio"]
        s[1] += x["md"]
        tot[0] += x["md"] * x["log_ratio"]
        tot[1] += x["md"]
    level = tot[0] / tot[1] if tot[1] else 0.0
    factors, md = {}, {}
    for u, (sw, m) in sums.items():
        mean = sw / m
        factors[u] = math.exp(m / (m + k) * (mean - level))
        md[u] = m
    return factors, md, level


def _weekly(op: str, k: float, cap_pct: float) -> dict:
    """Speeds at each Monday, each week's move capped."""
    key = ("weekly", op, k, cap_pct)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    rows = _observations()["rows"][op]
    checkpoints = learn.weekly_origins(WINDOW_START + timedelta(days=7), TOMORROW + timedelta(days=6))
    prev: dict = {}
    series = []
    cap = cap_pct / 100.0
    for cp in checkpoints:
        raw, md, level = _raw_factors(rows, cp.toordinal(), k)
        cur = {}
        for u, f in raw.items():
            p = prev.get(u, 1.0)
            cur[u] = min(max(f, p * (1 - cap)), p * (1 + cap))
        for u, p in prev.items():
            cur.setdefault(u, p)
        series.append({"date": cp, "factors": cur, "md": md, "level": level})
        prev = cur
    with _LOCK:
        _CACHE[key] = series
    return series


def _settings() -> tuple[float, float]:
    return (float(assumptions.get("rate_prior_man_days")),
            float(assumptions.get("rate_max_weekly_change_pct")))


def speeds_at(op: str, d: date) -> dict:
    """Every unit's learned speed as known on `d`: {unit: {factor, man_days, history}}."""
    if op not in RATE_OPS:
        return {}
    k, cap = _settings()
    series = _weekly(op, k, cap)
    past = [s for s in series if s["date"] <= d]
    if not past:
        return {}
    cur = past[-1]
    out = {}
    for u, f in cur["factors"].items():
        hist = [round(s["factors"].get(u, 1.0), 3) for s in past[-4:]]
        out[u] = {"factor": round(f, 3), "man_days": round(cur["md"].get(u, 0.0), 1),
                  "history": hist, "as_of": cur["date"].isoformat()}
    return out


def crew_factor(op: str, crew_code: str, d: date) -> float:
    if int(assumptions.get("use_learned_rates")) != 1 or op == "harvest":
        return 1.0
    if not backtest_passes(op):
        return 1.0
    return float((speeds_at(op, d).get(crew_code) or {}).get("factor", 1.0))


def block_factor(block_key: str, d: date) -> float:
    if int(assumptions.get("use_learned_rates")) != 1 or not backtest_passes("harvest"):
        return 1.0
    return float((speeds_at("harvest", d).get(block_key) or {}).get("factor", 1.0))


# ── scoring ────────────────────────────────────────────────────────────────

def _score(op: str, k: float, cap: float) -> dict | None:
    rows = _observations()["rows"][op]
    if len(rows) < 50:
        return None
    series = {s["date"]: s for s in _weekly(op, k, cap)}
    origins = learn.weekly_origins(date(2025, 2, 3), WINDOW_END)
    errs_m, errs_b = [], []
    for o in origins:
        s = series.get(o)
        if s is None:
            continue
        end = (o + timedelta(days=7)).toordinal()
        for x in rows:
            if o.toordinal() <= x["ord"] < end:
                base = s["level"]
                f = s["factors"].get(x["unit"], 1.0)
                errs_b.append(abs(x["log_ratio"] - base))
                errs_m.append(abs(x["log_ratio"] - base - math.log(f)))
    if not errs_m:
        return None
    m, b = float(np.mean(errs_m)), float(np.mean(errs_b))
    return {"orders": len(errs_m), "model_error_pct": round(100 * (math.exp(m) - 1), 1),
            "old_error_pct": round(100 * (math.exp(b) - 1), 1),
            "improvement_pct": learn.improvement_pct(m, b)}


def backtest() -> dict:
    with _LOCK:
        if "backtest" in _CACHE:
            return _CACHE["backtest"]
    k, cap = _settings()
    obs = _observations()
    per = {}
    for op in RATE_OPS:
        curve = []
        for kk in K_GRID:
            sc = _score(op, kk, cap)
            if sc:
                curve.append({"prior_man_days": kk, "error_pct": sc["model_error_pct"],
                              "improvement_pct": sc["improvement_pct"]})
        used = _score(op, k, cap)
        if not used:
            per[op] = {"available": False}
            continue
        best = min(curve, key=lambda c: c["error_pct"]) if curve else None
        g = learn.grade(used["improvement_pct"])
        per[op] = {
            "available": True, **used, "grade": g,
            "prior_man_days": k, "best_prior_man_days": best["prior_man_days"] if best else None,
            "curve": curve,
            "excluded_completed": obs["excluded"].get(op, 0),
            "rain_divided_out": obs["divide"].get(op), "rain_correlation": (obs.get("rain_corr") or {}).get(op),
            "plain": (f"{WORK_WORDS[op].capitalize()}: checked week by week on {used['orders']:,} orders. "
                      f"Knowing each {UNIT_WORDS[op]}'s own pace, the guess of output per man-day was off by "
                      f"{used['model_error_pct']}%, against {used['old_error_pct']}% if every {UNIT_WORDS[op]} "
                      "is taken as average."),
        }
    ok = [(op, v) for op, v in per.items() if v.get("available")]
    # Weighted by orders checked, so one strong operation cannot grade the rest.
    n = sum(v["orders"] for _, v in ok)
    overall = round(sum(v["orders"] * (v["improvement_pct"] or 0) for _, v in ok) / n, 1) if n else None
    by_op = "; ".join(f"{WORK_WORDS[op]} {'blocks' if op == 'harvest' else 'crews'}: {v['grade']['label'].lower()} "
                      f"({v['improvement_pct']}% better)" for op, v in ok)
    out = {
        "available": bool(ok), "model": "rates", "method": "empirical Bayes shrinkage toward the book rate",
        "by_operation": per,
        "improvement_pct": overall,
        "grade": learn.grade(overall),
        "trained_on": "synthetic",
        "plain": {
            "headline": ("Crew speeds are learned for pruning, weeding and spraying crews, and a pace for "
                         "each harvest block. Each is checked week by week on orders it had not seen."),
            "by_operation": f"By operation, {by_op}.",
            "how": (f"A crew's speed starts at the textbook rate and moves toward what its records show. "
                    f"With {int(k)} man-days of records it sits halfway; a week's move is capped at "
                    f"{int(cap)}%."),
        },
    }
    with _LOCK:
        _CACHE["backtest"] = out
    return out


def backtest_passes(op: str) -> bool:
    b = backtest()
    v = (b.get("by_operation") or {}).get(op) or {}
    return bool(v.get("available") and v["grade"]["passes"])


def recovery() -> dict:
    """Learned speeds against the generator's answer key."""
    with _LOCK:
        if "recovery" in _CACHE:
            return _CACHE["recovery"]
    path = Path(ops._DIR) / "ec_crews.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        truth = {r["crew_code"]: float(r["skill"])
                 for r in csv.DictReader(ln for ln in fh if not ln.startswith("#"))}
    rows = []
    for op in ("prune", "weed", "spray"):
        sp = speeds_at(op, TOMORROW)
        pairs = [(v["factor"], truth[u]) for u, v in sp.items() if u in truth and v["man_days"] >= 20]
        if len(pairs) < 5:
            continue
        corr = float(np.corrcoef([p[0] for p in pairs], [p[1] for p in pairs])[0, 1])
        ok = corr >= 0.7
        rows.append({"effect": f"{op}_speed", "label": f"Which {WORK_WORDS[op]} crews are faster",
                     "planted": "a hidden speed per crew", "found": round(corr, 3), "bar": 0.7,
                     "recovered": ok, "planted_something": True, "crews": len(pairs),
                     "plain": (f"{WORK_WORDS[op].capitalize()}: the generator gave each crew a hidden speed. "
                               f"The learned speeds line up with it at {corr:.2f} across {len(pairs)} crews "
                               "(1.0 would be perfect; the bar is 0.7).")})
    # Harvest: the generator set no block pace on purpose, but its crew sizing
    # (whole cutters, at most nine) leaves one. The honest test of "not
    # invented" is whether a block's pace repeats: the same block measured on
    # January to Lebaran, and again from Lebaran to the end of the export.
    hrows = _observations()["rows"]["harvest"]
    split = date(2025, 3, 24).toordinal()

    def pace(lo, hi):
        sums: dict = defaultdict(lambda: [0.0, 0.0])
        for x in hrows:
            if lo <= x["ord"] < hi:
                sums[x["unit"]][0] += x["md"] * x["log_ratio"]
                sums[x["unit"]][1] += x["md"]
        return {u: v[0] / v[1] for u, v in sums.items() if v[1] >= 20}

    first, second = pace(WINDOW_START.toordinal(), split), pace(split, TOMORROW.toordinal())
    both = [u for u in first if u in second]
    if len(both) > 20:
        corr = float(np.corrcoef([first[u] for u in both], [second[u] for u in both])[0, 1])
        ok = corr >= 0.5
        rows.append({"effect": "harvest_block_pace", "label": "Harvest blocks with a pace of their own",
                     "planted": "none on purpose; the generator's whole-cutter gang sizing leaves one",
                     "found": round(corr, 3), "bar": 0.5, "recovered": ok, "planted_something": False,
                     "blocks": len(both),
                     "plain": (f"Harvesting: no block pace was set on purpose, but the generator sizes gangs "
                               f"in whole cutters, which leaves one. A block's pace from January to Lebaran "
                               f"lines up with its pace after Lebaran at {corr:.2f} across {len(both)} blocks "
                               f"(the bar is 0.5), so {'it is really in the data, not invented' if ok else 'it may be noise'}.")})
    out = {"available": True, "rows": rows, "all_recovered": all(r["recovered"] for r in rows)}
    with _LOCK:
        _CACHE["recovery"] = out
    return out


# ── for the screen ─────────────────────────────────────────────────────────

def table(op: str, d: date | None = None, limit: int = 40) -> dict:
    """Every crew's (or block's) learned speed, fastest and slowest first."""
    d = d or TOMORROW
    if op not in RATE_OPS:
        return {"available": False, "operation": op,
                "reason": ("Pest control man-days are set by treatment method and transport by vehicle "
                           "turnaround, so no speed is learned for them.")}
    av = assumptions.values()
    st = ops._state()
    sp = speeds_at(op, d)
    bt = (backtest().get("by_operation") or {}).get(op) or {}
    rows = []
    for u, v in sp.items():
        if op == "harvest":
            b = st["blocks"].get(u) or {}
            label = b.get("label") or u
            book = ops._harvest_rates().get(f"{b.get('division')}-{b.get('block_code')}") \
                or av["harvest_bunches_per_man_day"]
            unit_label = "bunches"
        else:
            label = u
            acts = ops.OPERATIONS[op]["activities"]
            book = av[RATE_KEY[acts[0]]]
            unit_label = ops.ACTIVITY_UNIT[acts[0]]
        f = v["factor"]
        rows.append({
            "unit": u, "label": label, "factor": f, "man_days": v["man_days"],
            "book_rate": round(book, 2), "learned_rate": round(book * f, 2),
            "history": v["history"],
            "plain": ((f"{label} is {learn.faster_words(f)} at {WORK_WORDS[op]}, based on "
                       f"{v['man_days']:,.0f} man-days of records.") if op != "harvest" else
                      (f"Block {label}: cutters get through {_block_words(f)} bunches a man-day than its "
                       f"target suggests, based on {v['man_days']:,.0f} man-days of records.")),
        })
    rows.sort(key=lambda r: -abs(r["factor"] - 1))
    return {
        "available": True, "operation": op, "unit": UNIT_WORDS[op], "date": d.isoformat(),
        "in_use": int(av["use_learned_rates"]) == 1 and bool(bt.get("grade", {}).get("passes")),
        "rows": rows[:limit], "total": len(rows),
        "faster": sum(1 for r in rows if r["factor"] >= 1.03),
        "slower": sum(1 for r in rows if r["factor"] <= 0.97),
        "backtest": bt,
    }


def _block_words(f: float) -> str:
    d = f - 1.0
    if abs(d) < 0.03:
        return "about as many"
    return f"about {abs(round(100 * d))}% {'more' if d > 0 else 'fewer'}"


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
