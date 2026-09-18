"""How long a supplier really takes, learned from purchase-order history.

SAP plans every order on the supplier's quoted lead time (MARC-PLIFZ). A
quote is not a measurement, and the spread matters more than the average: a
store that plans on 30 days runs out on the order that takes 60.

The method: three numbers per supplier, each on a whiteboard
------------------------------------------------------------
    usual time   = quote x the supplier's own factor x the month's factor
    spread       = how far orders on the same route land from their usual time
    very late    = the chance an order arrives more than 18 days after its usual time

The supplier factor is the middle of what its orders took over what it
quoted, moved toward its route's middle by  orders / (orders + k):  with k = 8
(`leadtime_prior_orders`), a supplier with 8 orders sits halfway. The month
factor is learned per route from the month the order was due, and pulled
toward "no effect" the same way. Nobody tells the model which months are wet.

The trap: an order still open is not a short lead time
------------------------------------------------------
At any cutoff the slowest orders are the ones not yet received. Dropping them
makes a supplier look faster than it is, and the one that misses sailings
looks fastest of all. Every middle and spread here is a Kaplan-Meier estimate:
an order still open at the cutoff counts as "at least this long", and one
already more than 18 days past its usual time counts as very late before it
arrives.

Rush buys are left out: they measure the emergency, not the supplier.

The answer key: the PO file's `delay_cause` says why an order ran late. The
loader strips it; `recovery()` reads it back, to check the learned chance of a
very late order against what the data actually carries.
"""

import csv
import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from threading import Lock

import numpy as np

from gis import assumptions
from gis.build_operations import TOMORROW, WINDOW_END
from gis.models import learn, mm

log = logging.getLogger("estate-command.models.leadtime")

_CACHE: dict = {}
_LOCK = Lock()

LATE_DAYS = 18
MONTH_PRIOR = 6.0
K_GRID = (2, 4, 8, 16, 32)
ITERATIONS = 4
ORIGINS = [date(2024, m, 1) for m in range(6, 13)] + [date(2025, m, 1) for m in range(1, 4)]
MONTH_NAMES = ("", "January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December")


# ── Kaplan-Meier ───────────────────────────────────────────────────────────

def _km(x: np.ndarray, event: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Survival after each sorted value. Censored values reduce who is at risk but never step."""
    if len(x) == 0:
        return x, x
    o = np.lexsort((~event, x))
    x, e = x[o], event[o]
    at_risk = len(x) - np.arange(len(x))
    s = np.cumprod(np.where(e, 1.0 - 1.0 / at_risk, 1.0))
    return x, s


def _km_quantile(x: np.ndarray, event: np.ndarray, q: float) -> float | None:
    xs, s = _km(x, event)
    if len(xs) == 0:
        return None
    hit = np.nonzero((1.0 - s) >= q - 1e-12)[0]
    return float(xs[hit[0]]) if len(hit) else float(xs[-1])


def _km_sample(x: np.ndarray, event: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Inverse-CDF draws. Mass beyond the last event (still-open orders) lands on the largest value seen."""
    xs, s = _km(x, event)
    if len(xs) == 0:
        return np.zeros_like(u)
    cdf = 1.0 - s
    idx = np.searchsorted(cdf, u, side="left")
    return xs[np.minimum(idx, len(xs) - 1)]


# ── observations ───────────────────────────────────────────────────────────

def _orders() -> list[dict]:
    st = mm.state()
    if not st.get("available"):
        return []
    out = []
    for po in st["pos"]:
        if po["bsart"] != "NB":
            continue
        v = st["vendors"][po["lifnr"]]
        m = st["materials"][po["matnr"]]
        out.append({"ebeln": po["ebeln"], "lifnr": po["lifnr"], "route": v["route"], "quoted": v["quoted_days"],
                    "bedat": po["bedat"], "eindt": po["eindt"], "month": po["eindt"].month,
                    "done": po["done"], "matnr": po["matnr"], "menge": po["menge"], "verpr": m["verpr"],
                    "splits": len(po["receipts"])})
    return out


def _prior_orders() -> float:
    return float(assumptions.get("leadtime_prior_orders"))


# ── fitting ────────────────────────────────────────────────────────────────

def fit(cutoff: date, k: float | None = None) -> dict | None:
    """Supplier factors, month factors, spread and very-late chances, from orders placed before `cutoff`."""
    k = _prior_orders() if k is None else float(k)
    key = ("fit", cutoff.toordinal(), k)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    rows = [o for o in _orders() if o["bedat"] < cutoff]
    if len(rows) < 20:
        return None
    n = len(rows)
    t = np.empty(n)
    ev = np.zeros(n, dtype=bool)
    for i, o in enumerate(rows):
        if o["done"] is not None and o["done"] < cutoff:
            t[i] = (o["done"] - o["bedat"]).days
            ev[i] = True
        else:
            t[i] = max((cutoff - o["bedat"]).days, 1)
    q = np.array([o["quoted"] for o in rows], float)
    y = np.log(np.maximum(t, 1.0) / q)
    vend = np.array([o["lifnr"] for o in rows])
    route = np.array([o["route"] for o in rows])
    month = np.array([o["month"] for o in rows])
    vendors = sorted(set(vend))
    routes = sorted(set(route))

    v_eff = {v: 0.0 for v in vendors}
    m_eff = {(r, mo): 0.0 for r in routes for mo in range(1, 13)}
    late = np.zeros(n, dtype=bool)
    for _ in range(ITERATIONS):
        base = np.array([v_eff[vend[i]] + m_eff[(route[i], month[i])] for i in range(n)])
        expected = q * np.exp(base)
        late = (t - expected) > LATE_DAYS          # an open order already this far past counts too
        body = ~late
        # Supplier factors: middle of (y - month), moved toward the route's middle.
        route_mid = {}
        for r in routes:
            sel = body & (route == r)
            route_mid[r] = _km_quantile(y[sel] - base[sel] + np.array([v_eff[x] for x in vend[sel]]),
                                        ev[sel], 0.5) or 0.0
        for v in vendors:
            sel = body & (vend == v)
            r = route[vend == v][0]
            if not sel.any():
                v_eff[v] = route_mid[r]
                continue
            mt = np.array([m_eff[(route[i], month[i])] for i in np.nonzero(sel)[0]])
            mid = _km_quantile(y[sel] - mt, ev[sel], 0.5)
            cnt = float(sel.sum())
            v_eff[v] = (cnt * mid + k * route_mid[r]) / (cnt + k)
        # Month factors, per route: middle of (y - supplier) in that month, against the route's middle.
        for r in routes:
            sel_r = body & (route == r)
            vt = np.array([v_eff[x] for x in vend[sel_r]])
            overall = _km_quantile(y[sel_r] - vt, ev[sel_r], 0.5) or 0.0
            for mo in range(1, 13):
                sel = sel_r & (month == mo)
                if not sel.any():
                    m_eff[(r, mo)] = 0.0
                    continue
                vt_m = np.array([v_eff[x] for x in vend[sel]])
                mid = _km_quantile(y[sel] - vt_m, ev[sel], 0.5)
                cnt = float(sel.sum())
                m_eff[(r, mo)] = cnt / (cnt + MONTH_PRIOR) * (mid - overall)

    # A month factor says how much slower than a typical month, so the typical
    # (median) month is 1.0 and the supplier factor is its usual time in one.
    # Without this the supplier factor quietly carries the season's average.
    for r in routes:
        med = float(np.median([m_eff[(r, mo)] for mo in range(1, 13)]))
        for mo in range(1, 13):
            m_eff[(r, mo)] -= med
        for v in vendors:
            if route[vend == v][0] == r:
                v_eff[v] += med

    base = np.array([v_eff[vend[i]] + m_eff[(route[i], month[i])] for i in range(n)])
    expected = q * np.exp(base)
    late = (t - expected) > LATE_DAYS
    resid = y - base
    spread = {}
    for r in routes:
        sel = (~late) & (route == r)
        spread[r] = {"x": resid[sel], "event": ev[sel]}
    # Very late: decided orders only (arrived, or already past the line).
    decided = ev | late
    route_rate = {r: float(late[decided & (route == r)].mean()) if (decided & (route == r)).any() else 0.0
                  for r in routes}
    tail = {}
    for v in vendors:
        sel = decided & (vend == v)
        cnt = float(sel.sum())
        own = float(late[sel].mean()) if cnt else 0.0
        r = route[vend == v][0]
        tail[v] = {"chance": (cnt * own + k * route_rate[r]) / (cnt + k), "own": own, "decided": int(cnt)}
    excess = (t - expected)[late & ev]
    counts = defaultdict(lambda: {"orders": 0, "arrived": 0, "open": 0})
    for i in range(n):
        c = counts[vend[i]]
        c["orders"] += 1
        c["arrived" if ev[i] else "open"] += 1

    out = {"cutoff": cutoff, "k": k, "vendor": v_eff, "month": m_eff, "spread": spread, "tail": tail,
           "excess": excess if len(excess) else np.array([float(LATE_DAYS + 10)]),
           "counts": dict(counts), "orders": n, "censored": int((~ev).sum())}
    with _LOCK:
        _CACHE[key] = out
    return out


def _factor(f: dict, lifnr: str, eindt_month: int) -> float:
    st = mm.state()
    r = st["vendors"][lifnr]["route"]
    return math.exp(f["vendor"].get(lifnr, 0.0) + f["month"].get((r, eindt_month), 0.0))


def usual_days(f: dict, lifnr: str, bedat: date) -> float:
    v = mm.state()["vendors"][lifnr]
    eindt = bedat + timedelta(days=v["quoted_days"])
    return v["quoted_days"] * _factor(f, lifnr, eindt.month)


def draws(f: dict, lifnr: str, bedat: date, n: int = 2000, rng=None) -> np.ndarray:
    """Lead times in days for an order placed on `bedat`."""
    rng = rng or np.random.default_rng(7)
    v = mm.state()["vendors"][lifnr]
    usual = usual_days(f, lifnr, bedat)
    sp = f["spread"].get(v["route"]) or {"x": np.zeros(1), "event": np.ones(1, dtype=bool)}
    body = usual * np.exp(_km_sample(sp["x"], sp["event"], rng.random(n)))
    tail = rng.random(n) < (f["tail"].get(lifnr) or {"chance": 0.0})["chance"]
    body[tail] += f["excess"][rng.integers(0, len(f["excess"]), int(tail.sum()))]
    return np.maximum(1.0, np.rint(body))


def remaining_draws(f: dict, po: dict, on: date, n: int = 1000, rng=None) -> np.ndarray:
    """Days from `on` until an open order completes, given it has not yet."""
    rng = rng or np.random.default_rng(11)
    elapsed = (on - po["bedat"]).days
    d = draws(f, po["lifnr"], po["bedat"], n * 4, rng)
    keep = d[d > elapsed]
    if len(keep) < n:
        # Past everything the model has seen: expect it within the late orders' excess.
        extra = elapsed + f["excess"][rng.integers(0, len(f["excess"]), n - len(keep))] * 0.5
        keep = np.concatenate([keep, extra])
    return keep[:n] - elapsed


def summary(f: dict, lifnr: str, bedat: date) -> dict:
    d = draws(f, lifnr, bedat, 4000)
    v = mm.state()["vendors"][lifnr]
    return {"quoted": v["quoted_days"], "usual": round(usual_days(f, lifnr, bedat), 1),
            "median": float(np.median(d)), "p90": float(np.percentile(d, 90)),
            "p97": float(np.percentile(d, 97)),
            "very_late_chance": round((f["tail"].get(lifnr) or {"chance": 0.0})["chance"], 3)}


# ── scoring ────────────────────────────────────────────────────────────────

def _score(k: float) -> dict | None:
    orders = _orders()
    err_m, err_b, inside, n_open = [], [], [], 0
    folds = []
    for o in ORIGINS:
        f = fit(o, k)
        if not f:
            continue
        nxt = date(o.year + (o.month == 12), o.month % 12 + 1, 1)
        fold_m, fold_b = [], []
        for po in orders:
            if not (o <= po["bedat"] < nxt):
                continue
            if po["done"] is None:
                n_open += 1
                continue
            actual = (po["done"] - po["bedat"]).days
            pred = usual_days(f, po["lifnr"], po["bedat"])
            p90 = float(np.percentile(draws(f, po["lifnr"], po["bedat"], 2000), 90))
            fold_m.append(abs(actual - pred))
            fold_b.append(abs(actual - po["quoted"]))
            inside.append(actual <= p90)
        if fold_m:
            folds.append({"origin": o.isoformat(), "orders": len(fold_m),
                          "model_error_days": round(float(np.mean(fold_m)), 1),
                          "quote_error_days": round(float(np.mean(fold_b)), 1)})
            err_m += fold_m
            err_b += fold_b
    if not err_m:
        return None
    m, b = float(np.mean(err_m)), float(np.mean(err_b))
    return {"orders": len(err_m), "model_error_days": round(m, 1), "quote_error_days": round(b, 1),
            "improvement_pct": learn.improvement_pct(m, b), "p90_coverage_pct": round(100 * float(np.mean(inside)), 1),
            "still_open": n_open, "folds": folds}


def backtest() -> dict:
    with _LOCK:
        if "backtest" in _CACHE:
            return _CACHE["backtest"]
    if not mm.available():
        return {"available": False, "reason": "No MM records."}
    k = _prior_orders()
    used = _score(k)
    if not used:
        return {"available": False, "reason": "Not enough purchase orders to check lead times."}
    curve = []
    for kk in K_GRID:
        sc = used if kk == k else _score(kk)
        if sc:
            curve.append({"prior_orders": kk, "error_days": sc["model_error_days"],
                          "improvement_pct": sc["improvement_pct"]})
    best = min(curve, key=lambda c: c["error_days"]) if curve else None
    g = learn.grade(used["improvement_pct"])
    cov = used["p90_coverage_pct"]
    out = {
        "available": True, "model": "leadtime", "trained_on": "synthetic",
        "method": "Kaplan-Meier supplier and month factors on the quote, shrunk toward the route",
        **used, "grade": g, "prior_orders": k, "best_prior_orders": best["prior_orders"] if best else None,
        "curve": curve, "coverage_ok": 85.0 <= cov <= 95.0,
        "plain": {
            "headline": (f"Checked month by month on {used['orders']:,} orders placed after each cutoff: the usual "
                         f"time was off by {used['model_error_days']} days on average, against "
                         f"{used['quote_error_days']} days for the supplier's quote."),
            "coverage": (f"The 'one order in ten takes longer' line held on {cov}% of those orders "
                         f"(the aim is 90%)."),
            "open": (f"{used['still_open']} orders placed in those months had not arrived by "
                     f"{WINDOW_END.strftime('%d %B %Y').lstrip('0')} and are not scored."),
        },
    }
    with _LOCK:
        _CACHE["backtest"] = out
    return out


def recovery() -> dict:
    """What the model learned against what the generator planted."""
    with _LOCK:
        if "recovery" in _CACHE:
            return _CACHE["recovery"]
    from gis import build_materials as bm
    f = fit(TOMORROW)
    if not f:
        return {"available": False}
    st = mm.state()
    rows = []
    for lifnr, p in bm.PLANTED.items():
        v = st["vendors"][lifnr]
        found = math.exp(f["vendor"][lifnr])
        ok = abs(found / p["ratio"] - 1.0) <= 0.10
        rows.append({"effect": f"quote_{lifnr}", "label": f"{v['name']}: real time over the quote",
                     "planted": p["ratio"], "found": round(found, 3), "bar": "within 10%", "recovered": ok,
                     "planted_something": True, "orders": f["counts"].get(lifnr, {}).get("orders", 0),
                     "plain": (f"{v['name']} quotes {v['quoted_days']} days. The generator made its usual time "
                               f"{p['ratio']:.2f} times the quote; the model found {found:.2f} "
                               f"from {f['counts'].get(lifnr, {}).get('orders', 0)} orders.")})
    # The season is planted as a ratio (due December-March against any other
    # month), so it is checked as one. The model's month factors are anchored on
    # the median month, which is a choice of scale, not a finding.
    for r, planted in bm.SEASON.items():
        wet = [math.exp(f["month"][(r, mo)]) for mo in bm.WET_MONTHS]
        dry = [math.exp(f["month"][(r, mo)]) for mo in range(1, 13) if mo not in bm.WET_MONTHS]
        dry_mean = float(np.mean(dry))
        ratio = float(np.mean(wet)) / dry_mean
        wobble = max(abs(x / dry_mean - 1.0) for x in dry)
        words = "sea" if r == "sea" else "road"
        rows.append({"effect": f"wet_season_{r}", "label": f"Wet season, by {words}",
                     "planted": planted, "found": round(ratio, 3), "bar": "within 0.08",
                     "recovered": abs(ratio - planted) <= 0.08, "planted_something": True,
                     "plain": (f"Orders by {words} due December to March were made {planted:.2f} times as slow as "
                               f"other months. Without being told which months, the model found {ratio:.2f}.")})
        rows.append({"effect": f"dry_months_{r}", "label": f"No season April to November, by {words}",
                     "planted": 0.0, "found": round(wobble, 3), "bar": "each month within 15% of the others",
                     "recovered": wobble <= 0.15, "planted_something": False,
                     "plain": (f"Nothing was planted April to November by {words}. The furthest of those eight months "
                               f"sits {wobble:.0%} from their average, which is noise.")})

    # Very late orders, three figures each: what was planted, what the data
    # carries, what the model found. "Carries" is the share of the supplier's
    # arrived orders more than LATE_DAYS past the generator's own usual time;
    # the ordinary spread puts a few there even where no sailing was missed.
    # Reads the answer key, which is what recovery is for.
    with (mm._DIR / "ec_mm_purchase_orders.csv").open(encoding="utf-8", newline="") as fh:
        cause = {r["ebeln"]: r["delay_cause"] for r in csv.DictReader(ln for ln in fh if not ln.startswith("#"))}
    orders = [o for o in _orders() if o["done"] is not None]
    for lifnr, p in bm.PLANTED.items():
        v = st["vendors"][lifnr]
        mine = [o for o in orders if o["lifnr"] == lifnr]
        planted_usual = [o["quoted"] * p["ratio"] * (bm.SEASON[o["route"]] if o["month"] in bm.WET_MONTHS else 1.0)
                         for o in mine]
        carried = sum(1 for o, u in zip(mine, planted_usual)
                      if (o["done"] - o["bedat"]).days - u > LATE_DAYS) / max(len(mine), 1)
        missed = sum(1 for o in mine if cause.get(o["ebeln"]) == "vessel_missed") / max(len(mine), 1)
        found = f["tail"][lifnr]["chance"]
        ok = abs(found - carried) <= 0.05
        if p.get("tail"):
            rows.append({"effect": f"missed_sailings_{lifnr}", "label": f"{v['name']}: missed sailings",
                         "planted": p["tail"], "carried": round(carried, 3), "found": round(found, 3),
                         "bar": "within 5 points of what the data carries", "recovered": ok,
                         "planted_something": True,
                         "plain": (f"The generator gave {v['name']} a {p['tail']:.0%} chance of missing a sailing, and "
                                   f"{missed:.0%} of its {len(mine)} orders did. With the ordinary spread, {carried:.0%} "
                                   f"arrived very late; the model gives {found:.0%}.")})
        else:
            rows.append({"effect": f"very_late_{lifnr}", "label": f"{v['name']}: very late orders",
                         "planted": 0.0, "carried": round(carried, 3), "found": round(found, 3),
                         "bar": "within 5 points of what the data carries", "recovered": ok,
                         "planted_something": False,
                         "plain": (f"No missed sailings were planted for {v['name']}. {carried:.0%} of its "
                                   f"{len(mine)} orders still landed very late by the ordinary spread; the model "
                                   f"gives {found:.0%}" + (", so it invents nothing." if ok else ".") )})


    splits = [o for o in orders if o["lifnr"] == "V103"]
    share = sum(1 for o in splits if o["splits"] > 1) / max(len(splits), 1)
    rows.append({"effect": "split_deliveries", "label": "Meroke Tetap Jaya delivers in two trucks",
                 "planted": bm.PLANTED["V103"]["split"], "found": round(share, 3), "bar": "within 10 points",
                 "recovered": abs(share - bm.PLANTED["V103"]["split"]) <= 0.10, "planted_something": True,
                 "plain": (f"{share:.0%} of Meroke Tetap Jaya's orders arrived in two receipts. Their lead time is "
                           "counted to the receipt that completes the order, not the first truck.")})

    # Not planted: order size and weekday. Residuals on arrived, not-late orders.
    res, size, wd = [], [], []
    med_qty = defaultdict(list)
    for o in orders:
        med_qty[o["matnr"]].append(o["menge"])
    med_qty = {m: float(np.median(v)) for m, v in med_qty.items()}
    for o in orders:
        actual = (o["done"] - o["bedat"]).days
        usual = usual_days(f, o["lifnr"], o["bedat"])
        if actual - usual > LATE_DAYS:
            continue
        res.append(math.log(actual / usual))
        size.append(math.log(o["menge"] / med_qty[o["matnr"]]))
        wd.append(o["bedat"].weekday())
    res, size, wd = np.array(res), np.array(size), np.array(wd)
    slope = float(np.polyfit(size, res, 1)[0]) if np.std(size) > 0 else 0.0
    rows.append({"effect": "order_size", "label": "Bigger orders take longer",
                 "planted": 0.0, "found": round(slope, 3), "bar": "at most 0.05 either way",
                 "recovered": abs(slope) <= 0.05, "planted_something": False,
                 "plain": (f"Nothing was planted. Doubling an order changes its time by {100 * (2 ** slope - 1):+.1f}% "
                           "in the data, which is no effect.")})
    by_wd = [float(res[wd == i].mean()) for i in range(7) if (wd == i).sum() >= 10]
    spread_wd = max(abs(x) for x in by_wd) if by_wd else 0.0
    rows.append({"effect": "weekday", "label": "The day an order is placed",
                 "planted": 0.0, "found": round(spread_wd, 3), "bar": "at most 0.06",
                 "recovered": spread_wd <= 0.06, "planted_something": False,
                 "plain": (f"Nothing was planted. The largest weekday difference is {100 * (math.exp(spread_wd) - 1):.1f}%, "
                           "which is noise.")})
    out = {"available": True, "rows": rows, "all_recovered": all(r["recovered"] for r in rows)}
    with _LOCK:
        _CACHE["recovery"] = out
    return out


# ── for the screen ─────────────────────────────────────────────────────────

def table(on: date | None = None) -> dict:
    """Every supplier: quote, usual time, one-in-ten line, very-late chance, season."""
    on = on or TOMORROW
    f = fit(on)
    st = mm.state()
    if not f:
        return {"available": False, "reason": "Not enough purchase orders."}
    rows = []
    for lifnr, v in st["vendors"].items():
        if lifnr not in f["vendor"]:
            continue
        s = summary(f, lifnr, on)
        months = [{"month": mo, "label": MONTH_NAMES[mo][:3],
                   "factor": round(math.exp(f["month"].get((v["route"], mo), 0.0)), 3)} for mo in range(1, 13)]
        wet = [m for m in months if m["factor"] >= 1.1]
        mats = sorted({st["materials"][po["matnr"]]["maktx"] for po in st["pos"] if po["lifnr"] == lifnr})
        c = f["counts"].get(lifnr, {})
        rows.append({
            "lifnr": lifnr, "name": v["name"], "city": v["city"], "route": v["route"], "materials": mats,
            "quoted_days": v["quoted_days"], "usual_days": round(v["quoted_days"] * math.exp(f["vendor"][lifnr]), 1),
            "factor": round(math.exp(f["vendor"][lifnr]), 3),
            "median_now": s["median"], "p90_now": s["p90"], "very_late_chance": s["very_late_chance"],
            "orders": c.get("orders", 0), "open": c.get("open", 0), "months": months,
            "slow_months": [m["label"] for m in wet],
            "plain": (f"{v['name']} quotes {v['quoted_days']} days and usually takes "
                      f"{v['quoted_days'] * math.exp(f['vendor'][lifnr]):.0f}. An order placed today: most likely "
                      f"{s['median']:.0f} days, and 1 in 10 takes {s['p90']:.0f} or more"
                      + (f"; {s['very_late_chance']:.0%} chance of arriving very late" if s["very_late_chance"] >= 0.05 else "")
                      + "."),
        })
    rows.sort(key=lambda r: -r["factor"])
    return {"available": True, "date": on.isoformat(), "suppliers": rows, "orders": f["orders"],
            "censored": f["censored"], "prior_orders": f["k"], "backtest": backtest()}


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
