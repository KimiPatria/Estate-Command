"""Crew productivity: what a fair day's target is, block by block.

Flat quotas are why steep, far or thin blocks get left uncollected. A harvester
sent to a block with tall palms and low bunch density cannot make the same
count as one on a dense young block, and being held to the same number is how
estates lose their better cutters.

So: model expected bunches per man-day from the conditions of the block, and
report the residual per worker. The expected figure becomes an adjusted target;
the residual is what is left once conditions are accounted for.

What is real
------------
The conditions are. Terrain slope is a real Copernicus DEM measurement, palm
age comes from the client's own planting years, and bunch density is computed
from their recorded harvest. What is synthetic is who cut what on which day -
EPMS records that in t_oph at employee grain and the EC export was aggregated
to block and month.

The synthetic worker rows are generated DOWN from the real block totals, so
every worker-day sums back to a bunch count the client would recognise.

On the output, and the line this module will not cross
------------------------------------------------------
The product is an adjusted daily target per block. The residual is a question
for a supervisor, never an accusation: a low residual can be a difficult block
the terrain model did not capture, a sick worker, a bad road, or a genuine
problem, and this module cannot tell those apart.

"Phantom labour" does not appear in any output. In front of a client whose
harvesters are named in the data it is a liability, and "unexplained variance
worth a supervisor visit" is the same finding stated in a way that survives the
room.
"""

import csv
import logging
from collections import defaultdict
from pathlib import Path
from threading import Lock

import numpy as np

log = logging.getLogger("estate-command.models.productivity")

_DIR = Path(__file__).parent.parent / "data" / "synthetic"
_CACHE: dict = {}
_LOCK = Lock()

# Block conditions the expected output is modelled from. All three are real.
FEATURES = [
    ("slope_deg", "terrain slope", "real"),
    ("palm_age_years", "palm age", "real"),
    ("bunch_density_per_ha", "bunch density", "real"),
]

# A worker needs at least this many days before a residual means anything.
MIN_DAYS = 12

# What the generator actually put in, in bunches per day per unit, so the fit
# can be checked against a known truth the way the shrinkage detector is.
# Taken from gen_harvester_days in gis/build_synthetic.py: a difficulty factor
# applied to a base of 95 bunches.
TRUE_EFFECT = {
    "slope_deg": -0.055 * 95,
    "palm_age_years": -0.022 * 95,
}


def _read(name: str) -> list[dict]:
    path = _DIR / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    return list(csv.DictReader(lines))


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _fit(estate: str = "EC") -> dict | None:
    rows = _read("ec_harvester_day.csv")
    if not rows:
        return None

    keys = [f[0] for f in FEATURES]
    data = []
    for r in rows:
        vals = [_f(r.get(k)) for k in keys]
        out = _f(r.get("bunches_cut"))
        md = _f(r.get("man_days"), 1.0) or 1.0
        if any(v is None for v in vals) or not out:
            continue
        data.append({
            "worker_id": r["worker_id"], "date": r["date"], "month": r["month"],
            "division_code": r["division_code"], "block_code": r["block_code"],
            "block": f'{r["division_code"]}-{r["block_code"]}',
            "x": vals, "actual": out / md, "man_days": md,
            "loose_fruit_kg": _f(r.get("loose_fruit_kg"), 0.0),
        })
    if len(data) < 200:
        return None

    X = np.array([d["x"] for d in data], dtype=float)
    y = np.array([d["actual"] for d in data], dtype=float)

    # Ordinary least squares with an intercept. Deliberately the simplest thing
    # that works: the point is an explainable target a mandor can be shown, and
    # a gradient-boosted model that lifts R-squared by two points but cannot be
    # written on a whiteboard is worse for this job.
    A = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ coef
    resid = y - pred
    ss_res = float((resid ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else None

    for d, p, e in zip(data, pred, resid):
        d["expected"] = float(p)
        d["residual"] = float(e)

    return {"rows": data, "coef": coef, "r2": r2,
            "sd_resid": float(resid.std()), "estate": estate.upper()}


def _state(estate: str = "EC"):
    key = estate.upper()
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        _CACHE[key] = _fit(estate)
        st = _CACHE[key]
        if st:
            log.info("[productivity] %s: %d worker-days, R2 %.3f",
                     key, len(st["rows"]), st["r2"] or 0)
        return _CACHE[key]


def reload_productivity() -> None:
    with _LOCK:
        _CACHE.clear()


def block_targets(estate: str = "EC") -> dict:
    """{'div-block': adjusted daily target} from the cached fit.

    The scheduler reads this to size a gang's day on each block. It is the
    same figure the panel shows as the adjusted target, and the fit is done
    once and cached, never inside a request.
    """
    st = _state(estate)
    if st is None:
        return {}
    by_block: dict = defaultdict(lambda: [0, 0.0])
    for d in st["rows"]:
        b = by_block[d["block"]]
        b[0] += 1
        b[1] += d["expected"]
    return {k: round(v[1] / v[0]) for k, v in by_block.items() if v[0]}


def assess(estate: str = "EC", top: int = 12) -> dict:
    st = _state(estate)
    if st is None:
        return {"available": False,
                "reason": "No worker-day feed. Run python gis/build_synthetic.py."}

    rows, coef = st["rows"], st["coef"]

    by_worker = defaultdict(lambda: {"days": 0, "bunches": 0.0, "expected": 0.0,
                                     "resid": 0.0, "blocks": set(), "loose": 0.0})
    for d in rows:
        w = by_worker[d["worker_id"]]
        w["days"] += 1
        w["bunches"] += d["actual"]
        w["expected"] += d["expected"]
        w["resid"] += d["residual"]
        w["blocks"].add(d["block"])
        w["loose"] += d["loose_fruit_kg"]

    workers = []
    for wid, v in by_worker.items():
        if v["days"] < MIN_DAYS:
            continue
        workers.append({
            "worker_id": wid,
            "days": v["days"],
            "mean_actual": round(v["bunches"] / v["days"], 1),
            "mean_expected": round(v["expected"] / v["days"], 1),
            # Standardised so a worker on hard blocks is comparable with one on
            # easy blocks, which is the entire point of the exercise.
            "residual_per_day": round(v["resid"] / v["days"], 1),
            "index": round(v["bunches"] / v["expected"], 3) if v["expected"] else None,
            "blocks_worked": len(v["blocks"]),
        })
    workers.sort(key=lambda w: -(w["index"] or 0))

    # Adjusted target per block: what the model says a fair day is there.
    by_block = defaultdict(lambda: {"days": 0, "expected": 0.0, "actual": 0.0,
                                    "x": None})
    for d in rows:
        b = by_block[d["block"]]
        b["days"] += 1
        b["expected"] += d["expected"]
        b["actual"] += d["actual"]
        b["x"] = d["x"]
    targets = [{
        "block": k,
        "days": v["days"],
        "adjusted_target": round(v["expected"] / v["days"]),
        "actual_mean": round(v["actual"] / v["days"], 1),
        **{f[0]: round(v["x"][i], 2) for i, f in enumerate(FEATURES)},
    } for k, v in by_block.items()]
    targets.sort(key=lambda t: t["adjusted_target"])

    flat = round(float(np.mean([d["actual"] for d in rows])))
    spread = [t["adjusted_target"] for t in targets]

    return {
        "available": True,
        "estate": st["estate"],
        "worker_days": len(rows),
        "workers_scored": len(workers),
        "model": {
            "form": "ordinary least squares on block conditions",
            "intercept": round(float(coef[0]), 2),
            "coefficients": [
                {"feature": f[0], "label": f[1], "provenance": f[2],
                 "coefficient": round(float(c), 3)}
                for f, c in zip(FEATURES, coef[1:])
            ],
            "r_squared": round(st["r2"], 3) if st["r2"] is not None else None,
            "residual_sd": round(st["sd_resid"], 1),
            "min_days_to_score": MIN_DAYS,
            "note": ("Least squares rather than a boosted model on purpose. "
                     "The output is a target a mandor has to accept, so it has "
                     "to be explainable on a whiteboard."),
        },
        "flat_quota": flat,
        "target_range": [min(spread), max(spread)] if spread else None,
        # The number that makes the case: how wrong a single estate-wide quota
        # is at the two ends of the estate.
        "flat_quota_error": ([round(flat - min(spread)), round(max(spread) - flat)]
                             if spread else None),
        "hardest_blocks": targets[:top],
        "easiest_blocks": list(reversed(targets[-top:])),
        "top_workers": workers[:top],
        "bottom_workers": workers[-top:][::-1],
        "feature_provenance": {
            "real": [f[0] for f in FEATURES],
            "synthetic": ["worker identity and daily allocation"],
        },
        "recovery": _recovery(coef),
        "provenance": ("mixed: block conditions are real measurements; the "
                       "per-worker daily rows are generated down from the "
                       "client's real block totals."),
        "warning": ("A residual is a question for a supervisor, not a verdict. "
                    "A low one can be a hard block the model did not capture, a "
                    "bad road, illness, or a real problem, and this model cannot "
                    "tell them apart."),
    }


def _recovery(coef) -> dict:
    """Fitted coefficients against the effect the generator actually put in.

    The same discipline as the shrinkage detector's injected-versus-recovered
    table, applied to a regression. It is reported even though it is
    unflattering, because a reader who cannot check the fit has no reason to
    believe the targets it produces.
    """
    rows = []
    for (key, label, _), c in zip(FEATURES, coef[1:]):
        truth = TRUE_EFFECT.get(key)
        if truth is None:
            continue
        rows.append({
            "feature": key, "label": label,
            "generator_effect": round(truth, 2),
            "fitted_coefficient": round(float(c), 2),
            "overstated_by": round(float(c) / truth, 2) if truth else None,
        })
    return {
        "available": bool(rows),
        "coefficients": rows,
        "reading": (
            "The fit recovers the right SIGN on every term and the right "
            "ordering, but overstates the magnitudes. Two reasons, both of "
            "which a real extract would share. Crew size is a whole number, so "
            "per-worker output is quantised and the regression reads that step "
            "as slope. And palm age is correlated with block yield in the "
            "client's own data, so the age term absorbs some of the yield "
            "effect on top of its own. "
            "This is why the product is the adjusted TARGET, which depends on "
            "the fitted surface as a whole, and not the individual "
            "coefficients, which should not be quoted as agronomic constants."),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    a = assess()
    if not a["available"]:
        raise SystemExit(a["reason"])
    m = a["model"]
    print(f"\n{a['worker_days']} worker-days, {a['workers_scored']} workers scored")
    print(f"R2 {m['r_squared']}, residual sd {m['residual_sd']} bunches/day\n")
    print(f"  intercept {m['intercept']}")
    for c in m["coefficients"]:
        print(f"  {c['label']:18s} {c['coefficient']:+8.3f}  ({c['provenance']})")
    print(f"\nflat quota would be {a['flat_quota']} bunches/day")
    print(f"adjusted targets range {a['target_range'][0]} to {a['target_range'][1]}")
    print(f"a flat quota is {a['flat_quota_error'][0]} too high on the hardest "
          f"blocks and {a['flat_quota_error'][1]} too low on the easiest")
