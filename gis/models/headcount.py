"""Who turns up tomorrow: a likely range per crew, not one flat average.

The plan used to take each crew's mean attendance over the last fourteen days
and apply it to the roll. That average lags every step change: the week after
Lebaran it still carries the dip, and it plans a Sunday like a Tuesday.

The model
---------
A binomial logistic regression per crew-day, fitted by IRLS in numpy
(gis/models/learn.py). What moves the chance that one person on the roll
turns up:

    recent turnout     the crew's rate over the last 14 ordinary days
                       (Sundays and holiday leave left out, so a holiday does
                       not drag the following week down)
    day of the week
    Lebaran leave      Idul Fitri from the calendar, 7 days before to 9 after,
                       and the week after it
    payday             the two days after the register's payday. None was
                       planted in the generated feed; it is offered so the
                       test can show the model does not invent an effect
    heavy rain         25 mm or more. Trained on recorded rain; served on the
                       rain model's chance, since at six in the morning
                       nobody has the recorded figure
    harvest gangs      their own Sunday and holiday terms: a gang cannot
                       report fewer men present than it had cutting, so its
                       dips are shallower
    the crew itself    a small pull toward crews that run persistently
                       above or below their recent rate

Output: most likely headcount and an 80% range (1 in 10 below, 1 in 10
above), from the binomial spread widened by the overdispersion the fit
measures, plus the extra spread from not knowing whether it will rain.

What it cannot know
-------------------
The ledger holds one Lebaran. A week-by-week check starting before it has
never seen a holiday, so it cannot learn one; until the training days include
one, the leave effect is the register's holiday_attendance_factor and the
payload says so.

Trained on generated attendance. `recovery()` sets what the model found
against the generator's rules and against what the generated data actually
carries once the generator's own adjustments have acted on them.
"""

import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from threading import Lock

import numpy as np

from gis import assumptions, ops
from gis.build_operations import RAIN_HEAVY_MM, WINDOW_END
from gis.models import learn

log = logging.getLogger("estate-command.models.headcount")

_CACHE: dict = {}
_LOCK = Lock()
MAX_FITS = 24

# Idul Fitri (1 Syawal), from the national calendar.
IDUL_FITRI = {2024: date(2024, 4, 10), 2025: date(2025, 3, 31), 2026: date(2026, 3, 20)}
LEAVE_BEFORE, LEAVE_AFTER = 7, 9
TRAIL_DAYS = 14
Z80 = 1.2816
CREW_LAM = 400.0

PLANTED = {"sunday": 0.62, "leave": 0.72, "heavy_rain": 0.93}
BASE_COLS = ["intercept", "recent", "tue", "wed", "thu", "fri", "sat", "sun",
             "leave", "after_leave", "payday", "heavy_rain", "sun_harvest", "leave_harvest"]
C_SUN, C_LEAVE, C_AFTER, C_PAY, C_HEAVY, C_SUN_H, C_LEAVE_H = 7, 8, 9, 10, 11, 12, 13


def _phase(d: date) -> str | None:
    for y in (d.year - 1, d.year, d.year + 1):
        f = IDUL_FITRI.get(y)
        if not f:
            continue
        if f - timedelta(days=LEAVE_BEFORE) <= d <= f + timedelta(days=LEAVE_AFTER):
            return "leave"
        end = f + timedelta(days=LEAVE_AFTER)
        if end < d <= end + timedelta(days=7):
            return "after"
    return None


def _payday(d: date, day_of_month: int) -> bool:
    dom = min(max(int(day_of_month), 1), 28)
    last = date(d.year, d.month, dom)
    if last > d:
        last = date(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1, dom)
    return 0 <= (d - last).days <= 2


def _logit(p):
    p = min(max(p, 0.3), 0.99)
    return math.log(p / (1 - p))


# ── data ───────────────────────────────────────────────────────────────────

def _data() -> dict:
    with _LOCK:
        if "data" in _CACHE:
            return _CACHE["data"]
    st = ops._state()
    by_crew = {code: {d: r for d, r in series} for code, series in st["att_series"].items()}
    crews = [learn.strip(c) for c in st["crews"]]
    out = {"by_crew": by_crew, "crews": crews, "crew_by": {c["crew_code"]: c for c in crews},
           "codes": sorted(by_crew), "rain": st["rain"]}
    with _LOCK:
        _CACHE["data"] = out
    return out


def recent_rate(code: str, d: date) -> float | None:
    """Turnout over the last 14 ordinary days before `d`: Sundays and holiday
    leave left out. Known the evening before."""
    rows = _data()["by_crew"].get(code) or {}
    pres = roll = 0
    for back in range(1, TRAIL_DAYS + 1):
        x = d - timedelta(days=back)
        if x.weekday() == 6 or _phase(x) == "leave":
            continue
        r = rows.get(x.isoformat())
        if r and r["on_roll"]:
            pres += r["present"]
            roll += r["on_roll"]
    return pres / roll if roll else None


def _x(code: str, d: date, heavy: float, payday_dom: int, crew_idx: dict) -> np.ndarray:
    D = _data()
    rate = recent_rate(code, d)
    wd = d.weekday()
    ph = _phase(d)
    v = np.zeros(len(BASE_COLS) + len(crew_idx))
    v[0] = 1.0
    v[1] = _logit(rate if rate is not None else 0.85) - _logit(0.85)
    if wd >= 1:
        v[1 + wd] = 1.0
    v[C_LEAVE] = 1.0 if ph == "leave" else 0.0
    v[C_AFTER] = 1.0 if ph == "after" else 0.0
    v[C_PAY] = 1.0 if _payday(d, payday_dom) else 0.0
    v[C_HEAVY] = heavy
    if (D["crew_by"].get(code) or {}).get("crew_type") == "harvest":
        v[C_SUN_H] = v[C_SUN]
        v[C_LEAVE_H] = v[C_LEAVE]
    if code in crew_idx:
        v[len(BASE_COLS) + crew_idx[code]] = 1.0
    return v


def _table() -> dict:
    """Every crew-day with its features, built once."""
    with _LOCK:
        if "table" in _CACHE:
            return _CACHE["table"]
    D = _data()
    payday = int(assumptions.get("payday_day_of_month"))
    lookback = int(assumptions.get("attendance_lookback_days"))
    crew_idx = {c: i for i, c in enumerate(D["codes"])}
    X, meta = [], []
    for code in D["codes"]:
        rows = D["by_crew"][code]
        for ds in sorted(rows):
            r = rows[ds]
            if not r["on_roll"]:
                continue
            d = date.fromisoformat(ds)
            heavy = 1.0 if (D["rain"].get(ds) or 0.0) >= RAIN_HEAVY_MM else 0.0
            X.append(_x(code, d, heavy, payday, crew_idx))
            # The old method, for the backtest: mean attendance over the
            # lookback days, every day counted.
            pres = rl = 0
            for back in range(1, lookback + 1):
                b = rows.get((d - timedelta(days=back)).isoformat())
                if b and b["on_roll"]:
                    pres += b["present"]
                    rl += b["on_roll"]
            meta.append((code, d, r["on_roll"], r["present"], pres / rl if rl else 0.85))
    out = {
        "X": np.array(X), "crew_idx": crew_idx,
        "code": np.array([m[0] for m in meta]),
        "ord": np.array([m[1].toordinal() for m in meta]),
        "dates": [m[1] for m in meta],
        "roll": np.array([m[2] for m in meta], dtype=float),
        "present": np.array([m[3] for m in meta], dtype=float),
        "old_rate": np.array([m[4] for m in meta], dtype=float),
        "harvest": np.array([D["crew_by"][m[0]]["crew_type"] == "harvest" for m in meta]),
        "index": {(m[0], m[1].isoformat()): i for i, m in enumerate(meta)},
        "payday": payday, "lookback": lookback,
    }
    with _LOCK:
        _CACHE["table"] = out
    return out


# ── fitting ────────────────────────────────────────────────────────────────

def _fit(cutoff: date) -> dict | None:
    """Fit on every crew-day strictly before `cutoff`."""
    key = ("fit", cutoff.isoformat())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    T = _table()
    mask = T["ord"] < cutoff.toordinal()
    if mask.sum() < 500:
        return None
    X = T["X"][mask].copy()
    y = T["present"][mask] / T["roll"][mask]
    w = T["roll"][mask]
    leave_rows = int(X[:, C_LEAVE].sum())
    learned_leave = leave_rows >= 30
    if not learned_leave:
        X[:, [C_LEAVE, C_AFTER, C_LEAVE_H]] = 0.0
    lam = np.full(X.shape[1], 1e-3)
    lam[len(BASE_COLS):] = CREW_LAM
    beta = learn.logistic(X, y, w, lam)
    p = learn.sigmoid(X @ beta)
    pearson = (w * (y - p) ** 2) / np.maximum(p * (1 - p), 1e-6)
    phi = max(1.0, float(pearson.sum() / max(1, len(y) - len(BASE_COLS))))
    out = {"beta": beta, "phi": phi, "cutoff": cutoff, "rows": int(mask.sum()),
           "learned_leave": learned_leave, "leave_rows": leave_rows}
    with _LOCK:
        _CACHE[key] = out
        fits = [k for k in _CACHE if isinstance(k, tuple) and k[0] == "fit"]
        for k in fits[:-MAX_FITS]:
            _CACHE.pop(k, None)
    return out


def _cutoff_for(d: date) -> date:
    return min(d, WINDOW_END + timedelta(days=1))


def _row_x(code: str, d: date) -> np.ndarray:
    T = _table()
    i = T["index"].get((code, d.isoformat()))
    if i is not None:
        return T["X"][i].copy()
    return _x(code, d, 0.0, T["payday"], T["crew_idx"])


def _spread(fit: dict, x0: np.ndarray, roll: float, d: date, p_heavy: float) -> dict:
    """Most likely, range and rain sensitivity for one crew-day (vectorisable by rows)."""
    beta = fit["beta"]
    x0 = np.atleast_2d(x0).copy()
    x0[:, C_HEAVY] = 0.0
    x1 = x0.copy()
    x1[:, C_HEAVY] = 1.0
    if not fit["learned_leave"]:
        x0[:, [C_LEAVE, C_AFTER, C_LEAVE_H]] = 0.0
        x1[:, [C_LEAVE, C_AFTER, C_LEAVE_H]] = 0.0
    s0, s1 = learn.sigmoid(x0 @ beta), learn.sigmoid(x1 @ beta)
    if not fit["learned_leave"]:
        f = float(assumptions.get("holiday_attendance_factor"))
        leave = np.array([_phase(dd) == "leave" for dd in np.atleast_1d(d)])
        s0 = np.where(leave, s0 * f, s0)
        s1 = np.where(leave, s1 * f, s1)
    p_heavy = np.asarray(p_heavy, dtype=float)
    roll = np.asarray(roll, dtype=float)
    p = (1 - p_heavy) * s0 + p_heavy * s1
    mean = roll * p
    var = fit["phi"] * roll * p * (1 - p) + roll ** 2 * p_heavy * (1 - p_heavy) * (s1 - s0) ** 2
    sd = np.sqrt(np.maximum(var, 0.0))
    lo = np.clip(np.floor(mean - Z80 * sd + 0.5), 0, roll)
    hi = np.clip(np.floor(mean + Z80 * sd + 0.5), 0, roll)
    return {"p": p, "mean": mean, "sd": sd, "low": lo, "high": hi, "s0": s0, "s1": s1, "x": x0}


def _drivers(fit: dict, x: np.ndarray, roll: int, d: date, p_heavy: float,
             s0: float, s1: float, recent: float | None) -> list[str]:
    """Why this day differs from the crew's recent normal, in people."""
    beta = fit["beta"]
    base_p = float(learn.sigmoid(x @ beta))

    def delta(*cols):
        z = x.copy()
        z[list(cols)] = 0.0
        return roll * (base_p - float(learn.sigmoid(z @ beta)))

    out = []
    if recent is not None:
        out.append(f"Recently {round(100 * recent)}% of this crew has turned up on normal working days.")
    wd = d.weekday()
    if wd >= 1:
        dd = delta(C_SUN, C_SUN_H) if wd == 6 else delta(1 + wd)
        if abs(dd) >= 0.5:
            out.append(f"{d.strftime('%A')}: usually about {abs(round(dd))} "
                       f"{'more' if dd > 0 else 'fewer'} than on a Monday.")
    ph = _phase(d)
    if ph == "leave":
        if fit["learned_leave"]:
            out.append(f"Lebaran leave: about {abs(round(delta(C_LEAVE, C_LEAVE_H)))} fewer people.")
        else:
            f = float(assumptions.get("holiday_attendance_factor"))
            out.append(f"Lebaran leave: turnout taken as {round(100 * f)}% of normal (the register's "
                       "figure, because no holiday has been seen yet).")
    elif ph == "after" and fit["learned_leave"] and abs(delta(C_AFTER)) >= 0.5:
        dd = delta(C_AFTER)
        out.append(f"The week after Lebaran: about {abs(round(dd))} {'more' if dd > 0 else 'fewer'} than normal.")
    if p_heavy >= 0.15:
        dd = roll * (s0 - s1) * p_heavy
        if dd >= 0.3:
            out.append(f"Heavy rain is {learn.chance_words(p_heavy)} ({round(100 * p_heavy)}%), "
                       f"which trims about {max(1, round(dd))} off the expected turnout.")
    return out


# ── the forecast ───────────────────────────────────────────────────────────

def _heavy_p(d: date) -> float:
    from gis.models import rain
    if int(assumptions.get("use_rain_model")) != 1:
        return 0.0
    fc = rain.forecast(d)
    return float(fc["chances"]["heavy"]["p"]) if fc.get("available") else 0.0


def predict(on: str | date, codes: list[str] | None = None) -> dict:
    d = date.fromisoformat(on) if isinstance(on, str) else on
    key = ("predict", d.isoformat(), assumptions.fingerprint())
    with _LOCK:
        if key in _CACHE and not codes:
            return _CACHE[key]
    fit = _fit(_cutoff_for(d))
    if fit is None:
        return {"available": False, "reason": "Not enough attendance history to fit."}
    D = _data()
    ph = _heavy_p(d)
    out = {}
    for c in D["crews"]:
        code = c["crew_code"]
        if codes and code not in codes:
            continue
        rec = (D["by_crew"].get(code) or {}).get(d.isoformat())
        roll = (rec or {}).get("on_roll") or c["establishment"]
        if not roll:
            continue
        x = _row_x(code, d)
        one = _spread(fit, x, roll, d, ph)
        lo, hi, mean = int(one["low"][0]), int(one["high"][0]), float(one["mean"][0])
        s0, s1 = float(one["s0"][0]), float(one["s1"][0])
        recent = recent_rate(code, d)
        out[code] = {
            "crew_code": code, "crew_type": c["crew_type"], "division_code": c["division_code"],
            "on_roll": roll, "most_likely": int(round(mean)), "low": lo, "high": hi,
            "expected": round(mean, 2), "sd": round(float(one["sd"][0]), 2),
            "turnout_pct": round(100 * float(one["p"][0]), 1),
            "recent_pct": round(100 * recent, 1) if recent is not None else None,
            "rain_sensitivity": round(roll * (s0 - s1), 2),
            "recorded": rec["present"] if rec and d <= WINDOW_END else None,
            "drivers": _drivers(fit, one["x"][0], roll, d, ph, s0, s1, recent),
            "plain": (f"Expect {lo} to {'all ' if hi == roll else ''}{hi} of {roll} "
                      f"(most likely {int(round(mean))})."),
        }
    res = {"available": True, "date": d.isoformat(), "p_heavy": ph, "crews": out,
           "learned_leave": fit["learned_leave"], "trained_rows": fit["rows"]}
    if not codes:
        with _LOCK:
            _CACHE[key] = res
    return res


def expected_present(code: str, on: date) -> dict | None:
    """What ops.capacity reads when the model is switched on."""
    p = predict(on)
    return (p.get("crews") or {}).get(code) if p.get("available") else None


def present_draws(code: str, on: date, heavy: np.ndarray, rng: np.random.Generator) -> np.ndarray | None:
    """Simulated headcounts for the plan, one per rain draw (heavy = 0 or 1)."""
    fit = _fit(_cutoff_for(on))
    D = _data()
    c = D["crew_by"].get(code)
    if fit is None or not c:
        return None
    rec = (D["by_crew"].get(code) or {}).get(on.isoformat())
    roll = (rec or {}).get("on_roll") or c["establishment"]
    if not roll:
        return None
    one = _spread(fit, _row_x(code, on), roll, on, 0.0)
    s0, s1 = float(one["s0"][0]), float(one["s1"][0])
    p = np.where(heavy > 0, s1, s0)
    sd = np.sqrt(fit["phi"] * roll * p * (1 - p))
    return np.clip(np.round(roll * p + sd * rng.standard_normal(len(heavy))), 0, roll)


def estate(on: str | date, crew_type: str | None = None, codes: list[str] | None = None) -> dict:
    """Totals with a plain headline, for the outlook card and the plan."""
    d = date.fromisoformat(on) if isinstance(on, str) else on
    p = predict(d)
    if not p.get("available"):
        return p
    rows = [r for r in p["crews"].values()
            if (not crew_type or r["crew_type"] == crew_type) and (codes is None or r["crew_code"] in codes)]
    ph = p["p_heavy"]
    mean = sum(r["expected"] for r in rows)
    # Each crew's spread is its own, except the rain: if it pours, it pours on
    # every crew at once, so that part adds up across crews rather than cancelling.
    indep = sum(max(r["sd"] ** 2 - ph * (1 - ph) * r["rain_sensitivity"] ** 2, 0.0) for r in rows)
    shock = sum(r["rain_sensitivity"] for r in rows)
    sd = math.sqrt(indep + ph * (1 - ph) * shock ** 2)
    roll = sum(r["on_roll"] for r in rows)
    lo, hi = int(round(mean - Z80 * sd)), int(round(mean + Z80 * sd))
    rec = [r["recorded"] for r in rows if r["recorded"] is not None]
    return {
        "available": True, "date": d.isoformat(), "crews": len(rows), "on_roll": roll,
        "most_likely": int(round(mean)), "low": lo, "high": hi,
        "turnout_pct": round(100 * mean / roll, 1) if roll else None,
        "recorded": sum(rec) if len(rec) == len(rows) and rows else None,
        "plain": (f"Expect {lo:,} to {hi:,} of {roll:,} people across {len(rows)} crews "
                  f"(most likely {int(round(mean)):,}, {round(100 * mean / roll) if roll else 0}% turnout)."),
        "rows": rows,
    }


# ── scoring ────────────────────────────────────────────────────────────────

def backtest() -> dict:
    with _LOCK:
        if "backtest" in _CACHE:
            return _CACHE["backtest"]
    T = _table()
    origins = learn.weekly_origins(date(2025, 2, 3), WINDOW_END)
    pieces = []
    holiday_folds = 0
    heavy_cache: dict = {}
    for o in origins:
        fit = _fit(o)
        if fit is None:
            continue
        holiday_folds += 0 if fit["learned_leave"] else 1
        end = min(o + timedelta(days=7), WINDOW_END + timedelta(days=1))
        idx = np.where((T["ord"] >= o.toordinal()) & (T["ord"] < end.toordinal()))[0]
        if not len(idx):
            continue
        days = [T["dates"][i] for i in idx]
        for dd in set(days):
            if dd not in heavy_cache:
                heavy_cache[dd] = _heavy_p(dd)
        ph = np.array([heavy_cache[dd] for dd in days])
        sp = _spread(fit, T["X"][idx], T["roll"][idx], days, ph)
        pieces.append((idx, sp["mean"], sp["low"], sp["high"]))
    if not pieces:
        return {"available": False, "reason": "No held-out weeks."}
    idx = np.concatenate([p[0] for p in pieces])
    model = np.concatenate([p[1] for p in pieces])
    low = np.concatenate([p[2] for p in pieces])
    high = np.concatenate([p[3] for p in pieces])
    actual = T["present"][idx]
    old = T["old_rate"][idx] * T["roll"][idx]
    dates = [T["dates"][i] for i in idx]
    phase = np.array([_phase(d) or "" for d in dates])
    sunday = np.array([d.weekday() == 6 for d in dates])

    def errs(mask):
        if not mask.any():
            return None
        m, b = learn.mae(model[mask], actual[mask]), learn.mae(old[mask], actual[mask])
        return {"crew_days": int(mask.sum()), "model_error": round(m, 2), "old_error": round(b, 2),
                "improvement_pct": learn.improvement_pct(m, b)}

    segments = {
        "all": errs(np.ones(len(idx), bool)),
        "sundays": errs(sunday & (phase == "")),
        "lebaran": errs(phase == "leave"),
        "after_lebaran": errs(phase == "after"),
        "ordinary": errs(~sunday & (phase == "")),
    }
    by_day = defaultdict(lambda: np.zeros(3))
    for d, a, m, b in zip(dates, actual, model, old):
        by_day[d] += (a, m, b)
    day_m = float(np.mean([abs(v[1] - v[0]) for v in by_day.values()]))
    day_b = float(np.mean([abs(v[2] - v[0]) for v in by_day.values()]))
    cover = float(np.mean((low <= actual) & (actual <= high)))
    a = segments["all"]
    lb = T["lookback"]
    s, lv, af = segments["sundays"], segments["lebaran"], segments["after_lebaran"]
    out = {
        "available": True, "model": "headcount",
        "method": "binomial logistic regression, crew-day",
        "weeks": len(pieces), "crew_days": int(len(idx)),
        "from": origins[0].isoformat(), "to": WINDOW_END.isoformat(),
        "segments": segments,
        "estate_daily": {"model_error": round(day_m, 1), "old_error": round(day_b, 1),
                         "improvement_pct": learn.improvement_pct(day_m, day_b)},
        "range_coverage_pct": round(100 * cover, 1),
        "weeks_without_a_holiday_to_learn_from": holiday_folds,
        "old_method": f"the crew's mean attendance over the last {lb} days, applied to the roll",
        "grade": learn.grade(a["improvement_pct"]),
        "trained_on": "synthetic",
        "plain": {
            "headline": (f"Checked week by week on {len(idx):,} crew-days it had not seen: off by "
                         f"{a['model_error']} people per crew per day on average, against "
                         f"{a['old_error']} for the old {lb}-day average."),
            "sundays": f"On Sundays: off by {s['model_error']} against {s['old_error']}." if s else None,
            "lebaran": (f"Over Lebaran leave: off by {lv['model_error']} against {lv['old_error']}; "
                        f"the week after: {af['model_error']} against {af['old_error']}.") if lv and af else None,
            "range": (f"The likely range held the real headcount on {round(100 * cover)}% of crew-days "
                      "(it is built to hold it on about 8 in 10)."),
            "estate": (f"For the whole estate's daily total: off by {round(day_m)} people against "
                       f"{round(day_b)}."),
            "holiday": (f"{holiday_folds} of the {len(pieces)} weeks were forecast before any Lebaran was in "
                        "the training days, so those used the register's holiday figure."),
        },
    }
    with _LOCK:
        _CACHE["backtest"] = out
    return out


def recovery() -> dict:
    """What the model found against what the generator planted.

    Three figures per effect, all as turnout on the affected days over
    turnout on comparable ordinary days: the generator's rule; what the
    generated data actually carries once the generator's own adjustments have
    acted (a gang floor, monthly totals forced to the labour feed), measured
    against the same crew's ordinary days in the same week or month; and what
    the model found, as its prediction on those same days with the effect over
    its prediction with the effect removed. The model is judged against the
    second figure.
    """
    with _LOCK:
        if "recovery" in _CACHE:
            return _CACHE["recovery"]
    T = _table()
    fit = _fit(WINDOW_END + timedelta(days=1))
    if fit is None:
        return {"available": False}
    beta = fit["beta"]
    rain = _data()["rain"]
    dates = T["dates"]
    n = len(dates)
    wd = np.array([d.weekday() for d in dates])
    phase = np.array([_phase(d) or "" for d in dates])
    heavy = np.array([(rain.get(d.isoformat()) or 0.0) >= RAIN_HEAVY_MM for d in dates])
    pay = T["X"][:, C_PAY] > 0
    week = np.array([d.isocalendar()[:2] for d in dates])
    wkey = np.array([f"{c}|{w[0]}-{w[1]}" for c, w in zip(T["code"], week)])
    mkey = np.array([f"{c}|{d:%Y-%m}" for c, d in zip(T["code"], dates)])
    ordinary = (wd != 6) & (phase == "")
    rate = T["present"] / T["roll"]

    def data_shows(sel, comp, group):
        """Turnout on `sel` days over the same crew's `comp` days in the same group."""
        num = den = 0.0
        comp_rate = defaultdict(lambda: [0.0, 0.0])
        for i in np.where(comp)[0]:
            comp_rate[group[i]][0] += T["present"][i]
            comp_rate[group[i]][1] += T["roll"][i]
        for i in np.where(sel)[0]:
            c = comp_rate.get(group[i])
            if c and c[1]:
                num += T["present"][i]
                den += T["roll"][i] * c[0] / c[1]
        return num / den if den else None

    def found(sel, cols):
        X = T["X"][sel]
        with_ = (T["roll"][sel] * learn.sigmoid(X @ beta)).sum()
        Z = X.copy()
        Z[:, cols] = 0.0
        without = (T["roll"][sel] * learn.sigmoid(Z @ beta)).sum()
        return float(with_ / without) if without else None

    hv, oth = T["harvest"], ~T["harvest"]
    specs = [
        ("sunday", "Sunday turnout, crews other than harvest", PLANTED["sunday"],
         oth & (wd == 6) & (phase == ""), oth & ordinary, wkey, [C_SUN, C_SUN_H]),
        ("sunday_harvest", "Sunday turnout, harvest gangs", PLANTED["sunday"],
         hv & (wd == 6) & (phase == ""), hv & ordinary, wkey, [C_SUN, C_SUN_H]),
        ("leave", "Lebaran leave turnout, crews other than harvest", PLANTED["leave"],
         oth & (wd != 6) & (phase == "leave"), oth & ordinary, mkey, [C_LEAVE, C_LEAVE_H]),
        ("leave_harvest", "Lebaran leave turnout, harvest gangs", PLANTED["leave"],
         hv & (wd != 6) & (phase == "leave"), hv & ordinary, mkey, [C_LEAVE, C_LEAVE_H]),
        ("heavy_rain", "Turnout on heavy-rain days", PLANTED["heavy_rain"],
         ordinary & heavy, ordinary & ~heavy, wkey, [C_HEAVY]),
        ("payday", "Turnout in the two days after payday", None,
         ordinary & pay, ordinary & ~pay, wkey, [C_PAY]),
    ]
    rows = []
    tol = 0.04
    for key, label, rule, sel, comp, group, cols in specs:
        shows = data_shows(sel, comp, group)
        got = found(sel, cols)
        target = shows if shows is not None else (rule or 1.0)
        ok = bool(got is not None and abs(got - target) <= tol)
        if rule is None:
            plain = (f"{label}: the generator set no effect. The data shows {round(100 * target)}% of a "
                     f"normal day and the model found {round(100 * got)}%, "
                     f"{'so it did not invent one' if ok else 'a small effect that is not there'}.")
        else:
            plain = (f"{label}: the generator's rule was {round(100 * rule)}% of a normal day; after its "
                     f"own adjustments the data shows {round(100 * target)}%. The model found "
                     f"{round(100 * got)}%.")
        rows.append({"effect": key, "label": label, "rule": rule, "data_shows": learn.safe(shows),
                     "found": learn.safe(got), "tolerance": tol, "recovered": ok,
                     "planted_something": rule is not None, "days": int(sel.sum()), "plain": plain})
    out = {"available": True, "rows": rows, "all_recovered": all(r["recovered"] for r in rows),
           "fitted_rows": fit["rows"]}
    with _LOCK:
        _CACHE["recovery"] = out
    return out


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
