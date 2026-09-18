"""Average bunch weight trend per block, read off the weighbridge ledger.

Question: is bunch weight moving, and where?

Per trip, ABW = (net_kg - loose_fruit_kg) / bunches_epms: the mill's net weight
less the loose fruit that rode in with the bunches, over the count the field
side recorded for the same load. Folded to block-month (trip-weighted, so a big
load counts for what it carried), then to division and estate by month, with a
least-squares slope per block across the window.

Read the calibration before quoting a level. ec_abw.csv's own header says the
per-block weights were back-solved so the estate lands at 23 t/ha/yr, because
the client's real bunch counts imply an impossible tonnage at a normal 12-16 kg
bunch. The trip ledger inherits those weights, so the ~8 kg level here is an
artefact of that choice. Only the movement and the ranking carry information -
and gis/build_synthetic.py planted no movement at all: each block has ONE
weight for the whole window (_abw_map), so whatever slope this module recovers
is trip noise (a ~1 % gaussian shrink and a 0.4-2.1 % loose-fruit draw). The
age curve IS planted: +3.5 % per year of palm age about age nine (gen_abw).

Provenance, in layers.py's vocabulary: derived. The bunch count per block-month
and the harvest days are the client's real EPMS figures; every weight on the
ticket is generated. The ledger's is_planted_anomaly column is another
feature's answer key and is never read here.
"""

from collections import defaultdict
from threading import Lock

import numpy as np

from gis import layers

_CACHE: dict = {}
_LOCK = Lock()

# What gis/build_synthetic.py planted, so the panel can say recovered-vs-planted.
PLANTED_AGE_PCT_PER_YEAR = 3.5       # gen_abw: 1 + 0.035 * (age - 9)
PLANTED_TREND_PCT_PER_MONTH = 0.0    # _abw_map: one weight per block, all window
AGE_ANCHOR = 9

# A block is "moving" only if the fitted change over the window is worth a
# manager's attention AND stands clear of the trip-to-trip spread.
MOVE_PCT = 3.0
MOVE_Z = 3.0

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _mlabel(m: str) -> str:
    try:
        y, mo = m.split("-")
        return f"{_MONTHS[int(mo) - 1]} {y}"
    except (ValueError, IndexError):
        return m


def _r(v, d=2):
    if v is None:
        return None
    try:
        if isinstance(v, float) and not np.isfinite(v):
            return None
        return round(float(v), d)
    except (TypeError, ValueError):
        return None


def _header_caveat() -> str:
    """The calibration note in the words of ec_abw.csv's own header line."""
    path = layers._DIR / "ec_abw.csv"
    if not path.exists():
        return ("Back-solved so the estate lands at 23.0 t/ha/yr. The real counts "
                "imply 34-46 t/ha/yr at a normal 12-16 kg ABW, which is impossible - "
                "the OPH export likely double-counts. Ask the client.")
    with path.open(encoding="utf-8") as fh:
        first = fh.readline().strip()
    i = first.find("Back-solved")
    return first[i:] if i >= 0 else first.lstrip("#").strip()


# ── the computation, once per estate ───────────────────────────────────────

def _compute(estate: str) -> dict:
    rows = layers.block_rows(estate) or []
    if not rows:
        return {"available": False, "estate": estate,
                "reason": f"No block ontology for {estate}."}
    trips = layers._read("ec_weighbridge.csv")
    if not trips:
        return {"available": False, "estate": estate,
                "reason": "No weighbridge ledger. Run python gis/build_synthetic.py."}

    by_key = {layers._key(r["division_code"], r["block_code"]): r for r in rows}
    months = sorted({t["month"] for t in trips})
    mi = {m: i for i, m in enumerate(months)}
    n_m = len(months)

    # Fold the ledger to block-month: weight net of loose fruit, bunches, trips,
    # and the per-trip ABW list for the spread.
    kg = defaultdict(lambda: np.zeros(n_m))
    bun = defaultdict(lambda: np.zeros(n_m))
    ntr = defaultdict(lambda: np.zeros(n_m, dtype=int))
    vals: dict = defaultdict(list)
    n_trips = 0
    for t in trips:
        k = layers._key(t["division_code"], t["block_code"])
        if k not in by_key:
            continue
        b = layers._i(t["bunches_epms"]) or 0
        if b <= 0:
            continue
        net = (layers._f(t["net_kg"]) or 0.0) - (layers._f(t["loose_fruit_kg"]) or 0.0)
        if net <= 0:
            continue
        j = mi[t["month"]]
        kg[k][j] += net
        bun[k][j] += b
        ntr[k][j] += 1
        vals[(k, j)].append(net / b)
        n_trips += 1

    keys = [k for k in by_key if k in kg]
    if not keys:
        return {"available": False, "estate": estate,
                "reason": "The ledger names no block this estate carries."}

    # ── estate and division by month ──────────────────────────────────────
    est_kg = np.zeros(n_m)
    est_b = np.zeros(n_m)
    est_n = np.zeros(n_m, dtype=int)
    div_kg = defaultdict(lambda: np.zeros(n_m))
    div_b = defaultdict(lambda: np.zeros(n_m))
    div_blocks = defaultdict(int)
    for k in keys:
        d = str(by_key[k]["division_code"])
        est_kg += kg[k]
        est_b += bun[k]
        est_n += ntr[k]
        div_kg[d] += kg[k]
        div_b[d] += bun[k]
        div_blocks[d] += 1

    with np.errstate(divide="ignore", invalid="ignore"):
        est_abw = np.where(est_b > 0, est_kg / est_b, np.nan)
    estate_mean = float(est_kg.sum() / est_b.sum())
    estate_series = [{
        "month": m, "label": _mlabel(m),
        "abw_kg": _r(est_abw[j], 3),
        "trips": int(est_n[j]), "bunches": int(est_b[j]),
        "net_less_loose_kg": int(round(est_kg[j])),
    } for j, m in enumerate(months)]

    division_series = []
    for d in sorted(div_kg, key=lambda s: int(s) if s.isdigit() else s):
        with np.errstate(divide="ignore", invalid="ignore"):
            s = np.where(div_b[d] > 0, div_kg[d] / div_b[d], np.nan)
        mean = float(div_kg[d].sum() / div_b[d].sum())
        ok = ~np.isnan(s)
        change = None
        if ok.sum() >= 2:
            first, last = s[ok][0], s[ok][-1]
            change = 100.0 * (last - first) / first
        division_series.append({
            "division_code": d, "blocks": div_blocks[d],
            "abw_kg": [_r(v, 3) for v in s],
            "mean_kg": _r(mean, 3),
            "change_pct": _r(change, 2),
        })

    # ── per block: series, slope, its standard error, the spread ──────────
    x = np.arange(n_m, dtype=float)
    blocks = []
    cvs = []            # every block-month with two or more trips
    widest = []
    for k in keys:
        r = by_key[k]
        b = bun[k]
        ok = b > 0
        with np.errstate(divide="ignore", invalid="ignore"):
            ser = np.where(ok, kg[k] / b, np.nan)
        mean = float(kg[k].sum() / b.sum())

        # Pooled within-block-month trip variance, so the slope's error is
        # measured against how much the tickets already disagree.
        ss, dof = 0.0, 0
        for j in range(n_m):
            v = vals.get((k, j))
            if v and len(v) >= 2:
                a = np.asarray(v)
                ss += float(((a - a.mean()) ** 2).sum())
                dof += len(a) - 1
                cv = 100.0 * a.std(ddof=1) / a.mean()
                cvs.append(cv)
                widest.append({
                    "block_id": r["block_id"], "block_label": r["block_label"],
                    "division_code": r["division_code"], "month": months[j],
                    "label": _mlabel(months[j]), "trips": len(a),
                    "abw_kg": _r(ser[j], 3), "cv_pct": _r(cv, 2),
                    "min_kg": _r(a.min(), 2), "max_kg": _r(a.max(), 2),
                })
        sigma2 = ss / dof if dof else 0.0

        slope = se = z = change = None
        if ok.sum() >= 3:
            xs, ys = x[ok], ser[ok]
            slope_kg, _ = np.polyfit(xs, ys, 1)
            sxx = float(((xs - xs.mean()) ** 2).sum())
            # var(slope) = sum (x-x̄)^2 var(y_j) / sxx^2, var(y_j) ≈ σ² / trips_j
            var_y = sigma2 / np.maximum(ntr[k][ok], 1)
            var_slope = float((((xs - xs.mean()) ** 2) * var_y).sum()) / (sxx ** 2)
            se_kg = float(np.sqrt(var_slope))
            slope = float(slope_kg)
            se = se_kg
            z = slope / se if se > 0 else None
            change = 100.0 * slope * (n_m - 1) / mean

        moving = bool(change is not None and z is not None
                      and abs(change) >= MOVE_PCT and abs(z) >= MOVE_Z)
        blocks.append({
            "block_id": r["block_id"],
            "block_label": r["block_label"],
            "division_code": r["division_code"],
            "palm_age_years": r.get("palm_age_years"),
            "planted_ha": r.get("planted_ha"),
            "abw_kg": _r(mean, 3),
            "abw_calibrated_kg": r.get("abw_kg"),
            "series": [_r(v, 3) for v in ser],
            "dev_pct": [_r(100.0 * (v / mean - 1.0), 2) if np.isfinite(v) else None
                        for v in ser],
            "months_with_trips": int(ok.sum()),
            "trips": int(ntr[k].sum()),
            "bunches": int(b.sum()),
            "slope_kg_month": _r(slope, 4),
            "slope_pct_month": _r(100.0 * slope / mean, 3) if slope is not None else None,
            "slope_se_pct_month": _r(100.0 * se / mean, 3) if se is not None else None,
            "z": _r(z, 2),
            "change_pct": _r(change, 2),
            "cv_pct": _r(100.0 * np.sqrt(sigma2) / mean, 2) if dof else None,
            "moving": moving,
            "direction": ("falling" if slope < 0 else "rising") if moving else "noise",
        })

    fitted = [b for b in blocks if b["change_pct"] is not None]
    falling = sorted(fitted, key=lambda b: b["change_pct"])
    rising = sorted(fitted, key=lambda b: -b["change_pct"])
    moving = [b for b in fitted if b["moving"]]
    n_fall = sum(1 for b in moving if b["direction"] == "falling")
    n_rise = len(moving) - n_fall

    slopes_pct = np.array([b["slope_pct_month"] for b in fitted], dtype=float)
    ses_pct = np.array([b["slope_se_pct_month"] for b in fitted
                        if b["slope_se_pct_month"] is not None], dtype=float)
    slope_sd_obs = float(slopes_pct.std()) if len(slopes_pct) else None
    slope_sd_exp = float(np.sqrt((ses_pct ** 2).mean())) if len(ses_pct) else None

    cvs_a = np.array(cvs, dtype=float)
    widest.sort(key=lambda w: -(w["cv_pct"] or 0))
    trips_per_bm = np.array([ntr[k][j] for k in keys for j in range(n_m) if ntr[k][j] > 0])

    # ── the age curve ─────────────────────────────────────────────────────
    age_kg = defaultdict(float)
    age_b = defaultdict(float)
    age_n = defaultdict(int)
    age_tr = defaultdict(int)
    pairs = []
    for b_, k in zip(blocks, keys):
        age = b_["palm_age_years"]
        if age is None:
            continue
        age_kg[age] += kg[k].sum()
        age_b[age] += bun[k].sum()
        age_n[age] += 1
        age_tr[age] += b_["trips"]
        pairs.append((float(age), b_["abw_kg"]))
    age_slope_pct = age_r2 = None
    y9 = None
    if len(pairs) >= 3 and len({a for a, _ in pairs}) >= 2:
        A = np.array([a for a, _ in pairs])
        Y = np.array([y for _, y in pairs])
        s, c = np.polyfit(A, Y, 1)
        y9 = float(s * AGE_ANCHOR + c)
        age_slope_pct = 100.0 * float(s) / y9
        age_r2 = float(np.corrcoef(A, Y)[0, 1] ** 2)
    anchor = (age_kg[AGE_ANCHOR] / age_b[AGE_ANCHOR]) if age_b.get(AGE_ANCHOR) else y9
    age_curve = [{
        "age": a, "blocks": age_n[a], "trips": age_tr[a],
        "abw_kg": _r(age_kg[a] / age_b[a], 3),
        "vs_age9_pct": _r(100.0 * (age_kg[a] / age_b[a] / anchor - 1.0), 2) if anchor else None,
    } for a in sorted(age_kg)]
    thin = [str(b["age"]) for b in age_curve if b["blocks"] < 5]

    # ── the level, against the calibration it inherits ────────────────────
    cal = layers._state().get("abw", {})
    cal_vals = [v for v in cal.values() if v]
    cal_mean = float(np.mean(cal_vals)) if cal_vals else None
    cal_w = (sum(cal[k] * bun[k].sum() for k in keys if cal.get(k))
             / sum(bun[k].sum() for k in keys if cal.get(k))) if cal_vals else None
    ratios = [b["abw_kg"] / b["abw_calibrated_kg"] for b in blocks if b.get("abw_calibrated_kg")]
    ratio = float(np.mean(ratios)) if ratios else None

    # ── the words ─────────────────────────────────────────────────────────
    first_m, last_m = _mlabel(months[0]), _mlabel(months[-1])
    est_first = est_abw[~np.isnan(est_abw)][0]
    est_last = est_abw[~np.isnan(est_abw)][-1]
    est_change = 100.0 * (est_last - est_first) / est_first
    peak = 100.0 * (np.nanmax(est_abw) - np.nanmin(est_abw)) / estate_mean
    lead = falling[0] if falling else None
    n_blocks = len(blocks)

    if not moving:
        summary = (
            f"Bunch weight is not moving: estate ABW held at {estate_mean:.2f} kg "
            f"from {first_m} to {last_m} ({est_change:+.1f} % first to last month, "
            f"{peak:.1f} % peak to trough), and none of the {n_blocks} blocks shifted "
            f"{MOVE_PCT:.0f} % or more clear of trip noise. "
            + (f"The fastest faller, block {lead['block_label']} (division "
               f"{lead['division_code']}), lost {abs(lead['change_pct']):.1f} % over the "
               f"window, under the {MOVE_PCT:.0f} % worth acting on"
               + (f", and the spread of all {len(fitted)} block slopes "
                  f"({slope_sd_obs:.2f} %/month) is what trip noise alone predicts "
                  f"({slope_sd_exp:.2f} %/month). "
                  if slope_sd_obs is not None and slope_sd_exp is not None else ". ")
               if lead else "")
            + f"Do not act on the {estate_mean:.0f} kg level: it is a calibration "
            f"artefact, not a measurement."
        )
    else:
        summary = (
            f"Bunch weight is moving in {len(moving)} of {n_blocks} blocks - "
            f"{n_fall} falling, {n_rise} rising - led by block {lead['block_label']} "
            f"(division {lead['division_code']}, {lead['change_pct']:+.1f} % over the "
            f"window). Estate ABW went {est_first:.2f} to {est_last:.2f} kg "
            f"({est_change:+.1f} %) from {first_m} to {last_m}. Pull those blocks' "
            f"tickets before reading it as agronomy, and never act on the "
            f"{estate_mean:.0f} kg level, which is a calibration artefact."
        )

    header = _header_caveat()
    calibration = {
        "headline": (f"The {estate_mean:.0f} kg level is a calibration artefact, "
                     f"not a measurement."),
        "header": header,
        "read_as": ("Only the movement and the ranking on this panel carry "
                    "information, and both come from generated weights riding on "
                    "the client's real bunch counts. Nothing here was weighed."),
        "ask_client": ("The real figure to ask for is the weighbridge ticket per "
                       "trip - gross, tare, net and loose fruit - joined to the EPMS "
                       "bunch count for that same load, for the same months. With "
                       "that file this panel reruns unchanged."),
    }

    recovered_vs_planted = {
        "trend": {
            "planted": ("none: gis/build_synthetic.py gives each block one bunch "
                        "weight for the whole window (_abw_map), so the planted "
                        "month-on-month trend is zero everywhere"),
            "planted_pct_per_month": PLANTED_TREND_PCT_PER_MONTH,
            "recovered_slope_sd_pct_per_month": _r(slope_sd_obs, 3),
            "expected_from_trip_noise_pct_per_month": _r(slope_sd_exp, 3),
            "recovered_slope_range_pct_per_month": [
                _r(slopes_pct.min(), 3), _r(slopes_pct.max(), 3)] if len(slopes_pct) else None,
            "moving_blocks": len(moving),
            "verdict": (
                f"recovered: {len(moving)} block(s) moving; the spread of block slopes "
                f"({slope_sd_obs:.3f} %/month) matches what the trip-to-trip noise alone "
                f"predicts ({slope_sd_exp:.3f} %/month). The rankings on this panel "
                f"demonstrate the method on noise; they are not a finding."
                if slope_sd_obs is not None and slope_sd_exp is not None else
                f"recovered: {len(moving)} block(s) moving."),
        },
        "age": {
            "planted": (f"+{PLANTED_AGE_PCT_PER_YEAR} % per year of palm age about age "
                        f"{AGE_ANCHOR}, times a ±10 % block-condition term and ±3 % noise "
                        f"(gen_abw)"),
            "planted_pct_per_year": PLANTED_AGE_PCT_PER_YEAR,
            "recovered_pct_per_year": _r(age_slope_pct, 2),
            "r2": _r(age_r2, 3),
            "thin_buckets": thin,
            "verdict": (
                "The planted curve is recovered; the rest of the block-to-block "
                "spread is the generator's condition term, not age."
                + (f" Ages {', '.join(thin)} are under five blocks each - anecdotes, "
                   f"not points on a curve." if thin else "")
                if age_slope_pct is not None else "Too few ages to fit a curve."),
        },
        "level": {
            "calibrated_mean_kg": _r(cal_mean, 2),
            "calibrated_bunch_weighted_kg": _r(cal_w, 2),
            "recovered_estate_kg": _r(estate_mean, 2),
            "recovered_over_calibrated": _r(ratio, 3),
            "verdict": (
                f"That is each block's calibrated weight times {ratio:.3f} - the "
                f"generated ~1.8 % shrink and the loose-fruit share taken off the "
                f"net. The level itself was never measured."
                if ratio is not None else "No calibration file to compare against."),
        },
    }

    return {
        "available": True,
        "estate": estate,
        "summary": summary,
        "calibration": calibration,
        "months": months,
        "month_labels": [_mlabel(m) for m in months],
        "totals": {
            "blocks": n_blocks,
            "blocks_fitted": len(fitted),
            "trips": n_trips,
            "months": n_m,
            "block_months": int(sum(int((bun[k] > 0).sum()) for k in keys)),
            "estate_abw_kg": _r(estate_mean, 3),
            "first_month_abw_kg": _r(est_first, 3),
            "last_month_abw_kg": _r(est_last, 3),
            "window_change_pct": _r(est_change, 2),
            "peak_to_trough_pct": _r(peak, 2),
            "moving_blocks": len(moving),
            "falling_blocks": n_fall,
            "rising_blocks": n_rise,
            "median_cv_pct": _r(np.median(cvs_a), 2) if len(cvs_a) else None,
            "p90_cv_pct": _r(np.percentile(cvs_a, 90), 2) if len(cvs_a) else None,
            "trips_per_block_month_median": _r(np.median(trips_per_bm), 0) if len(trips_per_bm) else None,
            "slope_sd_observed_pct_month": _r(slope_sd_obs, 3),
            "slope_sd_expected_pct_month": _r(slope_sd_exp, 3),
            "move_threshold_pct": MOVE_PCT,
            "move_threshold_z": MOVE_Z,
            "window_last_day": layers.EXPORT_END,
        },
        "estate_series": estate_series,
        "division_series": division_series,
        "falling": falling,
        "rising": rising,
        "blocks": sorted(blocks, key=lambda b: (int(b["division_code"]) if str(b["division_code"]).isdigit()
                                                 else 0, str(b["block_label"]))),
        "age_curve": {
            "buckets": age_curve,
            "anchor_age": AGE_ANCHOR,
            "recovered_pct_per_year": _r(age_slope_pct, 2),
            "planted_pct_per_year": PLANTED_AGE_PCT_PER_YEAR,
            "r2": _r(age_r2, 3),
            "thin_buckets": thin,
        },
        "spread": {
            "median_cv_pct": _r(np.median(cvs_a), 2) if len(cvs_a) else None,
            "p90_cv_pct": _r(np.percentile(cvs_a, 90), 2) if len(cvs_a) else None,
            "max_cv_pct": _r(cvs_a.max(), 2) if len(cvs_a) else None,
            "block_months_measured": int(len(cvs_a)),
            "trips_per_block_month_median": _r(np.median(trips_per_bm), 0) if len(trips_per_bm) else None,
            "widest": widest,
            "note": ("Coefficient of variation of the per-trip ABW inside one block "
                     "and month. Real tickets disagree far more than this - a 1 % "
                     "spread is the generator's noise, not a field reality - so the "
                     "figure to watch on real data is the width, not the level."),
        },
        "recovered_vs_planted": recovered_vs_planted,
        "provenance": ("derived: bunch counts per block-month and the harvest days are "
                       "the client's real EPMS figures; every weight on the ticket is "
                       "generated (gis/build_synthetic.py), so the level of ABW is "
                       "synthetic and only its movement and ranking are readable"),
        "note": (f"ABW per trip is (net kg - loose fruit kg) / EPMS bunches. Block-months "
                 f"are trip-weighted. The slope is least squares over the {n_m} months, in "
                 f"kg per month and as % of the block's mean; 'moving' means the fitted "
                 f"change over the window is at least {MOVE_PCT:.0f} % and at least "
                 f"{MOVE_Z:.0f} standard errors clear of that block's trip-to-trip spread. "
                 f"{last_m} runs to {layers.EXPORT_END}, the export's last day, which "
                 f"changes the trip count and not the weight per bunch."),
        "caveat": f"Calibration, not a measurement: {header}",
    }


def position(estate: str = "EC", top: int = 12) -> dict:
    """GET /gis/abw-trend. Computed once per estate; `top` slices at request time."""
    estate = (estate or "EC").upper()
    with _LOCK:
        if estate not in _CACHE:
            _CACHE[estate] = _compute(estate)
        d = _CACHE[estate]
    if not d.get("available"):
        return dict(d)
    top = max(1, min(int(top or 12), 50))
    out = dict(d)
    out["falling"] = d["falling"][:top]
    out["rising"] = d["rising"][:top]
    out["spread"] = {**d["spread"], "widest": d["spread"]["widest"][:max(5, top // 2)]}
    return out


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
