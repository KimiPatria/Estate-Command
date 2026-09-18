"""Tomorrow's rain, as a chance a manager can act on.

The only one of the four forecasting models that is real end to end: real
forecasts from the evening before (gis/build_rain_forecast.py) against real
recorded rain (ec_rainfall.json), 2024-02 onward.

The problem it solves
---------------------
The plan used to read the rain that fell on the day. On a replay that is
hindsight: the spray go/no-go was right by construction. For tomorrow it was
"unknown". And last night's forecast, taken at face value, is poor here: it
halves the rain and catches a small share of the wash-off days.

The method: forecast analogues
------------------------------
For tomorrow, find the past days whose forecast looked most like tomorrow's
(the one-day and two-day-ahead amounts, and the time of year), and count how
often it actually rained 15 mm or more on them. That count is the chance.
It is k-nearest-neighbours, and it explains itself: "of the 60 past days with
a forecast most like tomorrow's, 23 had 15 mm or more". A few days of the
month's usual weather are mixed in so a thin neighbourhood cannot say 0% or
100%.

Only days before the one being forecast are ever searched, so a forecast for
24 May 2025 is built from what was known by 23 May 2025.

Scoring
-------
Every day from the fourth month of the series onward is forecast from the days
before it and scored against what fell, beside two baselines: the usual
chance for that month, and the raw forecast taken literally. The number of
neighbours is chosen on days before the app's anchor date only.
"""

import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

import numpy as np

from gis import environment
from gis.build_operations import RAIN_HEAVY_MM, SPRAY_RAIN_MM, TOMORROW
from gis.models import learn

log = logging.getLogger("estate-command.models.rain")

FORECAST_PATH = Path(__file__).parent.parent / "data" / "ec_rain_forecast.json"
_CACHE: dict = {}
_LOCK = Lock()

K_GRID = (20, 40, 60, 100)
CLIM_WEIGHT = 8          # pseudo-days of the month's usual weather mixed in
WARMUP_DAYS = 90         # the first quarter of the series only trains
SEASON_WEIGHT = 0.35     # how much time of year counts beside the forecast


# ── data ───────────────────────────────────────────────────────────────────

def _load() -> dict | None:
    obs = environment.rainfall_by_day("EC") or {}
    if not FORECAST_PATH.exists() or not obs:
        return None
    raw = json.loads(FORECAST_PATH.read_text(encoding="utf-8"))
    fc = {}
    for r in raw.get("days") or []:
        d1, d2 = r.get("day1_mm"), r.get("day2_mm")
        if d1 is None and d2 is None:
            continue
        fc[r["date"]] = (d1 if d1 is not None else d2, d2 if d2 is not None else d1)
    pairs = sorted((k, v[0], v[1], obs[k]) for k, v in fc.items() if obs.get(k) is not None)
    if len(pairs) < WARMUP_DAYS + 30:
        return None
    ords = np.array([date.fromisoformat(p[0]).toordinal() for p in pairs])
    feats = np.array([_feat(p[1], p[2], date.fromisoformat(p[0])) for p in pairs])
    clim_dates = sorted(obs)
    return {
        "fc": fc, "obs": obs, "pairs": pairs, "ords": ords, "feats": feats,
        "y": np.array([p[3] for p in pairs], dtype=float),
        "clim_ords": np.array([date.fromisoformat(k).toordinal() for k in clim_dates]),
        "clim_months": np.array([int(k[5:7]) for k in clim_dates]),
        "clim_mm": np.array([obs[k] for k in clim_dates], dtype=float),
        "source": raw.get("source") or {},
        "window": raw.get("window") or {},
    }


def _state() -> dict | None:
    with _LOCK:
        if "state" not in _CACHE:
            _CACHE["state"] = _load()
        return _CACHE["state"]


def reload() -> None:
    with _LOCK:
        _CACHE.clear()


def _feat(d1: float, d2: float, d: date) -> list:
    doy = 2 * math.pi * d.timetuple().tm_yday / 365.25
    return [math.log1p(max(d1, 0.0)), math.log1p(max(d2, 0.0)),
            SEASON_WEIGHT * math.sin(doy), SEASON_WEIGHT * math.cos(doy)]


def _clim(st: dict, on: date, thresholds: list[float]) -> tuple[np.ndarray, np.ndarray]:
    """The month's usual chance of each threshold, from years before `on`."""
    mask = (st["clim_ords"] < on.toordinal()) & (st["clim_months"] == on.month)
    mm = st["clim_mm"][mask]
    if len(mm) < 20:
        mm = st["clim_mm"][st["clim_ords"] < on.toordinal()]
    p = np.array([(np.sum(mm >= t) + 0.5) / (len(mm) + 1.0) for t in thresholds])
    return p, mm


def _analogues(st: dict, on: date, k: int, thresholds: list[float]) -> dict | None:
    """The k past days whose forecast looked most like the forecast for `on`."""
    f = st["fc"].get(on.isoformat())
    clim_p, clim_mm = _clim(st, on, thresholds)
    if f is None:
        return {"source": "fallback", "p": clim_p, "draws": clim_mm, "clim_p": clim_p,
                "hits": None, "k": len(clim_mm), "forecast": None}
    train = st["ords"] < on.toordinal()
    if train.sum() < 60:
        return {"source": "fallback", "p": clim_p, "draws": clim_mm, "clim_p": clim_p,
                "hits": None, "k": len(clim_mm), "forecast": {"day1_mm": f[0], "day2_mm": f[1]}}
    q = np.array(_feat(f[0], f[1], on))
    F, y = st["feats"][train], st["y"][train]
    dist = np.sqrt(((F - q) ** 2).sum(axis=1))
    idx = np.argsort(dist, kind="stable")[:k]
    near = y[idx]
    hits = np.array([np.sum(near >= t) for t in thresholds])
    p = (hits + CLIM_WEIGHT * clim_p) / (len(near) + CLIM_WEIGHT)
    return {"source": "model", "p": p, "draws": near, "clim_p": clim_p, "hits": hits,
            "k": len(near), "forecast": {"day1_mm": f[0], "day2_mm": f[1]}}


# ── scoring ────────────────────────────────────────────────────────────────

def _reliability(p: np.ndarray, y: np.ndarray) -> list[dict]:
    out = []
    edges = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    for lo, hi in edges:
        m = (p >= lo) & (p < hi)
        if m.sum() == 0:
            continue
        said, fell = float(p[m].mean()), float(y[m].mean())
        out.append({
            "band": f"{int(lo * 100)}-{min(100, int(hi * 100))}%", "days": int(m.sum()),
            "said_pct": round(100 * said), "happened_pct": round(100 * fell),
            "plain": (f"On the {int(m.sum())} days we said about {round(100 * said)}%, "
                      f"it happened on {round(100 * fell)}% of them."),
        })
    return out


def backtest() -> dict:
    with _LOCK:
        if "backtest" in _CACHE:
            return _CACHE["backtest"]
    st = _state()
    if st is None:
        return {"available": False, "reason": "No rain forecast file. Run python gis/build_rain_forecast.py."}
    ths = [SPRAY_RAIN_MM, RAIN_HEAVY_MM]
    first = st["ords"][0] + WARMUP_DAYS
    rows = [p for p in st["pairs"] if date.fromisoformat(p[0]).toordinal() >= first]
    days = [date.fromisoformat(p[0]) for p in rows]
    y = np.array([p[3] for p in rows], dtype=float)
    raw = np.array([p[1] for p in rows], dtype=float)

    preds = {k: np.zeros((len(rows), len(ths))) for k in K_GRID}
    clim = np.zeros((len(rows), len(ths)))
    for i, d in enumerate(days):
        for k in K_GRID:
            a = _analogues(st, d, k, ths)
            preds[k][i] = a["p"]
        clim[i] = a["clim_p"]

    before = np.array([d < TOMORROW for d in days])
    def score(k, mask):
        return float(np.mean([learn.brier(preds[k][mask, j], (y[mask] >= t))
                              for j, t in enumerate(ths)]))
    k_curve = [{"neighbours": k, "brier": round(score(k, before), 4)} for k in K_GRID]
    best_k = min(K_GRID, key=lambda k: score(k, before))
    P = preds[best_k]

    in_ledger = np.array([date(2025, 1, 1) <= d <= date(2025, 5, 23) for d in days])
    per = {}
    for j, t in enumerate(ths):
        hit = y >= t
        bm, bc = learn.brier(P[:, j], hit), learn.brier(clim[:, j], hit)
        br = learn.brier((raw >= t).astype(float), hit)
        imp = learn.improvement_pct(bm, bc)
        said_likely = P[:, j] >= 0.5
        raw_said = raw >= t
        per[str(int(t))] = {
            "threshold_mm": t, "days": len(rows), "days_it_happened": int(hit.sum()),
            "brier": {"model": round(bm, 4), "month_average": round(bc, 4), "raw_forecast": round(br, 4)},
            "improvement_vs_month_average_pct": imp,
            "improvement_vs_raw_forecast_pct": learn.improvement_pct(bm, br),
            "grade": learn.grade(imp),
            "caught_pct": round(100 * float(said_likely[hit].mean()), 1) if hit.any() else None,
            "raw_caught_pct": round(100 * float(raw_said[hit].mean()), 1) if hit.any() else None,
            "right_when_likely_pct": round(100 * float(hit[said_likely].mean()), 1) if said_likely.any() else None,
            "reliability": _reliability(P[:, j], hit.astype(float)),
            "ledger_window": {
                "days": int(in_ledger.sum()),
                "brier_model": learn.safe(learn.brier(P[in_ledger, j], hit[in_ledger]), 4),
                "brier_month_average": learn.safe(learn.brier(clim[in_ledger, j], hit[in_ledger]), 4),
                "days_it_happened": int(hit[in_ledger].sum()),
            },
        }
    w = per[str(int(SPRAY_RAIN_MM))]
    corr = float(np.corrcoef(raw, y)[0, 1])
    out = {
        "available": True,
        "model": "rain", "method": "forecast analogues (k-nearest neighbours)",
        "neighbours": best_k, "neighbour_curve": k_curve,
        "scored_days": len(rows), "from": days[0].isoformat(), "to": days[-1].isoformat(),
        "thresholds": per,
        "raw_forecast": {
            "correlation": round(corr, 2),
            "total_forecast_mm": round(float(raw.sum())), "total_fell_mm": round(float(y.sum())),
        },
        "grade": w["grade"],
        "trained_on": "real",
        "plain": {
            "headline": (f"Checked on {len(rows)} past days. For spraying weather (15 mm or more), "
                         f"these chances were {w['improvement_vs_month_average_pct']}% more accurate than "
                         "using the month's usual rain."),
            "raw": (f"Taken literally, last night's forecast is a weak guide here: over those days it "
                    f"predicted {round(float(raw.sum())):,} mm against {round(float(y.sum())):,} mm that fell, "
                    f"and caught {w['raw_caught_pct']}% of the days with 15 mm or more."),
            "caught": (f"When we said rain of 15 mm or more was likely, it came {w['right_when_likely_pct']}% of "
                       f"the time, and we flagged {w['caught_pct']}% of the days it actually came."),
        },
    }
    with _LOCK:
        _CACHE["backtest"] = out
    return out


# ── the forecast ───────────────────────────────────────────────────────────

def in_ten(p: float) -> str:
    n = int(round(10 * p))
    if n <= 0:
        return "less than 1 in 10"
    if n >= 10:
        return "almost certain"
    return f"about {n} in 10"


def draws(on: str | date, cutoff_mm: float = 45.0) -> np.ndarray:
    """Plausible rain amounts for the day, for simulating the plan."""
    d = date.fromisoformat(on) if isinstance(on, str) else on
    st = _state()
    if st is None:
        return np.zeros(1)
    k = backtest().get("neighbours", 60)
    a = _analogues(st, d, k, [SPRAY_RAIN_MM, RAIN_HEAVY_MM, cutoff_mm])
    return np.asarray(a["draws"], dtype=float)


def forecast(on: str | date, cutoff_mm: float = 45.0) -> dict:
    d = date.fromisoformat(on) if isinstance(on, str) else on
    key = ("fc", d.isoformat(), cutoff_mm)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    st = _state()
    if st is None:
        return {"available": False,
                "reason": "No rain forecast file. Run python gis/build_rain_forecast.py."}
    bt = backtest()
    k = bt.get("neighbours", 60)
    ths = [SPRAY_RAIN_MM, RAIN_HEAVY_MM, cutoff_mm]
    a = _analogues(st, d, k, ths)
    p15, p25, pstop = (float(x) for x in a["p"])
    dr = np.asarray(a["draws"], dtype=float)
    median = float(np.median(dr)) if len(dr) else None
    p90 = float(np.percentile(dr, 90)) if len(dr) else None
    recorded = st["obs"].get(d.isoformat())
    day_label = d.strftime("%A %d %B").replace(" 0", " ")
    fc = a["forecast"]
    model = a["source"] == "model"

    how = (f"We looked at the {a['k']} past days whose weather forecast looked most like the one for "
           f"{day_label}, and counted how often the rain actually reached each amount. "
           f"{int(a['hits'][0])} of those {a['k']} days had 15 mm or more.") if model else (
           f"There is no archived forecast for {day_label}, so these are the usual chances for "
           f"{d.strftime('%B')}, from {a['k']} past {d.strftime('%B')} days.")
    out = {
        "available": True,
        "date": d.isoformat(), "date_label": day_label,
        "source": a["source"],
        "source_label": ("forecast analogues from real forecasts" if model
                         else "the month's usual rain (no forecast for this day)"),
        "forecast_said": fc,
        "chances": {
            "washoff": {"threshold_mm": SPRAY_RAIN_MM, "p": round(p15, 3), "pct": round(100 * p15),
                        "words": learn.chance_words(p15), "in_ten": in_ten(p15),
                        "label": "Rain that washes off spraying",
                        "meaning": (f"If crews spray on {day_label}, the chance that rain of "
                                    f"{SPRAY_RAIN_MM:.0f} mm or more washes the herbicide off is "
                                    f"{round(100 * p15)}% ({in_ten(p15)}).")},
            "heavy": {"threshold_mm": RAIN_HEAVY_MM, "p": round(p25, 3), "pct": round(100 * p25),
                      "words": learn.chance_words(p25), "in_ten": in_ten(p25),
                      "label": "Heavy rain",
                      "meaning": (f"Heavy rain of {RAIN_HEAVY_MM:.0f} mm or more is {learn.chance_words(p25)} "
                                  f"({round(100 * p25)}%). In rain like that crews lose part of the day "
                                  "and wet roads slow the tractors.")},
            "stop": {"threshold_mm": cutoff_mm, "p": round(pstop, 3), "pct": round(100 * pstop),
                     "words": learn.chance_words(pstop), "in_ten": in_ten(pstop),
                     "label": "Rain that stops field work",
                     "meaning": (f"Rain of {cutoff_mm:.0f} mm or more, which stops field work, is "
                                 f"{learn.chance_words(pstop)} ({round(100 * pstop)}%).")},
        },
        "amount": {
            "most_likely_mm": learn.safe(median, 1), "high_mm": learn.safe(p90, 1),
            "plain": (f"Most likely about {median:.0f} mm; 9 days in 10 like this stayed under "
                      f"{p90:.0f} mm.") if median is not None else None,
        },
        "recorded_mm": recorded,
        "recorded_plain": (f"What actually fell that day: {recorded:.0f} mm." if recorded is not None
                           and d < TOMORROW else None),
        "how": how,
        "neighbours": a["k"], "hits_15mm": int(a["hits"][0]) if a["hits"] is not None else None,
        "month_average_pct": {"washoff": round(100 * float(a["clim_p"][0])),
                              "heavy": round(100 * float(a["clim_p"][1]))},
        "grade": bt.get("grade"),
        "headline": (f"{day_label}: a {round(100 * p15)}% chance of {SPRAY_RAIN_MM:.0f} mm of rain or more, "
                     f"enough to wash off spraying. Heavy rain ({RAIN_HEAVY_MM:.0f} mm+) is "
                     f"{learn.chance_words(p25)}."),
        "forecast_plain": ((f"Last night's weather forecast said {fc['day1_mm']:.0f} mm"
                            + (f"; the night before it said {fc['day2_mm']:.0f} mm." if fc.get('day2_mm') is not None else "."))
                           if fc else None),
        "trained_on": "real",
        "provenance": ("predicted · trained on real: Open-Meteo forecasts issued the evening before, "
                       "scored against Open-Meteo recorded rainfall (ERA5 reanalysis)."),
    }
    with _LOCK:
        _CACHE[key] = out
    return out


def spray_call(p_washoff: float, defer_per_day_idr: float, herbicide_idr: float,
               p_usual: float, month_name: str) -> dict:
    """Spray or hold, as a cost decision.

    Spray, and with chance p the rain washes it off: the herbicide is lost and
    the blocks still wait. Hold, and the blocks wait for a drier day, which in
    an ordinary week of this month comes after 1 / (1 - usual chance) days.
    Labour is paid either way, so it is not counted.

        spray when  p x (herbicide + a day's wait)  <  the wait for a drier day
    """
    wait_days = min(5.0, 1.0 / max(1e-6, 1.0 - p_usual))
    hold_cost = defer_per_day_idr * wait_days
    lose_cost = herbicide_idr + defer_per_day_idr
    be = hold_cost / (hold_cost + herbicide_idr) if (hold_cost + herbicide_idr) else 1.0
    go = p_washoff * lose_cost < hold_cost
    m = lambda v: f"{v / 1e6:,.1f}M IDR"
    wait_words = f"about {wait_days:.1f} days" if wait_days >= 1.05 else "about a day"
    reason = ((f"The {round(100 * p_washoff)}% chance of wash-off is below the break-even point of "
               f"{round(100 * be)}%. Holding would leave these blocks waiting for a drier day, usually "
               f"{wait_words} in {month_name}, costing about {m(hold_cost)}; a round lost to rain would "
               f"waste about {m(herbicide_idr)} of herbicide.") if go else
              (f"The {round(100 * p_washoff)}% chance of wash-off is above the break-even point of "
               f"{round(100 * be)}%. A round lost to rain would waste about {m(herbicide_idr)} of herbicide, "
               f"while waiting for a drier day, usually {wait_words} in {month_name}, costs about "
               f"{m(hold_cost)}."))
    return {
        "go": go, "breakeven_p": round(be, 3), "breakeven_pct": round(100 * be),
        "p_washoff_pct": round(100 * p_washoff), "usual_pct": round(100 * p_usual),
        "wait_days": round(wait_days, 1),
        "hold_cost_idr": round(hold_cost), "herbicide_idr": round(herbicide_idr),
        "expected_loss_if_spray_idr": round(p_washoff * lose_cost),
        "verdict": "Spray" if go else "Hold spraying",
        "reason": reason,
        "plain": ((f"Spray. The {round(100 * p_washoff)}% chance of wash-off is below the break-even point of "
                   f"{round(100 * be)}%. Holding would leave these blocks waiting for a drier day, usually "
                   f"{wait_words} in {month_name}, costing about {m(hold_cost)}; a round lost to rain would "
                   f"waste about {m(herbicide_idr)} of herbicide.") if go else
                  (f"Hold spraying. The {round(100 * p_washoff)}% chance of wash-off is above the break-even "
                   f"point of {round(100 * be)}%. A round lost to rain would waste about {m(herbicide_idr)} of "
                   f"herbicide, while waiting for a drier day, usually {wait_words} in {month_name}, costs about "
                   f"{m(hold_cost)}.")),
        "labour_note": "Labour is paid whether the team sprays or not, so it is left out of the comparison.",
    }
