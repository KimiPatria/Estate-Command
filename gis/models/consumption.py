"""How much of each material goes out between now and when an order arrives.

SAP sets a reorder point from average use: the trailing thirteen weeks, times
the lead time. That is right for nothing on this estate. Fertiliser goes out in
two rounds a year, spraying follows the dry weather, and parts follow the
trucks.

Three kinds of use, one formula each
------------------------------------
    programme   fertiliser    open reservations x the share usually issued,
                              placed in time by how late past rounds went out
    seasonal    herbicide,    the last eight weeks' daily use, taken out of its
                diesel        season and put back into the coming one
    jobs        parts, pest   a rate per day (parts: per tonne hauled), from the
                chemicals     last 26 weeks, pooled toward the other parts

Every figure reads only what was on the books the evening before. A round
still being issued is not used to learn how late rounds go out: its latest
issues have not happened yet, and learning from it would make every round look
early. Only rounds more than 120 days past their target date teach timing.

The spread is the model's own past errors over the same horizon, not an
assumed distribution. Jobs are counted, so their spread is the count's own.

Tested week by week against SAP's trailing average (`backtest`), and against
the handling and storage losses the generator planted (`recovery`).
"""

import csv
import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from threading import Lock

import numpy as np

from gis.build_operations import TOMORROW, WINDOW_END
from gis.models import learn, mm

log = logging.getLogger("estate-command.models.consumption")

_CACHE: dict = {}
_LOCK = Lock()

TRAIL_DAYS = 56
BASELINE_DAYS = 91
JOBS_DAYS = 182
MATURE_ROUND_DAYS = 120
RESERVATION_VISIBLE_DAYS = 150
PARTS_PRIOR_TONNES = 10_000.0
SEASON_PRIOR_DAYS = 30.0
BACKTEST_FROM = date(2024, 6, 3)
SEASONAL = ("AC-001", "AC-002", "FU-001")
KIND_WORDS = {"programme": "the fertiliser programme", "seasonal": "recent use, adjusted for the season",
              "jobs": "how often the job comes up"}


def kind(matnr: str) -> str:
    m = mm.state()["materials"][matnr]
    if m["matkl"] == "FERT":
        return "programme"
    if matnr in SEASONAL:
        return "seasonal"
    return "jobs"


def horizon(matnr: str) -> int:
    """The window use is scored on: the quoted lead time plus a week."""
    return mm.state()["materials"][matnr]["plifz"] + 7


# ── drivers ────────────────────────────────────────────────────────────────

def _read(name):
    with (mm._DIR / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(ln for ln in fh if not ln.startswith("#")))


def tonnes() -> dict:
    """Harvest tonnes per day: history spread over each month, and the forward forecast after it."""
    with _LOCK:
        if "tonnes" in _CACHE:
            return _CACHE["tonnes"]
    abw = {(r["division_code"], r["block_code"]): float(r["abw_kg"]) for r in _read("ec_abw.csv")}
    mean_abw = sum(abw.values()) / len(abw)
    month_t: dict = defaultdict(float)
    for r in _read("ec_harvest_history.csv"):
        month_t[r["month"]] += int(r["bunches"]) * abw.get((r["division_code"], r["block_code"]), mean_abw) / 1000
    fwd: dict = defaultdict(float)
    for r in _read("ec_forecast_forward.csv"):
        fwd[r["month"]] += float(r["p50"]) * abw.get((r["division_code"], r["block_code"]), mean_abw) / 1000
    start = date(2022, 6, 1)
    end = date(2025, 11, 30)
    n = (end - start).days + 1
    daily = np.zeros(n)
    d = start
    while d <= end:
        key = d.strftime("%Y-%m")
        y, mo = d.year, d.month
        dim = (date(y + (mo == 12), mo % 12 + 1, 1) - date(y, mo, 1)).days
        if key in month_t:
            # The export's last month stops on the 23rd; its remaining days carry the
            # month's daily rate until the forward forecast takes over in June.
            days = WINDOW_END.day if key == WINDOW_END.strftime("%Y-%m") else dim
            daily[(d - start).days] = month_t[key] / days
        if key in fwd and d > WINDOW_END:
            daily[(d - start).days] = fwd[key] / dim
        d += timedelta(days=1)
    out = {"start": start, "daily": daily}
    with _LOCK:
        _CACHE["tonnes"] = out
    return out


def _t_index(d: date) -> int:
    return (d - tonnes()["start"]).days


def tonnes_expected(d: date, h: int) -> np.ndarray:
    """Tonnes per day for the next h days, known on d: last year's same days, scaled by the last 90 days."""
    t = tonnes()["daily"]
    i = _t_index(d)
    idx = np.clip(i - 364 + np.arange(h), 0, len(t) - 1)
    last_year = t[idx]
    recent = t[i - 90:i].sum()
    recent_ly = t[i - 90 - 364:i - 364].sum()
    scale = recent / recent_ly if recent_ly > 0 else 1.0
    return last_year * min(max(scale, 0.5), 2.0)


_MONTH_BASE = date(2022, 1, 1)
_MONTHS = np.array([(_MONTH_BASE + timedelta(days=k)).month for k in range(365 * 6)])


def _months(d: date, h: int, back: int = 0) -> np.ndarray:
    """Calendar month of each day from d - back to d + h - 1."""
    i = (d - _MONTH_BASE).days
    return _MONTHS[i - back:i + h]


# ── the three kinds ────────────────────────────────────────────────────────

def _programme_params(d: date) -> dict:
    key = ("prog", d.toordinal())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    st = mm.state()
    lags, got, due = [], 0.0, 0.0
    for r in st["reservations"]:
        if (d - r["bdter"]).days < MATURE_ROUND_DAYS:
            continue
        if r["issued_on"] is not None and r["issued_on"] < d:
            lags.append(max(0, (r["issued_on"] - r["bdter"]).days))
        got += r["enmng"] if r["issued_on"] is not None and r["issued_on"] < d else 0.0
        due += r["bdmng"]
    if not lags:
        out = None
    else:
        lags = np.array(lags)
        pmf = np.bincount(lags, minlength=lags.max() + 1).astype(float)
        pmf /= pmf.sum()
        surv = pmf[::-1].cumsum()[::-1]           # P(lag >= j)
        out = {"pmf": pmf, "surv": surv, "fulfil": got / due if due else 0.9, "rounds_seen": len(lags)}
    with _LOCK:
        _CACHE[key] = out
    return out


def round_spread(matnr: str, d: date) -> np.ndarray:
    """How far each finished round's issued total strayed from its programme, over the average.

    For a programme material this is the spread of use that matters. Errors on fixed
    weekly windows mostly measure when in the window a round happened to land, which
    the issue-lag distribution already carries; applied to a whole round they make it
    look half as big again as its programme.
    """
    key = ("rounds", matnr, d.toordinal())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    by_round: dict = defaultdict(lambda: [0.0, 0.0])
    for r in mm.state()["reservations"]:
        if r["matnr"] != matnr or (d - r["bdter"]).days < MATURE_ROUND_DAYS:
            continue
        by_round[r["window"]][0] += r["enmng"]
        by_round[r["window"]][1] += r["bdmng"]
    shares = np.array([g / b for g, b in by_round.values() if b > 0])
    out = shares / shares.mean() if len(shares) else np.ones(1)
    with _LOCK:
        _CACHE[key] = out
    return out


def _reservation_arrays(matnr: str) -> dict:
    key = ("res", matnr)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    rows = [r for r in mm.state()["reservations"] if r["matnr"] == matnr and not (r["kzear"] and r["issued_on"] is None)]
    out = {"bdter": np.array([r["bdter"].toordinal() for r in rows]),
           "bdmng": np.array([r["bdmng"] for r in rows]),
           "issued": np.array([r["issued_on"].toordinal() if r["issued_on"] else 10 ** 9 for r in rows])}
    with _LOCK:
        _CACHE[key] = out
    return out


def _programme(matnr: str, d: date, h: int) -> np.ndarray:
    p = _programme_params(d)
    out = np.zeros(h)
    if p is None:
        return out
    a = _reservation_arrays(matnr)
    o = d.toordinal()
    sel = (a["bdter"] - o <= RESERVATION_VISIBLE_DAYS) & (a["issued"] >= o)
    open_by_date: dict = defaultdict(float)
    for bd, qty in zip(a["bdter"][sel], a["bdmng"][sel]):
        open_by_date[int(bd)] += float(qty)
    pmf, surv = p["pmf"], p["surv"]
    for bdter, qty in open_by_date.items():
        e = o - bdter
        s = surv[e] if 0 <= e < len(surv) else (1.0 if e < 0 else 0.0)
        if s <= 1e-9:
            out[:min(h, 14)] += qty * p["fulfil"] / min(h, 14)     # past every lag seen: expect it soon
            continue
        idx = e + np.arange(h)
        prob = np.where((idx >= 0) & (idx < len(pmf)), pmf[np.clip(idx, 0, len(pmf) - 1)], 0.0) / s
        out += qty * p["fulfil"] * prob
    return out


def _season_index(matnr: str, d: date) -> np.ndarray | None:
    key = ("season", matnr, d.toordinal())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    use = mm.state()["use"][matnr]
    end = min(mm.day(d), mm.N_DAYS)
    if end < 300:
        out = None
    else:
        months = _months(mm.MM_START, end)
        overall = use[:end].mean()
        idx = np.ones(13)
        for mo in range(1, 13):
            sel = months == mo
            n = sel.sum()
            if n and overall > 0:
                raw = use[:end][sel].mean() / overall
                idx[mo] = (n * raw + SEASON_PRIOR_DAYS) / (n + SEASON_PRIOR_DAYS)
        out = idx
    with _LOCK:
        _CACHE[key] = out
    return out


def _seasonal(matnr: str, d: date, h: int) -> np.ndarray:
    use = mm.state()["use"][matnr]
    end = mm.day(d)
    trail = use[max(0, end - TRAIL_DAYS):end]
    base = trail.mean() if len(trail) else 0.0
    idx = _season_index(matnr, d)
    if idx is None:
        return np.full(h, base)
    trail_idx = float(idx[_months(d, 0, len(trail))].mean()) if len(trail) else 1.0
    return base / trail_idx * idx[_months(d, h)]


def _parts_rates(d: date) -> dict:
    key = ("parts", d.toordinal())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    st = mm.state()
    end = mm.day(d)
    t = tonnes()["daily"]
    ti = _t_index(d)
    exposure = float(t[ti - JOBS_DAYS:ti].sum())
    counts = {m: float(st["use"][m][max(0, end - JOBS_DAYS):end].sum())
              for m, v in st["materials"].items() if v["matkl"] == "SPARE"}
    pooled = (sum(counts.values()) / len(counts)) / exposure if exposure else 0.0
    out = {m: (c + PARTS_PRIOR_TONNES * pooled) / (exposure + PARTS_PRIOR_TONNES) for m, c in counts.items()}
    with _LOCK:
        _CACHE[key] = out
    return out


def _jobs(matnr: str, d: date, h: int) -> np.ndarray:
    st = mm.state()
    if st["materials"][matnr]["matkl"] == "SPARE":
        return _parts_rates(d)[matnr] * tonnes_expected(d, h)
    end = mm.day(d)
    return np.full(h, st["use"][matnr][max(0, end - JOBS_DAYS):end].mean())


def expected_daily(matnr: str, d: date, h: int) -> np.ndarray:
    """Expected use on each of the h days from d, from what was known before d."""
    k = kind(matnr)
    if k == "programme":
        return _programme(matnr, d, h)
    if k == "seasonal":
        return _seasonal(matnr, d, h)
    return _jobs(matnr, d, h)


def job_size(matnr: str, d: date) -> float:
    use = mm.state()["use"][matnr][:mm.day(d)]
    nz = use[use > 0]
    return float(nz.mean()) if len(nz) else 1.0


# ── scoring ────────────────────────────────────────────────────────────────

def _croston_sba(weekly: np.ndarray, alpha: float = 0.1) -> float:
    """Croston with the Syntetos-Boylan correction: demand per week for intermittent use."""
    z = p = None
    q = 1
    for x in weekly:
        if x > 0:
            z = x if z is None else z + alpha * (x - z)
            p = q if p is None else p + alpha * (q - p)
            q = 1
        else:
            q += 1
    if z is None:
        return 0.0
    return (1 - alpha / 2) * z / p


def _origins(matnr: str) -> list[date]:
    h = horizon(matnr)
    return learn.weekly_origins(BACKTEST_FROM, WINDOW_END - timedelta(days=h - 1))


def _run() -> dict:
    with _LOCK:
        if "run" in _CACHE:
            return _CACHE["run"]
    st = mm.state()
    per = {}
    for matnr, m in st["materials"].items():
        use = st["use"][matnr]
        h = horizon(matnr)
        rows = []
        for o in _origins(matnr):
            i = mm.day(o)
            actual = float(use[i:i + h].sum())
            model = float(expected_daily(matnr, o, h).sum())
            base = float(use[max(0, i - BASELINE_DAYS):i].mean() * h)
            row = {"origin": o, "actual": actual, "model": model, "old": base}
            if m["matkl"] == "SPARE":
                weeks = use[:i][-(i // 7) * 7:].reshape(-1, 7).sum(axis=1)
                row["croston"] = _croston_sba(weeks) * h / 7
            rows.append(row)
        per[matnr] = rows
    with _LOCK:
        _CACHE["run"] = per
    return per


def ratio_errors(matnr: str, before: date) -> np.ndarray:
    """Actual over forecast, on past windows that had closed by `before`. The spread of use."""
    rows = _run()[matnr]
    h = horizon(matnr)
    scale = max(np.mean([r["actual"] for r in rows]) if rows else 1.0, 1e-6)
    s = 0.05 * scale
    out = [(r["actual"] + s) / (r["model"] + s) for r in rows
           if r["origin"] + timedelta(days=h) <= before and r["model"] >= 0.2 * scale]
    return np.array(out)


def backtest() -> dict:
    with _LOCK:
        if "backtest" in _CACHE:
            return _CACHE["backtest"]
    if not mm.available():
        return {"available": False, "reason": "No MM records."}
    st = mm.state()
    per, num, den = {}, 0.0, 0.0
    for matnr, rows in _run().items():
        if not rows:
            continue
        m = st["materials"][matnr]
        scale = float(np.mean([r["actual"] for r in rows])) or 1.0
        em = float(np.mean([abs(r["actual"] - r["model"]) for r in rows]))
        eb = float(np.mean([abs(r["actual"] - r["old"]) for r in rows]))
        imp = learn.improvement_pct(em, eb)
        num += em / scale
        den += eb / scale
        entry = {"matnr": matnr, "maktx": m["maktx"], "kind": kind(matnr), "horizon_days": horizon(matnr),
                 "windows": len(rows), "unit": m["meins"], "mean_use": round(scale, 1),
                 "model_error": round(em, 1), "old_error": round(eb, 1),
                 "model_error_pct": round(100 * em / scale, 1), "old_error_pct": round(100 * eb / scale, 1),
                 "improvement_pct": imp, "grade": learn.grade(imp)}
        if m["matkl"] == "SPARE":
            ec = float(np.mean([abs(r["actual"] - r["croston"]) for r in rows]))
            entry["croston_error_pct"] = round(100 * ec / scale, 1)
        entry["plain"] = (f"{m['maktx']}: use over the next {horizon(matnr)} days was off by "
                          f"{entry['model_error_pct']}% on average, against {entry['old_error_pct']}% "
                          f"for SAP's trailing average ({len(rows)} weekly checks).")
        per[matnr] = entry
    overall = learn.improvement_pct(num, den)
    by_kind = {}
    for k in ("programme", "seasonal", "jobs"):
        es = [e for e in per.values() if e["kind"] == k]
        if es:
            by_kind[k] = learn.improvement_pct(sum(e["model_error_pct"] for e in es),
                                               sum(e["old_error_pct"] for e in es))
    out = {
        "available": True, "model": "consumption", "trained_on": "synthetic",
        "method": "programme reservations x issue lag; seasonal trailing use; job rates per day or per tonne",
        "materials": per, "improvement_pct": overall, "grade": learn.grade(overall), "by_kind": by_kind,
        "plain": {
            "headline": (f"Checked week by week from {BACKTEST_FROM.strftime('%d %B %Y').lstrip('0')} on "
                         f"{len(per)} materials: use over each material's lead time was forecast "
                         f"{overall}% closer than SAP's trailing 13-week average."),
            "by_kind": "; ".join(f"{KIND_WORDS[k]}: {v}% closer" for k, v in by_kind.items()) + ".",
        },
    }
    with _LOCK:
        _CACHE["backtest"] = out
    return out


def recovery() -> dict:
    """Handling and storage losses the generator planted, against what the ledger shows."""
    with _LOCK:
        if "recovery" in _CACHE:
            return _CACHE["recovery"]
    from gis import build_materials as bm
    st = mm.state()
    ha = {r["order_id"]: float(r["actual_qty"] or 0) for r in _read("ec_upkeep_orders.csv")
          if r["activity"] == "spraying"}
    rows = []
    for matnr, dose in (("AC-001", bm.GLYPHOSATE_L_PER_HA), ("AC-002", bm.METSULFURON_KG_PER_HA)):
        issued, book = 0.0, 0.0
        for mv in st["moves"]:
            if mv["matnr"] == matnr and mv["aufnr"] in ha:
                issued += mv["menge"]
                book += ha[mv["aufnr"]] * dose
        found = issued / book if book else None
        ok = found is not None and abs(found / bm.HANDLING_LOSS - 1) <= 0.04
        name = st["materials"][matnr]["maktx"]
        rows.append({"effect": f"handling_loss_{matnr}", "label": f"{name}: issued over the dose",
                     "planted": bm.HANDLING_LOSS, "found": round(found, 3) if found else None,
                     "bar": "within 4%", "recovered": ok, "planted_something": True,
                     "plain": (f"The generator issued {bm.HANDLING_LOSS:.2f} times the dose for every hectare sprayed. "
                               f"Joining the store's 2025 issues to the spraying orders gives {found:.2f}.")})
    for matnr, m in st["materials"].items():
        planted = bm.STORAGE_LOSS.get(matnr)
        r = mm.storage_loss_rate(matnr)
        if planted:
            ok = abs(r["rate_per_month"] - planted) <= 0.0015
            rows.append({"effect": f"storage_loss_{matnr}", "label": f"{m['maktx']}: lost in storage",
                         "planted": planted, "found": round(r["rate_per_month"], 4), "bar": "within 0.15 points a month",
                         "recovered": ok, "planted_something": True,
                         "plain": (f"The generator lost {planted:.1%} of {m['maktx']} stock a month in storage. "
                                   f"{r['counts']} counts scrapped {r['scrapped']:,.0f} {m['meins'].lower()}, "
                                   f"which is {r['rate_per_month']:.2%} of the stock held a month.")})
    others = [mm.storage_loss_rate(x)["rate_per_month"] for x in st["materials"] if x not in bm.STORAGE_LOSS]
    rows.append({"effect": "storage_loss_elsewhere", "label": "Storage loss on everything else",
                 "planted": 0.0, "found": round(max(others), 4), "bar": "none",
                 "recovered": max(others) <= 0.0005, "planted_something": False,
                 "plain": "Nothing else was given a storage loss, and no count scrapped any."})
    out = {"available": True, "rows": rows, "all_recovered": all(x["recovered"] for x in rows)}
    with _LOCK:
        _CACHE["recovery"] = out
    return out


# ── for the screen ─────────────────────────────────────────────────────────

def use_per_unit() -> dict:
    """Issued per hectare, palm and tonne in 2025, against the register's book dose."""
    from gis import assumptions
    st = mm.state()
    av = assumptions.values()
    ha = {r["order_id"]: float(r["actual_qty"] or 0) for r in _read("ec_upkeep_orders.csv")
          if r["activity"] == "spraying"}
    palms = {r["order_id"]: float(r["actual_qty"] or 0) for r in _read("ec_pest_orders.csv")}
    sums = defaultdict(lambda: [0.0, 0.0])
    for mv in st["moves"]:
        if mv["aufnr"] in ha:
            sums[mv["matnr"]][0] += mv["menge"]
            sums[mv["matnr"]][1] += ha[mv["aufnr"]]
        elif mv["aufnr"] in palms:
            sums[mv["matnr"]][0] += mv["menge"]
            sums[mv["matnr"]][1] += palms[mv["aufnr"]]
    book = {"AC-001": ("glyphosate_l_per_ha", "L per ha sprayed"), "AC-002": ("metsulfuron_kg_per_ha", "kg per ha sprayed"),
            "AC-003": ("hexaconazole_l_per_palm", "L per palm treated"), "AC-004": ("bait_kg_per_palm", "kg per palm baited")}
    out = []
    for matnr, (key, unit) in book.items():
        q, base = sums.get(matnr, [0.0, 0.0])
        if not base:
            continue
        per = q / base
        b = float(av[key])
        out.append({"matnr": matnr, "maktx": st["materials"][matnr]["maktx"], "unit": unit,
                    "issued_per_unit": round(per, 4), "book": b, "ratio": round(per / b, 3),
                    "plain": (f"{st['materials'][matnr]['maktx']}: {per:.3g} {unit} issued in 2025 against a book dose "
                              f"of {b:.3g}, {abs(per / b - 1):.0%} {'more' if per >= b else 'less'}.")})
    return {"rows": out}


def diesel_check(on: date | None = None, days: int = 30) -> dict:
    """The forward harvest forecast's diesel, beside the store model's."""
    on = on or TOMORROW
    st = mm.state()
    haul = sum(mv["menge"] for mv in st["moves"] if mv["matnr"] == "FU-001" and mv["bwart"] == "201"
               and mv["kostl"] != "EC-GEN" and mv["budat"] >= "2025-01-01")
    t = tonnes()["daily"]
    t25 = float(t[_t_index(date(2025, 1, 1)):_t_index(WINDOW_END) + 1].sum())
    gen = [mv["menge"] for mv in st["moves"] if mv["matnr"] == "FU-001" and mv["kostl"] == "EC-GEN"]
    per_t = haul / t25 if t25 else 0.0
    fwd_t = float(t[_t_index(on):_t_index(on) + days].sum())
    from_forecast = fwd_t * per_t + (float(np.mean(gen)) if gen else 0.0) * days
    model = float(expected_daily("FU-001", on, days).sum())
    return {"days": days, "tonnes": round(fwd_t), "litres_per_tonne": round(per_t, 2),
            "from_harvest_forecast_l": round(from_forecast), "store_model_l": round(model),
            "plain": (f"The forward harvest forecast ({fwd_t:,.0f} t over {days} days at {per_t:.2f} L a tonne, plus "
                      f"generators) implies {from_forecast:,.0f} L of diesel; the store model expects {model:,.0f} L.")}


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
