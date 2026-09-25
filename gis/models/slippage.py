"""How much of the plan gets done, and which blocks are likely to be carried.

The plan used to multiply every order by one figure: the ledger's adherence
in the day's rain band. A gang on an impassable-when-wet road and one on a
good road got the same expectation, and on a replay the band was chosen from
rain nobody had at six in the morning.

The model
---------
Two parts per order, on the order's conditions:

  1. Will it be worked at all?   logistic regression on weathered off or
                                 not started
  2. If worked, what share?      linear regression on log(actual / planned)

Log because rain, road and turnout compound; they multiply the day, they do
not add to it. The conditions:

    rain on the day      hinged at 5, 10 and 25 mm, separately for harvest,
                         upkeep and pest work
    road condition       and road x 15 mm, road x 25 mm
    turnout              present over the crew's recent normal
    block history        how this block's orders have gone before (earlier
                         days only), pulled toward zero when there are few
    crew history         the same for the crew; offered so the test can show
                         it adds nothing once turnout is known
    days carried         a job already carried is a job that keeps slipping
    Sunday, spraying in rain of 15 mm+, rain of 45 mm+

Trained on what happened, served on what was known
---------------------------------------------------
The effects are learned from recorded rain and recorded turnout, which is
where they can be measured. At plan time neither exists, so the plan runs
the order through every plausible rain amount from the rain model and the
turnout the headcount model expects, and averages. That gives the expected
share done and a 1-in-10 to 9-in-10 band.

Scored twice, week by week on orders it had not seen: once knowing the
weather (does it understand what drives a miss?) and once as at six in the
morning (does it help the plan?), each against the method it replaces.
"""

import logging
import math
import random
from collections import defaultdict
from datetime import date, timedelta
from threading import Lock

import numpy as np

from gis import assumptions, ops
from gis.build_operations import COMPLETE_AT, RAIN_HEAVY_MM, SPRAY_RAIN_MM, WINDOW_END
from gis.models import learn

log = logging.getLogger("estate-command.models.slippage")

_CACHE: dict = {}
_LOCK = Lock()
MAX_FITS = 24

OPS = ("harvest", "prune", "weed", "spray", "pest")
ACTIVITIES = ["harvest", "pruning", "circle_weeding", "path_upkeep", "spraying",
              "census", "treatment", "followup"]
GROUP = {"harvest": "harvest", "prune": "upkeep", "weed": "upkeep", "spray": "upkeep", "pest": "pest"}
ROADS = ["fair", "poor", "impassable-when-wet"]
STOP_MM = 45.0
PRIOR_N = 5.0
SHARE_CAP = 1.15
# A block is flagged as likely to need another day at 7 in 10. At 5 in 10 most
# harvest blocks qualify, since an order counts as finished only at 85%.
RISK_P = 0.7

COLS = (ACTIVITIES
        + [f"{g}_rain_{k}" for g in ("harvest", "upkeep", "pest") for k in (5, 10, 25)]
        + [f"road_{r}" for r in ROADS]
        + [f"road_{r}_wet15" for r in ROADS]
        + [f"road_{r}_wet25" for r in ROADS]
        + ["turnout", "block_history", "crew_history", "carried", "sunday",
           "spray_wet15", "rain_45", "lebaran"])
CI = {c: i for i, c in enumerate(COLS)}

ROAD_WORDS = {"good": "good", "fair": "fair", "poor": "poor",
              "impassable-when-wet": "impassable when wet"}


def _x_rows(activity: np.ndarray, group: np.ndarray, rain: np.ndarray, road: np.ndarray,
            turnout: np.ndarray, block_h: np.ndarray, crew_h: np.ndarray,
            carried: np.ndarray, days: np.ndarray) -> np.ndarray:
    """The design matrix for many orders at once; every argument is per row.
    `days` are date ordinals: Sunday and Lebaran leave are read from them."""
    from gis.models.headcount import _phase
    n = len(rain)
    days = np.asarray(days, dtype=int)
    uniq = {o: date.fromordinal(int(o)) for o in set(days.tolist())}
    sunday = np.array([uniq[o].weekday() == 6 for o in days.tolist()], dtype=float)
    leave = np.array([_phase(uniq[o]) == "leave" for o in days.tolist()], dtype=float)
    X = np.zeros((n, len(COLS)))
    for a in ACTIVITIES:
        X[:, CI[a]] = activity == a
    for g in ("harvest", "upkeep", "pest"):
        m = group == g
        for k in (5, 10, 25):
            X[:, CI[f"{g}_rain_{k}"]] = np.where(m, np.maximum(0.0, rain - k), 0.0)
    wet15, wet25 = rain >= SPRAY_RAIN_MM, rain >= RAIN_HEAVY_MM
    for r in ROADS:
        m = road == r
        X[:, CI[f"road_{r}"]] = m
        X[:, CI[f"road_{r}_wet15"]] = m & wet15
        X[:, CI[f"road_{r}_wet25"]] = m & wet25
    X[:, CI["turnout"]] = turnout
    X[:, CI["block_history"]] = block_h
    X[:, CI["crew_history"]] = crew_h
    X[:, CI["carried"]] = np.minimum(carried, 5) / 5.0
    X[:, CI["sunday"]] = sunday
    X[:, CI["spray_wet15"]] = (activity == "spraying") & wet15
    X[:, CI["rain_45"]] = rain >= STOP_MM
    X[:, CI["lebaran"]] = leave
    return X


# ── the table ──────────────────────────────────────────────────────────────

def _turnout_ratio(code: str, ds: str) -> float:
    from gis.models import headcount
    r = (headcount._data()["by_crew"].get(code) or {}).get(ds)
    if not r or not r["on_roll"]:
        return 0.0
    rec = headcount.recent_rate(code, date.fromisoformat(ds))
    if not rec:
        return 0.0
    return float(np.clip(math.log(max(r["present"], 0.5) / (r["on_roll"] * rec)), -1.5, 0.4))


def _table() -> dict:
    with _LOCK:
        if "table" in _CACHE:
            return _CACHE["table"]
    st = ops._state()
    rows = []
    for op in OPS:
        for r in st["orders"].get(op) or []:
            if r["planned_qty"] > 0:
                rows.append((op, r))
    rows.sort(key=lambda x: (x[1]["date"], x[1]["order_id"]))

    # Chains: how many times this job had already been carried.
    parent = {r["carried_to"]: r["order_id"] for _, r in rows if r["carried_to"]}
    depth: dict = {}
    for _, r in rows:
        p = parent.get(r["order_id"])
        depth[r["order_id"]] = (depth.get(p, 0) + 1) if p else 0

    # Block and crew history, strictly from earlier days.
    n = len(rows)
    block_h, crew_h = np.zeros(n), np.zeros(n)
    sums_b: dict = defaultdict(lambda: [0.0, 0])
    sums_c: dict = defaultdict(lambda: [0.0, 0])
    op_mean: dict = defaultdict(lambda: [0.0, 0])
    i = 0
    while i < n:
        day = rows[i][1]["date"]
        j = i
        while j < n and rows[j][1]["date"] == day:
            j += 1
        for k in range(i, j):
            op, r = rows[k]
            sb, sc = sums_b[(op, r["block_key"])], sums_c[(op, r["crew_code"])]
            block_h[k] = sb[0] / (sb[1] + PRIOR_N)
            crew_h[k] = sc[0] / (sc[1] + PRIOR_N)
        for k in range(i, j):
            op, r = rows[k]
            if r["actual_qty"] <= 0:
                continue
            la = math.log(min(max(r["actual_qty"] / r["planned_qty"], 0.02), SHARE_CAP))
            om = op_mean[op]
            dev = la - (om[0] / om[1] if om[1] else la)
            om[0] += la
            om[1] += 1
            for s in (sums_b[(op, r["block_key"])], sums_c[(op, r["crew_code"])]):
                s[0] += dev
                s[1] += 1
        i = j

    ops_ = np.array([op for op, _ in rows])
    act = np.array([r["activity"] for _, r in rows])
    grp = np.array([GROUP[op] for op in ops_])
    rain = np.array([r["rain_mm"] or 0.0 for _, r in rows])
    road = np.array([r["road_condition"] or "good" for _, r in rows])
    turnout = np.array([_turnout_ratio(r["crew_code"], r["date"]) for _, r in rows])
    carried = np.array([depth.get(r["order_id"], 0) for _, r in rows], dtype=float)
    dates = [date.fromisoformat(r["date"]) for _, r in rows]
    ords = np.array([d.toordinal() for d in dates])
    planned = np.array([r["planned_qty"] for _, r in rows], dtype=float)
    actual = np.array([r["actual_qty"] for _, r in rows], dtype=float)
    X = _x_rows(act, grp, rain, road, turnout, block_h, crew_h, carried, ords)
    out = {
        "X": X, "op": ops_, "activity": act, "group": grp, "rain": rain, "road": road,
        "turnout": turnout, "block_h": block_h, "crew_h": crew_h, "carried": carried,
        "dates": dates, "ord": ords,
        "planned": planned, "actual": actual,
        "worked": actual > 0,
        "share": np.clip(actual / planned, 0.0, SHARE_CAP),
        "block": np.array([r["block_key"] for _, r in rows]),
        "crew": np.array([r["crew_code"] for _, r in rows]),
        "status": np.array([r["status"] for _, r in rows]),
    }
    with _LOCK:
        _CACHE["table"] = out
    return out


# ── fitting ────────────────────────────────────────────────────────────────

def _fit(cutoff: date) -> dict | None:
    key = ("fit", cutoff.isoformat())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    T = _table()
    m = T["ord"] < cutoff.toordinal()
    if m.sum() < 800:
        return None
    X = T["X"][m]
    worked = T["worked"][m].astype(float)
    b_work = learn.logistic(X, worked, lam=0.5)
    w = m & T["worked"]
    y = np.log(np.clip(T["share"][w], 0.02, SHARE_CAP))
    b_share = learn.ridge(T["X"][w], y, lam=0.5)
    resid = y - T["X"][w] @ b_share
    # The misses are lopsided (a share is capped above and can fall a long way),
    # so the expected share averages over the model's own past misses rather
    # than assuming a bell curve. 200 evenly spaced quantiles keep it small.
    q = np.linspace(0.0025, 0.9975, 200)
    resid_q = {g: (np.quantile(resid[T["group"][w] == g], q) if (T["group"][w] == g).sum() > 50
                   else np.quantile(resid, q)) for g in ("harvest", "upkeep", "pest")}
    sigma = {g: float(np.std(v)) for g, v in resid_q.items()}
    out = {"b_work": b_work, "b_share": b_share, "sigma": sigma, "resid_q": resid_q,
           "rows": int(m.sum()),
           "cutoff": cutoff}
    with _LOCK:
        _CACHE[key] = out
        fits = [k for k in _CACHE if isinstance(k, tuple) and k[0] == "fit"]
        for k in fits[:-MAX_FITS]:
            _CACHE.pop(k, None)
    return out


def _cutoff_for(d: date) -> date:
    return min(d, WINDOW_END + timedelta(days=1))


def _expect(fit: dict, X: np.ndarray, group: np.ndarray) -> dict:
    """Expected share done, chance not worked and chance carried, per row."""
    pw = learn.sigmoid(X @ fit["b_work"])
    mu = X @ fit["b_share"]
    share_if = np.zeros(len(mu))
    below = np.zeros(len(mu))
    for g in set(group.tolist()):
        m = group == g
        r = fit["resid_q"][g]
        # Chunks keep the (rows x 200) matrix small.
        idx = np.where(m)[0]
        for c in range(0, len(idx), 4000):
            ii = idx[c:c + 4000]
            vals = np.minimum(SHARE_CAP, np.exp(mu[ii][:, None] + r[None, :]))
            share_if[ii] = vals.mean(axis=1)
            below[ii] = (vals < COMPLETE_AT).mean(axis=1)
    return {"p_worked": pw, "share": pw * share_if, "p_carried": (1 - pw) + pw * below, "mu": mu}


# ── priors at a date, for serving ──────────────────────────────────────────

def _history_at(d: date) -> tuple[dict, dict]:
    """Block and crew history as known the evening before `d`."""
    key = ("hist", d.isoformat())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    T = _table()
    cut = d.toordinal()
    m = (T["ord"] < cut) & T["worked"]
    la = np.log(np.clip(T["share"][m], 0.02, SHARE_CAP))
    op = T["op"][m]
    means = {o: float(la[op == o].mean()) for o in set(op)}
    dev = la - np.array([means[o] for o in op])
    bs: dict = defaultdict(lambda: [0.0, 0])
    cs: dict = defaultdict(lambda: [0.0, 0])
    for o, b, c, v in zip(op, T["block"][m], T["crew"][m], dev):
        bs[(o, b)][0] += v
        bs[(o, b)][1] += 1
        cs[(o, c)][0] += v
        cs[(o, c)][1] += 1
    block = {k: v[0] / (v[1] + PRIOR_N) for k, v in bs.items()}
    crew = {k: v[0] / (v[1] + PRIOR_N) for k, v in cs.items()}
    with _LOCK:
        _CACHE[key] = (block, crew)
    return block, crew


# ── serving the plan ───────────────────────────────────────────────────────

def _rain_draws(d: date, overrides_rain: float | None) -> tuple[np.ndarray, str]:
    from gis.models import rain
    if overrides_rain is not None:
        return np.array([float(overrides_rain)]), "set on the plan"
    if int(assumptions.get("use_rain_model")) == 1:
        dr = rain.draws(d)
        if len(dr):
            return dr, "the rain forecast"
    rec = ops._state()["rain"].get(d.isoformat())
    if rec is not None and d <= WINDOW_END:
        return np.array([rec]), "rain recorded on the day"
    return np.array([0.0]), "no rain information"


def item_expectations(op: str, d: date, items: list[dict], rain_mm: float | None = None) -> dict:
    """Per candidate block, before any crew is chosen: expected share done,
    chance it is not worked and chance it is carried, averaged over the rain
    the forecast allows. Turnout is taken as normal here; the crew's own
    turnout enters once a crew is assigned."""
    if op not in OPS or not items:
        return {}
    fit = _fit(_cutoff_for(d))
    if fit is None:
        return {}
    draws, source = _rain_draws(d, rain_mm)
    block_h, crew_h = _history_at(d)
    n, S = len(items), len(draws)
    act = np.array([i.get("activity") or op for i in items])
    act = np.where(np.isin(act, ACTIVITIES), act, op if op in ACTIVITIES else "harvest")
    grp = np.array([GROUP[op]] * n)
    road = np.array([i.get("road_condition") or "good" for i in items])
    bh = np.array([block_h.get((op, i["block_key"]), 0.0) for i in items])
    carried = np.array([1.0 if i.get("in_progress") else 0.0 for i in items])
    sunday = np.full(n, d.toordinal())
    # All rain draws at once: rows are (item, draw).
    R = np.repeat(draws[None, :], n, axis=0).ravel()
    rep = lambda a: np.repeat(a, S)
    X = _x_rows(rep(act), rep(grp), R, rep(road), np.zeros(n * S), rep(bh), np.zeros(n * S),
                rep(carried), rep(sunday))
    e = _expect(fit, X, rep(grp))
    share = e["share"].reshape(n, S).mean(axis=1)
    p_not = (1 - e["p_worked"]).reshape(n, S).mean(axis=1)
    p_car = e["p_carried"].reshape(n, S).mean(axis=1)
    # What the same block would do on a dry day, for the drivers.
    Xd = _x_rows(act, grp, np.zeros(n), road, np.zeros(n), bh, np.zeros(n), carried, sunday)
    dry = _expect(fit, Xd, grp)["share"]
    Xg = _x_rows(act, grp, np.zeros(n), np.array(["good"] * n), np.zeros(n), np.zeros(n),
                 np.zeros(n), carried, sunday)
    base = _expect(fit, Xg, grp)["share"]
    p15 = float(np.mean(draws >= SPRAY_RAIN_MM))
    out = {}
    for k, it in enumerate(items):
        drivers = []
        rain_cost = dry[k] - share[k]
        if rain_cost >= 0.03:
            drivers.append(f"Rain could cost about {round(100 * rain_cost)}% of the work "
                           f"(15 mm or more is {learn.chance_words(p15)}).")
        road_cost = base[k] - _expect(fit, _x_rows(act[k:k + 1], grp[k:k + 1], np.zeros(1), road[k:k + 1],
                                                 np.zeros(1), np.zeros(1), np.zeros(1),
                                                 carried[k:k + 1], sunday[k:k + 1]), grp[k:k + 1])["share"][0]
        if road[k] != "good" and road_cost >= 0.03:
            drivers.append(f"The road is {ROAD_WORDS.get(road[k], road[k])}, about "
                           f"{round(100 * road_cost)}% less done on a dry day.")
        if abs(bh[k]) >= 0.04:
            drivers.append(f"Work on this block has {'fallen short' if bh[k] < 0 else 'gone better'} "
                           f"before ({learn.signed_pct(math.exp(bh[k]) - 1)} against the usual).")
        if carried[k]:
            drivers.append("The job is already under way and has been carried before.")
        out[it["block_key"]] = {
            "expected_share": round(float(share[k]), 3),
            "p_not_worked": round(float(p_not[k]), 3),
            "p_carried": round(float(p_car[k]), 3),
            "dry_share": round(float(dry[k]), 3),
            "drivers": drivers,
        }
    return {"items": out, "rain_source": source, "draws": len(draws)}


def plan_expectations(op: str, d: date, crews: list[dict], rain_mm: float | None = None,
                      seed: int = 7) -> dict | None:
    """Expected work done per crew and in total, with a 1-in-10 to 9-in-10
    band, simulating rain (shared by every crew) and each crew's turnout."""
    from gis.models import headcount
    if op not in OPS:
        return None
    fit = _fit(_cutoff_for(d))
    if fit is None:
        return None
    draws, source = _rain_draws(d, rain_mm)
    block_h, crew_h = _history_at(d)
    rng = np.random.default_rng(seed)
    S = 300
    r_s = draws[rng.integers(0, len(draws), S)]
    tot = np.zeros(S)
    tot_planned = 0.0
    crew_out = []
    at_risk = []
    for c in crews:
        blocks = c.get("blocks") or []
        if not blocks:
            continue
        qty = np.array([b["qty"] for b in blocks], dtype=float)
        n = len(blocks)
        act = np.array([b.get("activity") or op for b in blocks])
        act = np.where(np.isin(act, ACTIVITIES), act, op if op in ACTIVITIES else "harvest")
        grp = np.array([GROUP[op]] * n)
        road = np.array([b.get("road_condition") or "good" for b in blocks])
        bh = np.array([block_h.get((op, b["block_key"]), 0.0) for b in blocks])
        ch = np.full(n, crew_h.get((op, c["crew_code"]), 0.0))
        carried = np.array([1.0 if b.get("in_progress") else 0.0 for b in blocks])
        sunday = np.full(n, d.toordinal())
        # Turnout draws: the headcount model's spread around what the plan expects.
        pres = headcount.present_draws(c["crew_code"], d, (r_s >= RAIN_HEAVY_MM).astype(float), rng) \
            if int(assumptions.get("use_headcount_model")) == 1 else None
        expected_present = max(float(c.get("present") or 0), 0.5)
        if pres is not None and c.get("model_present") is not None:
            ratio = np.log(np.maximum(pres, 0.5) / max(float(c["model_present"]), 0.5))
        else:
            ratio = np.zeros(S)
        ratio = np.clip(ratio, -1.5, 0.4)
        R = np.repeat(r_s[None, :], n, axis=0).ravel()
        rep = lambda a: np.repeat(a, S)
        X = _x_rows(rep(act), rep(grp), R, rep(road), np.tile(ratio, n), rep(bh), rep(ch),
                    rep(carried), rep(sunday))
        e = _expect(fit, X, rep(grp))
        worked = rng.random(n * S) < e["p_worked"]
        r = fit["resid_q"][GROUP[op]]
        share = np.minimum(SHARE_CAP, np.exp(e["mu"] + r[rng.integers(0, len(r), n * S)]))
        done = (worked * share).reshape(n, S) * qty[:, None]
        crew_done = done.sum(axis=0)
        tot += crew_done
        tot_planned += float(qty.sum())
        exp_share_blocks = e["share"].reshape(n, S).mean(axis=1)
        p_car = e["p_carried"].reshape(n, S).mean(axis=1)
        for k, b in enumerate(blocks):
            b["expected_share"] = round(float(exp_share_blocks[k]), 3)
            b["p_carried"] = round(float(p_car[k]), 3)
            if p_car[k] >= RISK_P:
                at_risk.append({"block_label": b["block_label"], "crew_code": c["crew_code"],
                                "p_carried": round(float(p_car[k]), 3),
                                "road_condition": b.get("road_condition"),
                                "expected_share": round(float(exp_share_blocks[k]), 3)})
        crew_out.append({
            "crew_code": c["crew_code"], "planned_qty": round(float(qty.sum()), 1),
            "expected_qty": round(float(crew_done.mean()), 1),
            "low_qty": round(float(np.percentile(crew_done, 10)), 1),
            "high_qty": round(float(np.percentile(crew_done, 90)), 1),
            "expected_share": round(float(crew_done.mean() / qty.sum()), 3) if qty.sum() else None,
        })
        del expected_present
    if not crew_out:
        return None
    at_risk.sort(key=lambda x: -x["p_carried"])
    mean = float(tot.mean())
    return {
        "planned_qty": round(tot_planned, 1),
        "expected_qty": round(mean, 1),
        "low_qty": round(float(np.percentile(tot, 10)), 1),
        "high_qty": round(float(np.percentile(tot, 90)), 1),
        "expected_share": round(mean / tot_planned, 3) if tot_planned else None,
        "low_share": round(float(np.percentile(tot, 10)) / tot_planned, 3) if tot_planned else None,
        "high_share": round(float(np.percentile(tot, 90)) / tot_planned, 3) if tot_planned else None,
        "crews": crew_out, "at_risk": at_risk[:15], "at_risk_count": len(at_risk),
        "rain_source": source,
    }


# ── scoring ────────────────────────────────────────────────────────────────

def _bucket_adherence(T: dict, mask: np.ndarray, by_rain: bool) -> dict:
    """The method this replaces: summed adherence per operation (and rain band)."""
    out = {}
    for o in OPS:
        mo = mask & (T["op"] == o)
        if not mo.any():
            continue
        if not by_rain:
            out[(o, None)] = float(T["actual"][mo].sum() / T["planned"][mo].sum())
            continue
        for label, lo, hi in ops._RAIN_BUCKETS:
            mb = mo & (T["rain"] >= lo) & (T["rain"] < hi)
            if mb.any():
                out[(o, label)] = float(T["actual"][mb].sum() / T["planned"][mb].sum())
        out[(o, None)] = float(T["actual"][mo].sum() / T["planned"][mo].sum())
    return out


def backtest() -> dict:
    with _LOCK:
        if "backtest" in _CACHE:
            return _CACHE["backtest"]
    from gis.models import headcount, rain as rain_model
    T = _table()
    origins = learn.weekly_origins(date(2025, 2, 3), WINDOW_END)
    rec = {"idx": [], "know": [], "know_old": [], "six": [], "six_old": []}
    draws_cache: dict = {}
    for o in origins:
        fit = _fit(o)
        if fit is None:
            continue
        end = min(o + timedelta(days=7), WINDOW_END + timedelta(days=1))
        idx = np.where((T["ord"] >= o.toordinal()) & (T["ord"] < end.toordinal()))[0]
        if not len(idx):
            continue
        prior = T["ord"] < o.toordinal()
        by_rain = _bucket_adherence(T, prior, True)
        flat = _bucket_adherence(T, prior, False)
        # Knowing the weather: recorded rain and turnout.
        e = _expect(fit, T["X"][idx], T["group"][idx])
        rec["know"].append(e["share"])
        rec["know_old"].append(np.array([
            by_rain.get((T["op"][i], ops._bucket(T["rain"][i], ops._RAIN_BUCKETS)),
                        flat.get((T["op"][i], None), 0.8)) for i in idx]))
        # As at six in the morning: the rain forecast, turnout as expected.
        six = np.zeros(len(idx))
        for d in sorted({T["dates"][i] for i in idx}):
            if d not in draws_cache:
                draws_cache[d] = rain_model.draws(d)
            dr = draws_cache[d]
            sel = np.array([T["dates"][i] == d for i in idx])
            ii = idx[sel]
            n, S = len(ii), len(dr)
            rep = lambda a: np.repeat(a, S)
            X = _x_rows(rep(T["activity"][ii]), rep(T["group"][ii]),
                        np.repeat(dr[None, :], n, axis=0).ravel(), rep(T["road"][ii]),
                        np.zeros(n * S), rep(T["block_h"][ii]), rep(T["crew_h"][ii]),
                        rep(T["carried"][ii]), rep(T["ord"][ii]))
            six[sel] = _expect(fit, X, rep(T["group"][ii]))["share"].reshape(n, S).mean(axis=1)
        rec["six"].append(six)
        rec["six_old"].append(np.array([flat.get((T["op"][i], None), 0.8) for i in idx]))
        rec["idx"].append(idx)
    idx = np.concatenate(rec["idx"])
    share, planned, actual = T["share"][idx], T["planned"][idx], T["actual"][idx]
    dates = [T["dates"][i] for i in idx]
    opv = T["op"][idx]

    def score(pred, old, op=None):
        m = np.ones(len(idx), bool) if op is None else (opv == op)
        if not m.any():
            return None
        order_m, order_b = learn.mae(pred[m], share[m]), learn.mae(old[m], share[m])
        daily = defaultdict(lambda: np.zeros(3))
        for k in np.where(m)[0]:
            daily[(opv[k], dates[k])] += (actual[k], planned[k] * pred[k], planned[k] * old[k])
        dm = float(np.mean([abs(v[1] - v[0]) / max(v[0], 1e-9) for v in daily.values() if v[0] > 0]))
        db = float(np.mean([abs(v[2] - v[0]) / max(v[0], 1e-9) for v in daily.values() if v[0] > 0]))
        return {"orders": int(m.sum()),
                "order_error_pts": round(100 * order_m, 1), "old_order_error_pts": round(100 * order_b, 1),
                "order_improvement_pct": learn.improvement_pct(order_m, order_b),
                "daily_total_error_pct": round(100 * dm, 1), "old_daily_total_error_pct": round(100 * db, 1),
                "daily_improvement_pct": learn.improvement_pct(dm, db)}

    know, know_old = np.concatenate(rec["know"]), np.concatenate(rec["know_old"])
    six, six_old = np.concatenate(rec["six"]), np.concatenate(rec["six_old"])
    modes = {
        "knowing_the_weather": {"all": score(know, know_old),
                                "by_operation": {o: score(know, know_old, o) for o in OPS}},
        "six_in_the_morning": {"all": score(six, six_old),
                               "by_operation": {o: score(six, six_old, o) for o in OPS}},
    }
    s, k = modes["six_in_the_morning"]["all"], modes["knowing_the_weather"]["all"]
    out = {
        "available": True, "model": "slippage",
        "method": "logistic (worked at all) and log-linear (share done) regressions per order",
        "weeks": len(rec["idx"]), "orders": int(len(idx)),
        "from": origins[0].isoformat(), "to": WINDOW_END.isoformat(),
        "modes": modes,
        "old_method": {"knowing_the_weather": "the ledger's adherence in the day's rain band, earlier days only",
                       "six_in_the_morning": "the ledger's overall adherence, earlier days only (rain unknown)"},
        "grade": learn.grade(s["order_improvement_pct"]),
        "trained_on": "synthetic",
        "plain": {
            "headline": (f"Checked week by week on {len(idx):,} orders it had not seen. As at six in the "
                         f"morning, its guess of each order's share done was off by {s['order_error_pts']} "
                         f"points on average, against {s['old_order_error_pts']} for the ledger's average."),
            "daily": (f"For a whole operation's day, the expected total was off by "
                      f"{s['daily_total_error_pct']}% against {s['old_daily_total_error_pct']}%."),
            "knowing": (f"Given the rain that actually fell, it was off by {k['order_error_pts']} points against "
                        f"{k['old_order_error_pts']} for the rain-band average: it has learned what drives a "
                        "miss, and most of what is left is not knowing the weather."),
        },
    }
    with _LOCK:
        _CACHE["backtest"] = out
    return out


def recovery() -> dict:
    """What the model found against what the generator planted."""
    with _LOCK:
        if "recovery" in _CACHE:
            return _CACHE["recovery"]
    fit = _fit(WINDOW_END + timedelta(days=1))
    if fit is None:
        return {"available": False}
    b = fit["b_share"]
    T = _table()

    def per_mm(g):
        # Share lost per mm between 10 and 25 mm, from the two lower hinges.
        return math.exp(b[CI[f"{g}_rain_5"]] + b[CI[f"{g}_rain_10"]]) - 1

    # The generator's rules, expressed the same way at 17.5 mm, mid-band.
    planted_h = -0.012 / (1 - 0.012 * 7.5)
    planted_u = -0.010 / (1 - 0.010 * 9.5)
    rows = [
        ("harvest_rain", "Harvest work lost per mm of rain between 10 and 25 mm", planted_h, per_mm("harvest"), 0.004, "per_mm"),
        ("upkeep_rain", "Upkeep work lost per mm of rain between 10 and 25 mm", planted_u, per_mm("upkeep"), 0.004, "per_mm"),
        ("road_fair", "Fair road, dry day", 0.97, math.exp(b[CI["road_fair"]]), 0.04, "ratio"),
        ("road_poor", "Poor road, dry day", 0.91, math.exp(b[CI["road_poor"]]), 0.04, "ratio"),
        ("road_iww", "Road impassable when wet, dry day", 0.86, math.exp(b[CI["road_impassable-when-wet"]]), 0.04, "ratio"),
        ("road_iww_wet", "Road impassable when wet, extra loss at 15 mm+", 0.65,
         math.exp(b[CI["road_impassable-when-wet_wet15"]]), 0.06, "ratio"),
        ("road_poor_wet", "Poor road, extra loss at 25 mm+", 0.85,
         math.exp(b[CI["road_poor_wet25"]] + b[CI["road_poor_wet15"]]), 0.06, "ratio"),
        ("road_fair_wet", "Fair road in rain (nothing planted)", 1.0,
         math.exp(b[CI["road_fair_wet15"]] + b[CI["road_fair_wet25"]]), 0.05, "ratio"),
    ]
    out_rows = []
    iww_orders = int((T["road"] == "impassable-when-wet").sum())
    for key, label, planted, found, tol, kind in rows:
        if key.startswith("road_iww") and iww_orders == 0:
            # The generator has the rule, but this estate's road feed gives no
            # block that condition, so no order ever met it.
            out_rows.append({"effect": key, "label": label, "planted": planted, "found": None,
                             "tolerance": tol, "recovered": None, "planted_something": True,
                             "plain": (f"{label}: the generator has a rule for it, but no block in this "
                                       "estate's road feed is impassable when wet, so there is nothing "
                                       "to find.")})
            continue
        ok = abs(found - planted) <= tol
        if kind == "per_mm":
            plain = (f"{label}: the generator's rule works out at {abs(100 * planted):.1f}% per mm; "
                     f"the model found {abs(100 * found):.1f}%.")
        else:
            plain = (f"{label}: the generator set {round(100 * planted)}% of the work done; the model found "
                     f"{round(100 * found)}%.")
        out_rows.append({"effect": key, "label": label, "planted": round(planted, 4),
                         "found": round(found, 4), "tolerance": tol, "recovered": ok,
                         "planted_something": planted != 1.0, "plain": plain})

    # Crew history: nothing planted beyond turnout.
    sd_c = float(np.std(T["crew_h"]))
    crew_eff = math.exp(b[CI["crew_history"]] * sd_c) - 1
    ok = abs(crew_eff) <= 0.02
    out_rows.append({"effect": "crew_history", "label": "A crew's own track record, once turnout is known",
                     "planted": 0.0, "found": round(crew_eff, 4), "tolerance": 0.02, "recovered": ok,
                     "planted_something": False,
                     "plain": (f"Crew track record: the generator set no effect; one typical step in a crew's "
                               f"record moves the work done by {learn.signed_pct(crew_eff, 1)}, "
                               f"{'so it did not invent one' if ok else 'a small effect that is not there'}.")})

    # Block history against the generator's hidden block-quality field.
    corr = None
    try:
        from gis import ontology
        from gis.build_synthetic import SEED, _latent_field
        feats = ontology.blocks_geojson("EC", synthetic_world=True)["features"]
        latent = _latent_field(feats, random.Random(SEED))
        block_h, _ = _history_at(WINDOW_END + timedelta(days=1))
        st = ops._state()
        xs, ys = [], []
        for (o, k), v in block_h.items():
            if o != "harvest":
                continue
            bid = (st["blocks"].get(k) or {}).get("id")
            if bid in latent:
                xs.append(v)
                ys.append(latent[bid])
        if len(xs) > 20:
            corr = float(np.corrcoef(xs, ys)[0, 1])
    except Exception as exc:
        log.warning("[slippage] latent field unavailable: %s", exc)
    if corr is not None:
        ok = corr >= 0.5
        out_rows.append({"effect": "block_history", "label": "Which blocks persistently fall short",
                         "planted": 1.0, "found": round(corr, 3), "tolerance": None, "recovered": ok,
                         "planted_something": True,
                         "plain": (f"Block track record: the generator gave each block a hidden quality score. "
                                   f"The model's view of each harvest block's record lines up with it at "
                                   f"{corr:.2f} (1.0 would be perfect; the bar is 0.5).")})
    out = {"available": True, "rows": out_rows,
           "all_recovered": all(r["recovered"] is not False for r in out_rows)}
    with _LOCK:
        _CACHE["recovery"] = out
    return out


def effects_plain() -> list[str]:
    """What drives a miss, in words, from the full fit."""
    fit = _fit(WINDOW_END + timedelta(days=1))
    if fit is None:
        return []
    b = fit["b_share"]
    out = []
    for g, name in (("harvest", "harvesting"), ("upkeep", "upkeep work")):
        v = math.exp(b[CI[f"{g}_rain_5"]] + b[CI[f"{g}_rain_10"]]) - 1
        out.append(f"Each mm of rain above 10 mm takes about {abs(100 * v):.1f}% off the {name} done that day.")
    poor = math.exp(b[CI["road_poor"]]) - 1
    poor_wet = math.exp(b[CI["road_poor"]] + b[CI["road_poor_wet15"]] + b[CI["road_poor_wet25"]]) - 1
    out.append(f"On poor roads about {abs(round(100 * poor))}% less gets done on a dry day, and about "
               f"{abs(round(100 * poor_wet))}% less in rain of 25 mm or more.")
    t = b[CI["turnout"]]
    out.append(f"Every 10% fewer people than usual means about "
               f"{abs(round(100 * (math.exp(t * math.log(0.9)) - 1)))}% less work done.")
    bh = float(np.std(_table()["block_h"]))
    out.append(f"Some blocks keep falling short: a block with a poor record typically gets about "
               f"{abs(round(100 * (math.exp(-b[CI['block_history']] * bh) - 1)))}% less done "
               "than an average one.")
    out.append("Spraying is called off when rain reaches 15 mm, and all field work when it reaches 45 mm.")
    return out


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
