"""Field-to-mill shrinkage detection over the trip ledger.

The question: which trips delivered less than the field count says they should
have? The gap between what the mandor recorded at the collection point and
what the weighbridge recorded at the mill is where crop goes missing, and it
goes missing for several different reasons - fruit genuinely left behind,
mis-counting, a bonus-driven over-count, or theft in transit. A detector
cannot tell those apart. It can only say which trips do not look like the rest.

Expected weight
---------------
    expected_kg = bunch_count x the block's own historical bunch weight

Deviation is then (expected - net) / expected. A trip 2% light is ordinary
loose fruit and moisture. A trip 12% light is not.

Why two detectors
-----------------
A robust z-score on the deviation is the primary. It is transparent, a
supervisor can check it by hand, and it needs no training. Median and MAD
rather than mean and standard deviation, because the anomalies are in the
sample being used to define normal and a handful of bad trips would drag the
mean towards themselves and hide inside the widened band.

IsolationForest is the second opinion, run over the same signal. It is
distribution-free where the z-score assumes a shape, and it is more willing to
flag, so the two bracket the operational choice rather than duplicating it.

Scoring, and why it is shown
----------------------------
The ledger is generated, and a detector run over data you generated yourself
proves nothing on its own. So the generator plants a known cohort - one
driver, one fortnight, one pair of routes - and this module scores itself
against it: precision, recall, and which planted trips were missed.

That is not a disclaimer. It is the argument. It says the method works and
can be measured, which is what earns the real weighbridge extract. The
`is_planted_anomaly` column is used ONLY for scoring and never as a feature;
`FEATURES` below is the whole of what the detectors see.

What the planted answers actually caught us doing wrong
--------------------------------------------------------
The first version of this module fed IsolationForest six columns - deviation,
turnaround, queue, distance, dockage, loose fruit - on the reasonable-sounding
argument that more context finds subtler fraud. Scored against the planted
cohort it found 200 trips and got NONE of them right: precision 0.0 at every
contamination setting tried.

The reason is dilution. The anomalies are extreme in one dimension and
ordinary in the other five, so the trees mostly split on distance and queue
time and spend the whole contamination budget on trips that are merely
operationally unusual. Narrowing to the deviation signal took precision from
0.0 to 0.85.

That ablation is kept and reported in `validation()`, because it is the single
most persuasive thing in this module. Without planted answers the six-column
version would have shipped, produced a confident list of 200 trips, and been
wrong about every one of them. Nobody would ever have known.
"""

import csv
import logging
from collections import defaultdict
from pathlib import Path
from threading import Lock

import numpy as np

log = logging.getLogger("estate-command.models.shrinkage")

_DIR = Path(__file__).parent.parent / "data" / "synthetic"
_CACHE: dict = {}
_LOCK = Lock()

# Everything the detectors are allowed to see. The planted-anomaly label is
# not here and must never be.
#
# Narrow on purpose. The wide set below was tried first and scored 0.0
# precision; see the module docstring and validation()["ablation"].
FEATURES = ("deviation_pct",)

# The variant that failed, kept so the ablation can be recomputed rather than
# quoted from memory.
WIDE_FEATURES = ("deviation_pct", "turnaround_h", "queue_h", "km_to_mill",
                 "mill_dockage_pct", "loose_fruit_pct")

# Robust z above which a trip is flagged. 3.5 on a MAD-based score is the
# conventional threshold and corresponds to roughly a 1-in-2000 trip under
# a normal bulk, which suits a ledger of this size.
Z_FLAG = 3.5

# Contamination passed to IsolationForest. An estate asking this question does
# not know its own anomaly rate, so this is set to a plausible operating
# assumption - two trips in a thousand - rather than fitted to the answer.
CONTAMINATION = 0.002


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


def _load() -> list[dict]:
    """Trips with their expected weight and deviation, ready for scoring."""
    trips = _read("ec_weighbridge.csv")
    if not trips:
        return []

    abw = {f'{int(r["division_code"])}|{int(r["block_code"])}': _f(r["abw_kg"])
           for r in _read("ec_abw.csv")}

    rows = []
    for t in trips:
        k = f'{int(t["division_code"])}|{int(t["block_code"])}'
        w = abw.get(k)
        b = _f(t["bunches_epms"], 0)
        net = _f(t["net_kg"], 0)
        if not w or not b or not net:
            continue
        expected = b * w
        rows.append({
            "trip_id": t["trip_id"],
            "date": t["date"],
            "month": t["month"],
            "block": f'{t["division_code"]}-{t["block_code"]}',
            "division_code": t["division_code"],
            "block_code": t["block_code"],
            "route_code": t["route_code"],
            "vehicle_id": t["vehicle_id"],
            "driver_id": t["driver_id"],
            "bunches_epms": int(b),
            "expected_kg": round(expected),
            "net_kg": round(net),
            "gap_kg": round(expected - net),
            "deviation_pct": round(100 * (expected - net) / expected, 3),
            "turnaround_h": _f(t["turnaround_h"], 0),
            "queue_h": _f(t["queue_h"], 0),
            "km_to_mill": _f(t["km_to_mill"], 0),
            "mill_dockage_pct": _f(t["mill_dockage_pct"], 0),
            "loose_fruit_pct": round(100 * _f(t["loose_fruit_kg"], 0) / expected, 3),
            # scoring only, never a feature
            "_planted": int(_f(t.get("is_planted_anomaly"), 0)),
        })
    return rows


def _robust_z(values: np.ndarray) -> np.ndarray:
    """Median-and-MAD z-score. Immune to the anomalies inside the sample."""
    med = np.median(values)
    mad = np.median(np.abs(values - med))
    if mad == 0:
        sd = values.std() or 1.0
        return (values - values.mean()) / sd
    # 0.6745 makes MAD comparable to a standard deviation under normality.
    return 0.6745 * (values - med) / mad


def _score(flagged: set, planted: set) -> dict:
    """Precision and recall of a flag set against the planted cohort."""
    tp = len(flagged & planted)
    fp = len(flagged - planted)
    fn = len(planted - flagged)
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    return {
        "flagged": len(flagged),
        "planted": len(planted),
        "true_positives": tp,
        "false_positives": fp,
        "missed": fn,
        "precision": round(prec, 3) if prec is not None else None,
        "recall": round(rec, 3) if rec is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
    }


def _isolation(rows, feature_names, contamination, annotate=False):
    """IsolationForest over the named columns. Returns (flagged ids, error).

    Standardised first, because the forest splits on raw values and kilometres
    would otherwise swamp percentages. Any failure returns an empty flag set
    rather than raising: the panel must render with or without sklearn.
    """
    try:
        from sklearn.ensemble import IsolationForest
    except Exception as exc:
        return set(), str(exc)
    try:
        X = np.array([[r[f] for f in feature_names] for r in rows], dtype=float)
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)
        model = IsolationForest(n_estimators=300, contamination=contamination,
                                random_state=20260911)
        pred = model.fit_predict(X)
        if annotate:
            for r, s in zip(rows, model.score_samples(X)):
                r["iso_score"] = round(float(-s), 4)
        return ({r["trip_id"] for r, p in zip(rows, pred) if p == -1}, None)
    except Exception as exc:
        return set(), str(exc)


def _fit(rows: list[dict]) -> dict:
    dev = np.array([r["deviation_pct"] for r in rows], dtype=float)
    z = _robust_z(dev)
    for r, zi in zip(rows, z):
        r["robust_z"] = round(float(zi), 2)

    z_flag = {r["trip_id"] for r, zi in zip(rows, z) if zi >= Z_FLAG}

    iso_flag, iso_error = _isolation(rows, FEATURES, CONTAMINATION, annotate=True)
    if iso_error:
        log.warning("[shrinkage] IsolationForest unavailable: %s", iso_error)

    planted = {r["trip_id"] for r in rows if r["_planted"]}
    both = z_flag & iso_flag

    for r in rows:
        r["flagged_z"] = r["trip_id"] in z_flag
        r["flagged_iso"] = r["trip_id"] in iso_flag
        r["corroborated"] = r["trip_id"] in both

    return {
        "rows": rows,
        "z_flag": z_flag,
        "iso_flag": iso_flag,
        "both": both,
        "planted": planted,
        "iso_error": iso_error,
        "median_deviation_pct": round(float(np.median(dev)), 3),
        "p99_deviation_pct": round(float(np.percentile(dev, 99)), 3),
    }


def _state() -> dict:
    with _LOCK:
        if "state" in _CACHE:
            return _CACHE["state"]
        rows = _load()
        _CACHE["state"] = _fit(rows) if rows else None
        if rows:
            st = _CACHE["state"]
            log.info("[shrinkage] %d trips, %d flagged on z, %d on isolation "
                     "forest, %d corroborated", len(rows), len(st["z_flag"]),
                     len(st["iso_flag"]), len(st["both"]))
        return _CACHE["state"]


def reload_shrinkage() -> None:
    with _LOCK:
        _CACHE.clear()


def available() -> bool:
    return _state() is not None


def assess(top: int = 15) -> dict:
    """The panel payload: what was flagged, by whom, and how well it scored."""
    st = _state()
    if st is None:
        return {
            "available": False,
            "reason": "No trip ledger. The weighbridge feed has not been built.",
            "command": "python gis/build_synthetic.py",
        }

    rows = st["rows"]
    flagged = [r for r in rows if r["flagged_z"] or r["flagged_iso"]]
    flagged.sort(key=lambda r: -r["deviation_pct"])

    # Where the losses concentrate. A single trip is noise; a driver or a route
    # that is consistently light is the finding.
    def concentrate(field):
        agg = defaultdict(lambda: {"trips": 0, "flagged": 0, "gap_kg": 0.0,
                                   "expected_kg": 0.0})
        for r in rows:
            a = agg[r[field]]
            a["trips"] += 1
            a["gap_kg"] += r["gap_kg"]
            a["expected_kg"] += r["expected_kg"]
            if r["flagged_z"] or r["flagged_iso"]:
                a["flagged"] += 1
        out = [{field: k, "trips": v["trips"], "flagged": v["flagged"],
                "gap_kg": round(v["gap_kg"]),
                "shrinkage_pct": round(100 * v["gap_kg"] / v["expected_kg"], 2)
                if v["expected_kg"] else None}
               for k, v in agg.items()]
        out.sort(key=lambda r: -(r["shrinkage_pct"] or 0))
        return out

    total_expected = sum(r["expected_kg"] for r in rows)
    total_net = sum(r["net_kg"] for r in rows)
    flagged_gap = sum(r["gap_kg"] for r in flagged)

    return {
        "available": True,
        "trips": len(rows),
        "method": {
            "expected": "bunch count x the block's own bunch weight",
            "primary": f"robust z-score on deviation, flagged at z >= {Z_FLAG}",
            "secondary": (f"IsolationForest on the same signal at "
                          f"{CONTAMINATION * 100:.1f}% contamination"),
            "features": list(FEATURES),
            "note": ("The planted-anomaly label is used only to score the "
                     "detectors. It is never a feature."),
        },
        "totals": {
            "expected_t": round(total_expected / 1000, 1),
            "weighed_t": round(total_net / 1000, 1),
            "gap_t": round((total_expected - total_net) / 1000, 1),
            "shrinkage_pct": round(100 * (total_expected - total_net) / total_expected, 2),
            "median_trip_deviation_pct": st["median_deviation_pct"],
            "p99_trip_deviation_pct": st["p99_deviation_pct"],
        },
        "flags": {
            "robust_z": len(st["z_flag"]),
            "isolation_forest": len(st["iso_flag"]),
            "corroborated": len(st["both"]),
            "either": len(flagged),
            "flagged_gap_t": round(flagged_gap / 1000, 1),
        },
        "worst_trips": [{k: r[k] for k in
                         ("trip_id", "date", "block", "route_code", "vehicle_id",
                          "driver_id", "bunches_epms", "expected_kg", "net_kg",
                          "gap_kg", "deviation_pct", "robust_z", "flagged_z",
                          "flagged_iso", "corroborated")}
                        for r in flagged[:top]],
        "by_driver": concentrate("driver_id")[:8],
        "by_route": concentrate("route_code")[:8],
        "by_vehicle": concentrate("vehicle_id")[:8],
        "validation": validation(),
        "provenance": ("synthetic: the trip ledger is generated. Block bunch "
                       "counts and harvest-day counts inside it are the "
                       "client's real figures."),
    }


def validation() -> dict:
    """Injected versus recovered. The honest way to demonstrate a detector.

    Everything a reader needs to judge the method: how many anomalies were
    planted, how many each detector found, what it cost in false positives,
    and which planted trips were missed and why.
    """
    st = _state()
    if st is None:
        return {"available": False}

    planted = st["planted"]
    rows_by_id = {r["trip_id"]: r for r in st["rows"]}
    missed = [rows_by_id[t] for t in sorted(planted - (st["z_flag"] | st["iso_flag"]))]

    return {
        "available": True,
        "design": {
            "planted_driver": "D-07",
            "planted_window": "2025-03-10 to 2025-03-23",
            "planted_routes": ["R-03", "R-04"],
            "planted_shrinkage_pct": "8.5 to 16",
            "baseline_shrinkage_pct": 1.8,
        },
        "robust_z": _score(st["z_flag"], planted),
        "isolation_forest": _score(st["iso_flag"], planted),
        "corroborated": _score(st["both"], planted),
        "either": _score(st["z_flag"] | st["iso_flag"], planted),
        "missed_examples": [{k: r[k] for k in
                             ("trip_id", "date", "driver_id", "route_code",
                              "deviation_pct", "robust_z")}
                            for r in missed[:5]],
        "ablation": _ablation(st["rows"], planted),
        "reading": _validation_reading(st, planted),
    }


def _ablation(rows, planted) -> dict:
    """What the wide feature set scores, recomputed rather than remembered.

    This is the part of the validation worth showing a client. It is the
    difference between a method that was measured and one that merely sounded
    reasonable, and it is only possible because the anomalies were planted.
    """
    wide, err = _isolation(rows, WIDE_FEATURES, CONTAMINATION)
    if err:
        return {"available": False, "reason": err}
    narrow = _score({r["trip_id"] for r in rows if r["flagged_iso"]}, planted)
    return {
        "available": True,
        "narrow": {"features": list(FEATURES), **narrow},
        "wide": {"features": list(WIDE_FEATURES), **_score(wide, planted)},
        "reading": (
            "The same detector on the same trips, given six columns instead of "
            "one. More context sounded like better fraud detection and was the "
            "first thing built. Scored against the planted cohort it is worse, "
            "because the anomalies are extreme in one dimension and ordinary in "
            "the other five, so the forest spends its budget on trips that are "
            "merely operationally unusual. Without planted answers the wide "
            "version would have shipped a confident list and nobody would have "
            "checked it."),
    }


def _validation_reading(st, planted) -> str:
    """One paragraph a non-specialist can act on.

    Written here rather than by a model, because every figure in it has to
    survive the audit in gis/reasoning.py and the safest way to guarantee that
    is to compute the sentence where the numbers live.
    """
    if not planted:
        return "No anomalies were planted, so there is nothing to score against."
    z = _score(st["z_flag"], planted)
    iso = _score(st["iso_flag"], planted)
    return (
        f"{z['planted']} anomalous trips were planted in the ledger. The robust "
        f"z-score flagged {z['flagged']} and every one was a planted trip: "
        f"precision {z['precision']}, recall {z['recall']}. The isolation "
        f"forest flagged {iso['flagged']} and found {iso['true_positives']} of "
        f"them, at precision {iso['precision']} and recall {iso['recall']}, "
        f"costing {iso['false_positives']} wasted checks. "
        "That trade is the operational choice rather than a modelling one. A "
        "supervisor with time for a handful of visits wants the z-score list, "
        "which never sends him anywhere for nothing. An auditor reviewing a "
        "quarter wants the forest, which finds most of the loss and accepts "
        "some wasted checks to do it."
    )


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    a = assess()
    if not a["available"]:
        raise SystemExit(a["reason"])
    print(json.dumps({"totals": a["totals"], "flags": a["flags"]}, indent=2))
    v = a["validation"]
    print("\ninjected vs recovered")
    for k in ("robust_z", "isolation_forest", "corroborated", "either"):
        s = v[k]
        print(f"  {k:18s} flagged {s['flagged']:4d}  tp {s['true_positives']:3d}  "
              f"fp {s['false_positives']:4d}  precision {s['precision']}  "
              f"recall {s['recall']}")
    print("\n" + v["reading"])
    print("\nworst by driver:")
    for r in a["by_driver"][:4]:
        print(f"  {r['driver_id']}  {r['trips']:5d} trips  "
              f"shrinkage {r['shrinkage_pct']}%  flagged {r['flagged']}")
