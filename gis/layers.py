"""Derived layers for Estate Command: the metrics the map can colour by.

Joins the real EC ontology (gis/ontology.py) to the synthetic feeds
(gis/data/synthetic/*.csv, written by gis/build_synthetic.py) and exposes one
value per block per metric.

Every metric declares its own provenance, because a demo that cannot tell the
client which numbers are theirs is worth nothing:

    real         computed only from the client's own data
    derived      real quantity transformed by a synthetic factor
                 (bunches are real, tonnes are bunches x a synthetic ABW)
    synthetic    no real input at all

The UI badges each metric with that word. Nothing here silently blends the two.
"""

import csv
import difflib
import logging
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from threading import Lock

from gis import environment, ontology, vegetation
from gis.build_synthetic import WINDOW_END as _EXPORT_END

log = logging.getLogger("estate-command.layers")

# The export's last day. A fertiliser issue dated after it is still a reservation.
EXPORT_END = _EXPORT_END.isoformat()

_DIR = Path(__file__).parent / "data" / "synthetic"
_CACHE: dict = {}
_LOCK = Lock()

# Landed-cost weights for vendor ranking (UC-09). Transport is per km per kg;
# the quality term prices OER away from a 21.5% reference; the reliability term
# charges for short deliveries and lateness.
TRANSPORT_IDR_KG_KM = 4.6
OER_REFERENCE = 21.5
OER_IDR_PER_POINT = 38.0
SHORTFALL_IDR = 900.0
LATE_IDR_PER_DAY = 22.0

REPLANT_AGE = 25          # oil palm is normally replanted at 25 years
IMMATURE_YEARS = 3        # years of no yield after replanting


def _key(division, block) -> str:
    return f"{int(str(division).strip())}|{int(str(block).strip())}"


def _read(name: str) -> list[dict]:
    """Read one synthetic CSV, skipping the leading '# SYNTHETIC.' note."""
    path = _DIR / name
    if not path.exists():
        log.warning("[layers] %s missing - run gis/build_synthetic.py", path)
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    return list(csv.DictReader(lines))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ── loading ────────────────────────────────────────────────────────────────

def _state() -> dict:
    with _LOCK:
        if "state" in _CACHE:
            return _CACHE["state"]

        abw = {_key(r["division_code"], r["block_code"]): _f(r["abw_kg"])
               for r in _read("ec_abw.csv")}

        cost = defaultdict(lambda: defaultdict(float))
        km_to_mill = {}
        for r in _read("ec_block_costs.csv"):
            k = _key(r["division_code"], r["block_code"])
            total = sum(_f(r[c]) or 0 for c in r if c.endswith("_idr"))
            cost[k][r["month"]] += total
            km_to_mill[k] = _f(r.get("km_to_mill"))

        fwd = defaultdict(dict)
        for r in _read("ec_forecast_forward.csv"):
            fwd[_key(r["division_code"], r["block_code"])][r["month"]] = {
                "p10": _i(r["p10"]), "p50": _i(r["p50"]), "p90": _i(r["p90"]),
                "horizon": _i(r["horizon"]),
            }

        rot = {_key(r["division_code"], r["block_code"]): {
            "gang_code": r["gang_code"],
            "target": _i(r["rotation_target_days"]),
            "last_harvest_date": r["last_harvest_date"],
            "days_since": _i(r["days_since_harvest"]),
        } for r in _read("ec_rotation.csv")}

        veg = defaultdict(dict)
        for r in _read("ec_vegetation.csv"):
            veg[_key(r["division_code"], r["block_code"])][r["month"]] = {
                "ndre": _f(r["ndre"]), "baseline": _f(r["ndre_baseline"]),
                "cloud_pct": _i(r["cloud_pct"]),
            }

        upkeep = defaultdict(dict)
        for r in _read("ec_upkeep.csv"):
            upkeep[_key(r["division_code"], r["block_code"])][r["activity"]] = {
                "days_overdue": _i(r["days_overdue"]),
                "days_since": _i(r["days_since"]),
                "interval": _i(r["interval_days"]),
                "last_done": r["last_done"],
            }

        # The trip ledger, aggregated to block and to block-month. 20k rows is
        # cheap to fold once at load and expensive to re-scan per request.
        trips_block: dict = defaultdict(
            lambda: {"trips": 0, "bunches": 0, "net_kg": 0.0, "expected_kg": 0.0,
                     "turnaround_h": 0.0, "queue_h": 0.0, "diesel_l": 0.0,
                     "ffa": 0.0, "dockage": 0.0, "loose_kg": 0.0, "km": 0.0})
        trips_month: dict = defaultdict(lambda: defaultdict(
            lambda: {"trips": 0, "net_kg": 0.0, "expected_kg": 0.0,
                     "turnaround_h": 0.0, "diesel_l": 0.0}))
        for r in _read("ec_weighbridge.csv"):
            k = _key(r["division_code"], r["block_code"])
            b = _i(r["bunches_epms"]) or 0
            w = abw.get(k) or 0
            exp = b * w
            net = _f(r["net_kg"]) or 0
            for bucket in (trips_block[k], trips_month[k][r["month"]]):
                bucket["trips"] += 1
                bucket["net_kg"] += net
                bucket["expected_kg"] += exp
                bucket["turnaround_h"] += _f(r["turnaround_h"]) or 0
                bucket["diesel_l"] += _f(r["diesel_l"]) or 0
            a = trips_block[k]
            a["bunches"] += b
            a["queue_h"] += _f(r["queue_h"]) or 0
            a["ffa"] += _f(r["ffa_pct"]) or 0
            a["dockage"] += _f(r["mill_dockage_pct"]) or 0
            a["loose_kg"] += _f(r["loose_fruit_kg"]) or 0
            a["km"] += _f(r["km_to_mill"]) or 0

        # Pest census, keyed by block, keeping the latest round for the map and
        # the whole series for the trend.
        pest_rounds: dict = defaultdict(dict)
        for r in _read("ec_pest_census.csv"):
            k = _key(r["division_code"], r["block_code"])
            insp = _i(r["palms_inspected"]) or 0
            pest_rounds[k][r["round"]] = {
                "round": r["round"], "date": r["census_date"],
                "palms_inspected": insp,
                "coverage_pct": _f(r["coverage_pct"]),
                "ganoderma_confirmed": _i(r["ganoderma_confirmed"]) or 0,
                "ganoderma_suspect": _i(r["ganoderma_suspect"]) or 0,
                "beetle_damaged": _i(r["beetle_damaged"]) or 0,
                "rat_damaged": _i(r["rat_damaged"]) or 0,
                "palms_felled": _i(r["palms_felled"]) or 0,
                "ganoderma_pct": round(100 * (_i(r["ganoderma_confirmed"]) or 0) / insp, 3) if insp else None,
                "beetle_pct": round(100 * (_i(r["beetle_damaged"]) or 0) / insp, 3) if insp else None,
                "rat_pct": round(100 * (_i(r["rat_damaged"]) or 0) / insp, 3) if insp else None,
            }

        treat: dict = defaultdict(list)
        for r in _read("ec_pest_treatment.csv"):
            treat[_key(r["division_code"], r["block_code"])].append({
                "treatment_id": r["treatment_id"], "pest": r["pest"],
                "method": r["method"], "treated_date": r["treated_date"],
                "palms_treated": _i(r["palms_treated"]),
                "followup_due": r["followup_due"],
                "days_overdue": _i(r["days_overdue"]) or 0,
            })

        # Fertiliser, folded to block totals plus the nutrient conversion. An
        # issue dated after the export's last day had not happened on that day:
        # it is a reservation still open, as the store's own record has it
        # (ec_mm_reservations.csv), so it counts as reserved, not issued, and its
        # lag is not yet known.
        fert: dict = defaultdict(lambda: {"target_kg": 0.0, "issued_kg": 0.0,
                                          "reserved_kg": 0.0, "lag_sum": 0.0,
                                          "n": 0, "palms": 0, "materials": {}})
        for r in _read("ec_fertiliser.csv"):
            k = _key(r["division_code"], r["block_code"])
            a = fert[k]
            late = r["issue_date"] > EXPORT_END
            a["target_kg"] += _f(r["target_kg"]) or 0
            a["reserved_kg" if late else "issued_kg"] += _f(r["issued_kg"]) or 0
            if not late:
                a["lag_sum"] += _i(r["application_lag_days"]) or 0
                a["n"] += 1
            a["palms"] = _i(r["palms"]) or a["palms"]
            m = a["materials"].setdefault(r["material"], {"target": 0.0, "issued": 0.0, "reserved": 0.0})
            m["target"] += _f(r["target_kg"]) or 0
            m["reserved" if late else "issued"] += _f(r["issued_kg"]) or 0

        roads = {}
        for r in _read("ec_roads.csv"):
            roads[_key(r["division_code"], r["block_code"])] = {
                "segment_id": r["segment_id"], "condition": r["condition"],
                "condition_score": _f(r["condition_score"]),
                "length_km": _f(r["length_km"]),
                "last_graded": r["last_graded"],
                "days_since_graded": _i(r["days_since_graded"]),
                "culvert_repairs_12m": _i(r["culvert_repairs_12m"]),
                "geom": [[_f(r["lon_a"]), _f(r["lat_a"])],
                         [_f(r["lon_b"]), _f(r["lat_b"])]],
            }

        # Worker output, folded to block. The per-worker rows stay on disk for
        # the productivity model; layers only needs the block aggregate.
        worker: dict = defaultdict(lambda: {"bunches": 0, "man_days": 0.0,
                                            "loose_kg": 0.0, "workers": set()})
        for r in _read("ec_harvester_day.csv"):
            a = worker[_key(r["division_code"], r["block_code"])]
            a["bunches"] += _i(r["bunches_cut"]) or 0
            a["man_days"] += _f(r["man_days"]) or 0
            a["loose_kg"] += _f(r["loose_fruit_kg"]) or 0
            a["workers"].add(r["worker_id"])

        _CACHE["state"] = {
            "pest": {k: dict(v) for k, v in pest_rounds.items()},
            "treatments": dict(treat),
            "fertiliser": dict(fert),
            "fert_stock": _read("ec_fertiliser_stock.csv"),
            "roads": roads,
            "worker": {k: {**v, "workers": len(v["workers"])}
                       for k, v in worker.items()},
            "abw": abw, "cost": cost, "km_to_mill": km_to_mill,
            "forward": fwd, "rotation": rot, "vegetation": veg, "upkeep": upkeep,
            "gangs": _read("ec_gangs.csv"),
            "vendors": _read("vendors.csv"),
            "orders": _read("sales_orders.csv"),
            "labour": _read("ec_labour.csv"),
            "vehicles": _read("ec_vehicles.csv"),
            "pm_orders": _read("ec_pm_orders.csv"),
            "trips_block": dict(trips_block),
            "trips_month": {k: dict(v) for k, v in trips_month.items()},
        }
        log.info("[layers] loaded synthetic feeds: %d blocks with ABW, %d vendors, "
                 "%d gangs", len(abw), len(_CACHE["state"]["vendors"]),
                 len(_CACHE["state"]["gangs"]))
        return _CACHE["state"]


def reload_layers() -> None:
    with _LOCK:
        _CACHE.clear()


def months(estate: str = "EC") -> dict:
    """Observed months (real harvest) and forward months (synthetic forecast)."""
    row = next((e for e in ontology.estate_index()
                if e["estate_code"] == estate.upper()), None)
    observed = (row or {}).get("harvest_window", {}).get("months", []) if row else []
    st = _state()
    fwd = sorted({m for b in st["forward"].values() for m in b})
    return {"observed": observed, "forward": fwd, "all": list(observed) + fwd}


# ── the metric catalogue ───────────────────────────────────────────────────

METRICS = {
    # real
    "bunches_per_ha":    ("Bunches per hectare", "real"),
    "bunches_total":     ("Bunches harvested", "real"),
    "peer_index":        ("Yield vs age-matched peers", "real"),
    "palm_age_years":    ("Palm age", "real"),
    "deduction_rate":    ("Grading deduction rate", "real"),
    "loose_per_bunch":   ("Loose fruit per bunch", "real"),
    # derived: real quantity x synthetic factor
    "tonnes":            ("Tonnes harvested", "derived"),
    "t_per_ha":          ("Tonnes per hectare", "derived"),
    "cost_per_kg":       ("Cost per kg FFB", "derived"),
    "margin_per_ha":     ("Margin per hectare", "derived"),
    # synthetic
    "forecast_p50":      ("Forecast bunches", "synthetic"),
    "forecast_band":     ("Forecast uncertainty", "synthetic"),
    "ripeness_pressure": ("Ripeness pressure", "synthetic"),
    "ndre":              ("Canopy vigour (NDRE)", "synthetic"),
    "ndre_anomaly":      ("Vigour anomaly vs peers", "synthetic"),
    "upkeep_overdue":    ("Upkeep days overdue", "synthetic"),
    "replant_year":      ("Replant year at age 25", "real"),
    # real, measured by us from public sources over the client's own polygons
    "slope_deg":         ("Terrain slope", "real"),
    "elevation_m":       ("Elevation", "real"),
    "canopy_roughness_m": ("Canopy roughness", "real"),
    # transport, from the synthetic trip ledger
    "shrinkage_pct":     ("Field-to-mill shrinkage", "synthetic"),
    "turnaround_h":      ("Turnaround to mill", "synthetic"),
    "diesel_l_per_tonne": ("Diesel per tonne", "synthetic"),
    "ffa_pct":           ("Free fatty acid at mill", "synthetic"),
    "loose_fruit_pct":   ("Loose fruit share", "synthetic"),
    # pest and disease, entirely synthetic - nothing in EPMS records a palm's health
    "ganoderma_pct":     ("Ganoderma incidence", "synthetic"),
    "beetle_pct":        ("Rhinoceros beetle damage", "synthetic"),
    "rat_pct":           ("Rat damage", "synthetic"),
    "treatment_overdue": ("Treatment follow-up overdue", "synthetic"),
    # nutrition and estate fabric
    "nutrient_gap_pct":  ("Nutrient shortfall vs programme", "synthetic"),
    "application_lag_days": ("Fertiliser application lag", "synthetic"),
    "kg_per_palm":       ("Fertiliser kg per palm", "synthetic"),
    "road_condition_score": ("Road condition", "synthetic"),
    # crew
    "bunches_per_man_day": ("Bunches per man-day", "synthetic"),
}

# Metrics where low is good, so the colour ramp must run the other way.
INVERTED = {"cost_per_kg", "upkeep_overdue", "ripeness_pressure", "forecast_band",
            "shrinkage_pct", "turnaround_h", "diesel_l_per_tonne", "ffa_pct",
            "canopy_roughness_m", "ganoderma_pct", "beetle_pct", "rat_pct",
            "treatment_overdue", "nutrient_gap_pct", "application_lag_days",
            "road_condition_score"}
# Metrics that diverge around a meaningful midpoint.
DIVERGING = {"peer_index": 1.0, "ndre_anomaly": 0.0, "margin_per_ha": 0.0}

# Vigour is the one metric whose provenance is not fixed at import: it reads
# synthetic until a Sentinel-2 scene has been pulled for the estate, and real
# afterwards. The badge on the map has to follow the data, not the table.
_SATELLITE_METRICS = ("ndre", "ndre_anomaly")


def metric_provenance(metric: str, estate: str = "EC") -> str:
    label_prov = METRICS.get(metric)
    if not label_prov:
        return "unknown"
    if metric in _SATELLITE_METRICS and vegetation.available(estate):
        return "real"
    return label_prov[1]


def metric_label(metric: str, estate: str = "EC") -> str:
    label = METRICS[metric][0]
    if metric in _SATELLITE_METRICS and vegetation.available(estate):
        scene = vegetation.scene(estate) or {}
        return f"{label} · {scene.get('date', '')}".strip(" ·")
    return label


def catalogue(estate: str = "EC") -> dict:
    """Every metric the map can colour by, with provenance resolved for real."""
    return {
        "metrics": [
            {"key": k,
             "label": metric_label(k, estate),
             "provenance": metric_provenance(k, estate),
             "inverted": k in INVERTED,
             "diverging_at": DIVERGING.get(k)}
            for k in METRICS
        ],
        "months": months(estate),
        "satellite": vegetation.scene(estate),
    }


def block_rows(estate: str = "EC", month: str | None = None) -> list[dict] | None:
    """One merged row per block: real attributes plus every derived value."""
    geo = ontology.blocks_geojson(estate)
    if geo is None:
        return None
    st = _state()
    mo = months(estate)
    is_forward = bool(month) and month in mo["forward"]
    # Real canopy vigour, when a Sentinel-2 scene has been pulled for this
    # estate. It is a single-date snapshot rather than a monthly series, so it
    # rides on every month and carries its own date - see the ndre_date field.
    real_ndre = vegetation.by_block(estate)
    real_month = vegetation.month(estate)
    # Terrain is a fixed property of the ground, so it rides on every month
    # unchanged. Empty until gis/build_terrain.py has been run.
    terrain = environment.terrain_by_block(estate)

    rows = []
    for f in geo["features"]:
        p = f["properties"]
        k = _key(p["division_code"], p["block_code"])
        ha = p.get("planted_ha")
        abw = st["abw"].get(k)

        if is_forward:
            fc = st["forward"].get(k, {}).get(month)
            bunches = fc["p50"] if fc else None
        elif month:
            bunches = (p.get("bunches_by_month") or {}).get(month)
        else:
            bunches = p.get("bunches_total")

        tonnes = (bunches * abw / 1000.0) if (bunches and abw) else None

        # Loose fruit per bunch, real: the export counts it on every record.
        # No forward value - a forecast month has no collection to measure.
        if is_forward:
            loose_pb = None
        elif month:
            lb = (p.get("loose_by_month") or {}).get(month)
            loose_pb = round(lb / bunches, 3) if (lb is not None and bunches) else None
        else:
            loose_pb = p.get("loose_per_bunch")

        if month:
            cost = st["cost"].get(k, {}).get(month)
        else:
            cost = sum(v for m, v in st["cost"].get(k, {}).items()
                       if m in mo["observed"]) or None

        veg = st["vegetation"].get(k, {}).get(month or mo["observed"][-1])
        rv = real_ndre.get(k)
        # A satellite that could not see the block through cloud has nothing
        # to say about it: null, never the synthetic value wearing a real badge.
        use_real = bool(rv) and rv.get("ndre") is not None
        rot = st["rotation"].get(k)
        up = st["upkeep"].get(k, {})
        fc_any = st["forward"].get(k, {}).get(month) if is_forward else None

        rows.append({
            "block_id": f["id"],
            "division_code": p["division_code"],
            "block_code": p["block_code"],
            "block_label": p.get("block_label"),
            "planted_ha": ha,
            "palms": p.get("palms"),
            "planted_year": p.get("planted_year"),
            "palm_age_years": p.get("palm_age_years"),
            "deduction_rate": p.get("deduction_rate"),
            "loose_per_bunch": loose_pb,
            "is_forecast": is_forward,
            "bunches": bunches,
            "abw_kg": abw,
            "tonnes": round(tonnes, 2) if tonnes else None,
            "t_per_ha": round(tonnes / ha, 2) if (tonnes and ha) else None,
            "cost_idr": round(cost) if cost else None,
            "cost_per_tonne": round(cost / tonnes) if (cost and tonnes) else None,
            # IDR per kg, which is how Indonesian planters quote cost and how
            # it compares directly against the FFB price.
            "cost_per_kg": round(cost / tonnes / 1000, 1) if (cost and tonnes) else None,
            "km_to_mill": st["km_to_mill"].get(k),
            "forecast": fc_any,
            "gang_code": rot["gang_code"] if rot else None,
            "days_since_harvest": rot["days_since"] if rot else None,
            "rotation_target_days": rot["target"] if rot else None,
            "last_harvest_date": rot["last_harvest_date"] if rot else None,
            "ndre": (rv["ndre"] if use_real else (veg["ndre"] if veg else None)),
            "ndre_source": ("real:sentinel-2" if use_real
                            else ("synthetic" if veg else None)),
            "ndre_date": (vegetation.scene(estate) or {}).get("date") if use_real else None,
            "ndre_month": real_month if use_real else (month or mo["observed"][-1]),
            "ndvi": rv["ndvi"] if use_real else None,
            "ndre_valid_pct": rv["valid_pct"] if rv else None,
            "ndre_baseline": veg["baseline"] if veg else None,
            "cloud_pct": (round(100 - rv["valid_pct"]) if rv
                          else (veg["cloud_pct"] if veg else None)),
            "upkeep": up,
            "upkeep_overdue": max((v["days_overdue"] for v in up.values()), default=None),
            "replant_year": ((p.get("planted_year") + REPLANT_AGE)
                             if p.get("planted_year") else None),
            # Real terrain, measured from the Copernicus DEM over the client's
            # own polygons. slope_deg is the landform figure; the raw surface
            # gradient over a palm canopy measures crowns, not ground.
            "slope_deg": (terrain.get(k) or {}).get("slope_deg"),
            "elevation_m": (terrain.get(k) or {}).get("elevation_m"),
            "relief_m": (terrain.get(k) or {}).get("relief_m"),
            "canopy_roughness_m": (terrain.get(k) or {}).get("canopy_roughness_m"),
            "terrain_source": "real:copernicus-dem" if terrain.get(k) else None,
            **_transport_values(st, k, month, is_forward),
            **_agronomy_values(st, k),
        })

    _add_relative(rows)
    return rows


_EMPTY_TRANSPORT = {"trips": None, "shrinkage_pct": None, "turnaround_h": None,
                    "diesel_l_per_tonne": None, "ffa_pct": None,
                    "loose_fruit_pct": None, "hauled_t": None, "queue_h": None,
                    "mill_dockage_pct": None}


def _transport_values(st, k, month, is_forward) -> dict:
    """Per-block haulage figures from the trip ledger.

    The ledger only covers recorded months, so a forward month has no trips
    and every value is null rather than a projection. Inventing haulage for a
    month that has not happened would put a synthetic number inside an already
    synthetic forecast and make the layer unreadable.
    """
    if is_forward:
        return dict(_EMPTY_TRANSPORT)
    src = (st["trips_month"].get(k, {}).get(month) if month
           else st["trips_block"].get(k))
    if not src or not src.get("trips"):
        return dict(_EMPTY_TRANSPORT)

    n = src["trips"]
    exp, net = src["expected_kg"], src["net_kg"]
    out = dict(_EMPTY_TRANSPORT)
    out["trips"] = n
    out["hauled_t"] = round(net / 1000, 2)
    out["turnaround_h"] = round(src["turnaround_h"] / n, 2)
    out["shrinkage_pct"] = round(100 * (exp - net) / exp, 2) if exp else None
    out["diesel_l_per_tonne"] = round(src["diesel_l"] / (net / 1000), 2) if net else None
    # The monthly bucket carries only what both buckets accumulate; the rest
    # are whole-window figures and are left null when a month is selected.
    if not month:
        out["ffa_pct"] = round(src["ffa"] / n, 2)
        out["queue_h"] = round(src["queue_h"] / n, 2)
        out["mill_dockage_pct"] = round(src["dockage"] / n, 2)
        out["loose_fruit_pct"] = round(100 * src["loose_kg"] / exp, 2) if exp else None
    return out


def _agronomy_values(st, k) -> dict:
    """Pest, nutrition, road and crew figures for one block.

    All of these are fixed-window rather than monthly: a census round, an
    application window, a road grading date. They do not move with the time
    scrubber and are not faked into moving.
    """
    out: dict = {}

    rounds = st["pest"].get(k) or {}
    latest = rounds.get(sorted(rounds)[-1]) if rounds else None
    first = rounds.get(sorted(rounds)[0]) if rounds else None
    out["pest_rounds"] = [rounds[r] for r in sorted(rounds)]
    out["ganoderma_pct"] = latest["ganoderma_pct"] if latest else None
    out["ganoderma_confirmed"] = latest["ganoderma_confirmed"] if latest else None
    out["ganoderma_suspect"] = latest["ganoderma_suspect"] if latest else None
    out["beetle_pct"] = latest["beetle_pct"] if latest else None
    out["rat_pct"] = latest["rat_pct"] if latest else None
    out["palms_inspected"] = latest["palms_inspected"] if latest else None
    out["census_coverage_pct"] = latest["coverage_pct"] if latest else None
    # Movement between the first and last round is the thing that decides
    # whether a block is a problem or a resolved one.
    out["ganoderma_trend_pct"] = (
        round(latest["ganoderma_pct"] - first["ganoderma_pct"], 3)
        if latest and first and latest["ganoderma_pct"] is not None
        and first["ganoderma_pct"] is not None else None)

    tr = st["treatments"].get(k) or []
    out["treatments"] = tr
    out["treatment_overdue"] = max((t["days_overdue"] for t in tr), default=None)

    fz = st["fertiliser"].get(k)
    if fz and fz["target_kg"]:
        out["fert_target_kg"] = round(fz["target_kg"], 1)
        out["fert_issued_kg"] = round(fz["issued_kg"], 1)
        out["fert_reserved_kg"] = round(fz["reserved_kg"], 1)
        out["nutrient_gap_pct"] = round(
            100 * (fz["target_kg"] - fz["issued_kg"]) / fz["target_kg"], 2)
        out["application_lag_days"] = round(fz["lag_sum"] / fz["n"], 1) if fz["n"] else None
        out["kg_per_palm"] = round(fz["issued_kg"] / fz["palms"], 3) if fz["palms"] else None
        out["fert_materials"] = fz["materials"]
    else:
        out.update({"fert_target_kg": None, "fert_issued_kg": None,
                    "nutrient_gap_pct": None, "application_lag_days": None,
                    "kg_per_palm": None, "fert_materials": {}})

    rd = st["roads"].get(k)
    out["road_condition"] = rd["condition"] if rd else None
    out["road_condition_score"] = rd["condition_score"] if rd else None
    out["road_days_since_graded"] = rd["days_since_graded"] if rd else None

    wk = st["worker"].get(k)
    out["man_days"] = round(wk["man_days"], 1) if wk else None
    out["harvesters"] = wk["workers"] if wk else None
    out["bunches_per_man_day"] = (round(wk["bunches"] / wk["man_days"], 1)
                                  if wk and wk["man_days"] else None)
    return out


def _add_relative(rows):
    """Values that only exist relative to a peer group, added in a second pass."""
    # Yield vs planting cohort.
    cohort = defaultdict(list)
    for r in rows:
        if r["bunches"] and r["planted_ha"]:
            cohort[r["planted_year"]].append(r["bunches"] / r["planted_ha"])
    med = {}
    for yr, vals in cohort.items():
        vals.sort()
        n = len(vals)
        med[yr] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    # Canopy vigour vs the same cohort, which is how UC-03 is meant to read:
    # an anomaly against age-matched peers, not against an absolute threshold.
    ndre_cohort = defaultdict(list)
    for r in rows:
        if r["ndre"]:
            ndre_cohort[r["planted_year"]].append(r["ndre"])
    ndre_med = {}
    for yr, vals in ndre_cohort.items():
        vals.sort()
        n = len(vals)
        ndre_med[yr] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    for r in rows:
        yph = (r["bunches"] / r["planted_ha"]) if (r["bunches"] and r["planted_ha"]) else None
        m = med.get(r["planted_year"])
        r["bunches_per_ha"] = round(yph, 1) if yph else None
        r["peer_index"] = round(yph / m, 3) if (yph and m) else None

        nm = ndre_med.get(r["planted_year"])
        r["ndre_anomaly"] = (round(r["ndre"] - nm, 4) if (r["ndre"] and nm) else None)

        r["bunches_total"] = r["bunches"]
        r["forecast_p50"] = r["forecast"]["p50"] if r["forecast"] else None
        r["forecast_band"] = (round((r["forecast"]["p90"] - r["forecast"]["p10"])
                                    / max(r["forecast"]["p50"], 1), 3)
                              if r["forecast"] else None)
        # Ripeness pressure: how far through its round the block is. Above 1.0
        # means overdue. This is the T0 half of UC-01 - it says which blocks are
        # ready, never which gang should go, since gang capacity is invented.
        r["ripeness_pressure"] = (round(r["days_since_harvest"] / r["rotation_target_days"], 2)
                                  if r["days_since_harvest"] and r["rotation_target_days"]
                                  else None)
        # Margin needs a price; FFB farmgate is a scenario input, not client data.
        if r["tonnes"] and r["cost_idr"] and r["planted_ha"]:
            revenue = r["tonnes"] * 1000 * 2600
            # Million IDR per hectare over the selected window. Raw rupiah per
            # hectare runs to eight digits and is unreadable on a legend.
            r["margin_per_ha"] = round((revenue - r["cost_idr"]) / r["planted_ha"] / 1e6, 2)
        else:
            r["margin_per_ha"] = None


def metric_values(estate: str, metric: str, month: str | None = None) -> dict | None:
    """Per-block values for one metric, in the shape the map expects."""
    if metric not in METRICS:
        return None
    mo_all = months(estate)
    # The forecast metrics only exist on forward months. Asking for one without
    # a month is a reasonable thing to do, so land on the first forward month
    # rather than returning an empty map.
    if metric.startswith("forecast") and month not in mo_all["forward"]:
        month = mo_all["forward"][0] if mo_all["forward"] else month
    rows = block_rows(estate, month)
    if rows is None:
        return None
    label = metric_label(metric, estate)
    provenance = metric_provenance(metric, estate)
    values = {r["block_id"]: r.get(metric) for r in rows}
    nums = sorted(v for v in values.values() if isinstance(v, (int, float)))
    mo = months(estate)
    return {
        "estate": estate.upper(),
        "metric": metric,
        "label": label,
        "provenance": provenance,
        "month": month,
        "is_forecast": bool(month) and month in mo["forward"],
        "months": mo["observed"],
        "forward_months": mo["forward"],
        "inverted": metric in INVERTED,
        "diverging_at": DIVERGING.get(metric),
        "values": values,
        "domain": [nums[0], nums[-1]] if nums else None,
        "median": (nums[len(nums) // 2] if nums else None),
        "n": len(nums),
    }


# ── metric agreement ───────────────────────────────────────────────────────

def _spearman(pairs) -> float | None:
    """Rank correlation of two equal-length series, ties averaged.

    Rank rather than Pearson because none of these metrics is normally
    distributed and a handful of extreme blocks would otherwise carry the
    coefficient on their own. No scipy: this is thirty lines and the estate
    has 291 blocks, not 291 million.
    """
    n = len(pairs)
    if n < 8:
        return None

    def ranks(values):
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra = ranks([p[0] for p in pairs])
    rb = ranks([p[1] for p in pairs])
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(cov / (va * vb), 3) if va and vb else None


def _strength(rho: float | None) -> str:
    if rho is None:
        return "too few blocks carry both values to say anything."
    a = abs(rho)
    direction = "together" if rho > 0 else "in opposite directions"
    if a < 0.2:
        return "effectively no relationship: the two metrics rank the estate independently."
    if a < 0.4:
        return f"a weak tendency to move {direction}."
    if a < 0.6:
        return f"a moderate tendency to move {direction}."
    return f"a strong tendency to move {direction}."


def unknown_metric(metric: str) -> dict:
    """The error a caller can act on: what was wrong, and what to call instead.

    An agent that receives "unknown metric" plus a list of sixteen keys tends
    to give up and tell the user the thing is unavailable. One that receives
    "did you mean bunches_per_ha" corrects itself and carries on, which is the
    difference between an answer and a dead end.
    """
    close = difflib.get_close_matches(metric or "", list(METRICS), n=3, cutoff=0.3)
    return {
        "error": f"Unknown metric {metric!r}.",
        "did_you_mean": close,
        "available_metrics": [{"key": k, "label": v[0]} for k, v in METRICS.items()],
        "recovery": ("Call again with one of the keys above. There is no yield "
                     "in tonnes: the export carries bunch counts, so yield is "
                     "bunches_per_ha and relative yield is peer_index."),
    }


def compare_metrics(estate: str, metric_a: str, metric_b: str,
                    month: str | None = None, n: int = 8) -> dict:
    """Do two metrics agree about which blocks are the problem?

    The question behind every anomaly layer: a block that looks weak on
    satellite vigour and weak on recorded yield is a different proposition
    from one that looks weak on only one of them. The first is corroborated
    and worth a visit; the second is a measurement artefact until proven
    otherwise.

    Returns the rank correlation plus the three interesting sets: weak on
    both, weak on A only, weak on B only, where weak means the bottom fifth.
    """
    for m in (metric_a, metric_b):
        if m not in METRICS:
            return unknown_metric(m)
    rows = block_rows(estate, month)
    if rows is None:
        return {"error": f"No block data for estate {estate!r}."}

    have = [r for r in rows
            if isinstance(r.get(metric_a), (int, float))
            and isinstance(r.get(metric_b), (int, float))]
    if not have:
        return {"error": "No block carries both metrics.",
                "metric_a": metric_a, "metric_b": metric_b,
                "note": "One of these is probably only defined on forecast months."}

    rho = _spearman([(r[metric_a], r[metric_b]) for r in have])
    cut = max(1, len(have) // 5)
    low_a = {r["block_id"] for r in sorted(have, key=lambda r: r[metric_a])[:cut]}
    low_b = {r["block_id"] for r in sorted(have, key=lambda r: r[metric_b])[:cut]}

    def pack(ids, order_by):
        picked = [r for r in have if r["block_id"] in ids]
        picked.sort(key=lambda r: r[order_by])
        return [{"block_label": r["block_label"], "division_code": r["division_code"],
                 "planted_year": r["planted_year"], "planted_ha": r["planted_ha"],
                 metric_a: r[metric_a], metric_b: r[metric_b]} for r in picked[:n]]

    both = low_a & low_b
    return {
        "estate": estate.upper(),
        "metric_a": {"key": metric_a, "label": metric_label(metric_a, estate),
                     "provenance": metric_provenance(metric_a, estate)},
        "metric_b": {"key": metric_b, "label": metric_label(metric_b, estate),
                     "provenance": metric_provenance(metric_b, estate)},
        "month": month or "full recorded window",
        "blocks_with_both": len(have),
        "spearman_rho": rho,
        "reading": _strength(rho),
        "bottom_fifth_size": cut,
        "weak_on_both": pack(both, metric_a),
        "weak_on_both_count": len(both),
        "weak_on_a_only": pack(low_a - low_b, metric_a),
        "weak_on_b_only": pack(low_b - low_a, metric_b),
        "note": ("Weak means the bottom fifth by raw value. For a metric where "
                 "low is good (cost, days overdue, ripeness pressure) read "
                 "'weak' as 'lowest', not 'worst'. Agreement between two "
                 "independent measurements is evidence; a block weak on only "
                 "one of them is a lead, not a finding."),
    }


# ── estate-level panels ────────────────────────────────────────────────────

def contract_position(estate: str = "EC") -> dict:
    """UC-08: production against committed volume, month by month."""
    st = _state()
    mo = months(estate)
    orders = defaultdict(lambda: {"tonnes": 0, "orders": [], "price": []})
    for o in st["orders"]:
        e = orders[o["month"]]
        e["tonnes"] += _i(o["committed_tonnes"]) or 0
        e["orders"].append(o["order_id"])
        e["price"].append(_f(o["price_idr_per_kg"]) or 0)

    out = []
    for m in mo["all"]:
        rows = block_rows(estate, m) or []
        produced = sum(r["tonnes"] or 0 for r in rows)
        band = None
        if m in mo["forward"]:
            lo = sum((r["forecast"]["p10"] * (r["abw_kg"] or 0) / 1000)
                     for r in rows if r["forecast"] and r["abw_kg"])
            hi = sum((r["forecast"]["p90"] * (r["abw_kg"] or 0) / 1000)
                     for r in rows if r["forecast"] and r["abw_kg"])
            band = [round(lo, 1), round(hi, 1)]
        c = orders.get(m)
        committed = c["tonnes"] if c else 0
        out.append({
            "month": m,
            "is_forecast": m in mo["forward"],
            "produced_t": round(produced, 1),
            "band_t": band,
            "committed_t": committed,
            "gap_t": round(produced - committed, 1),
            # A P50 surplus with a P10 deficit is still a purchasing decision,
            # which is exactly why the band matters more than the point.
            "gap_p10_t": round(band[0] - committed, 1) if band else None,
            "orders": c["orders"] if c else [],
            "price_idr_kg": round(sum(c["price"]) / len(c["price"])) if c and c["price"] else None,
        })
    # Headline on the P50 gap. Ranking forward months by their P10 gap always
    # returns the furthest month, because the band widens with horizon - that
    # is a property of the interval, not a signal about that month.
    fwd_rows = [r for r in out if r["is_forecast"]]
    worst = min(fwd_rows, key=lambda r: r["gap_t"], default=None)
    first_deficit = next((r for r in fwd_rows if r["gap_t"] < 0), None)
    p10_deficit = [r for r in fwd_rows if r["gap_p10_t"] is not None and r["gap_p10_t"] < 0]
    return {
        "estate": estate.upper(),
        "months": out,
        "worst_forward_month": worst,
        "first_deficit_month": first_deficit,
        "months_in_p10_deficit": len(p10_deficit),
        "months_in_p50_deficit": sum(1 for r in fwd_rows if r["gap_t"] < 0),
        "provenance": "derived: real bunches x synthetic ABW, against synthetic orders",
        "note": ("Tonnage uses the calibrated bunch weight, and the commitments "
                 "are invented. The shape of the gap is the point, not the level."),
    }


def vendor_ranking(shortfall_t: float | None = None) -> dict:
    """UC-09: rank vendors by landed cost at mill, not headline price."""
    st = _state()
    ranked = []
    for v in st["vendors"]:
        price = _f(v["price_idr_per_kg"]) or 0
        dist = _f(v["distance_km"]) or 0
        oer = _f(v["oer_pct"]) or OER_REFERENCE
        fill = _f(v["fill_rate"]) or 1.0
        late = _f(v["avg_days_late"]) or 0
        transport = dist * TRANSPORT_IDR_KG_KM
        quality = (OER_REFERENCE - oer) * OER_IDR_PER_POINT
        reliability = (1 - fill) * SHORTFALL_IDR + late * LATE_IDR_PER_DAY
        ranked.append({
            "vendor_code": v["vendor_code"], "name": v["name"],
            "lat": _f(v["lat"]), "lon": _f(v["lon"]),
            "distance_km": dist, "price_idr_per_kg": round(price),
            "transport_idr_per_kg": round(transport),
            "quality_adj_idr_per_kg": round(quality),
            "reliability_adj_idr_per_kg": round(reliability),
            "landed_idr_per_kg": round(price + transport + quality + reliability),
            "oer_pct": oer, "fill_rate": fill, "avg_days_late": late,
            "capacity_t_month": _i(v["capacity_t_month"]),
        })
    ranked.sort(key=lambda r: r["landed_idr_per_kg"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    allocation = []
    if shortfall_t and shortfall_t > 0:
        remaining = shortfall_t
        for r in ranked:
            if remaining <= 0:
                break
            # Expected delivery, not nominal capacity: a vendor that fills 80%
            # of what it promises has 80% of the capacity it advertises.
            usable = (r["capacity_t_month"] or 0) * r["fill_rate"]
            take = min(remaining, usable)
            if take <= 0:
                continue
            allocation.append({
                "vendor_code": r["vendor_code"], "name": r["name"],
                "tonnes": round(take, 1),
                "cost_idr": round(take * 1000 * r["landed_idr_per_kg"]),
                "landed_idr_per_kg": r["landed_idr_per_kg"],
            })
            remaining -= take
        if remaining > 0:
            allocation.append({"vendor_code": None, "name": "UNFILLED",
                               "tonnes": round(remaining, 1), "cost_idr": None,
                               "landed_idr_per_kg": None})

    cheapest = min(ranked, key=lambda r: r["price_idr_per_kg"]) if ranked else None
    return {
        "vendors": ranked,
        "allocation": allocation,
        "shortfall_t": shortfall_t,
        # The total the allocation actually costs. Present so that no caller -
        # a panel, an agent, a reader - ever has to derive it, which is the
        # one thing the numeric guardrail forbids.
        "allocation_total_idr": (round(sum(a["cost_idr"] for a in allocation))
                                 if allocation else None),
        "allocation_tonnes": (round(sum(a["tonnes"] for a in allocation), 1)
                              if allocation else None),
        "cheapest_headline": cheapest["name"] if cheapest else None,
        "best_landed": ranked[0]["name"] if ranked else None,
        "cheapest_is_best": (cheapest["name"] == ranked[0]["name"]) if ranked else None,
        "formula": ("landed = price + transport(distance) + quality(OER vs 21.5%) "
                    "+ reliability(fill rate, lateness)"),
        "provenance": "synthetic: ZEPMS_EM_VENDOR_OUT has no address and no coordinates",
    }


def labour_position(estate: str = "EC") -> dict:
    """UC-12: harvester supply against demand, by division and month."""
    st = _state()
    rows = [{
        "division_code": r["division_code"], "month": r["month"],
        "planted_ha": _f(r["planted_ha"]),
        "required": _i(r["harvesters_required"]),
        "on_roll": _i(r["harvesters_on_roll"]),
        "attendance_rate": _f(r["attendance_rate"]),
        "effective": _i(r["harvesters_effective"]),
        "deficit": _i(r["deficit"]),
    } for r in st["labour"]]
    by_month = defaultdict(int)
    for r in rows:
        by_month[r["month"]] += max(0, r["deficit"] or 0)
    worst = max(by_month.items(), key=lambda kv: kv[1]) if by_month else None
    return {
        "estate": estate.upper(), "rows": rows,
        "deficit_by_month": dict(sorted(by_month.items())),
        "worst_month": worst[0] if worst else None,
        "worst_deficit": worst[1] if worst else None,
        "gangs": len(st["gangs"]),
        "provenance": "synthetic: EPMS has t_attendance, the EC export carries none",
        "note": ("Ramadan and Lebaran fell in March-April 2025. That migration is "
                 "the largest predictable labour shock in the Indonesian calendar "
                 "and it is modelled here explicitly."),
    }


def replant_schedule(estate: str = "EC") -> dict:
    """UC-11 on real ages: when does this estate hit its replanting cliff.

    EC's planting years run 2015-2019 only, so there is nothing to replant now.
    That uniformity is itself the finding: the entire estate reaches replant age
    inside a five-year window, and replanting it on schedule would take the
    whole estate out of production at once. Real data, real problem.
    """
    rows = block_rows(estate) or []
    by_year = defaultdict(lambda: {"blocks": 0, "ha": 0.0, "palms": 0})
    for r in rows:
        y = r["replant_year"]
        if not y:
            continue
        e = by_year[y]
        e["blocks"] += 1
        e["ha"] += r["planted_ha"] or 0
        e["palms"] += r["palms"] or 0

    total_ha = sum(v["ha"] for v in by_year.values())
    schedule = [{
        "replant_year": y,
        "blocks": v["blocks"],
        "ha": round(v["ha"], 1),
        "palms": v["palms"],
        "pct_of_estate": round(100 * v["ha"] / total_ha, 1) if total_ha else 0,
        # Three immature years after replanting, so the production hole opens
        # the year of replant and closes three years later.
        "immature_until": y + IMMATURE_YEARS,
    } for y, v in sorted(by_year.items())]

    peak = max(schedule, key=lambda s: s["ha"]) if schedule else None
    span = (schedule[-1]["replant_year"] - schedule[0]["replant_year"]) if schedule else 0
    return {
        "estate": estate.upper(),
        "replant_age": REPLANT_AGE,
        "immature_years": IMMATURE_YEARS,
        "schedule": schedule,
        "window_years": span + 1,
        "peak_year": peak["replant_year"] if peak else None,
        "peak_pct": peak["pct_of_estate"] if peak else None,
        "total_ha": round(total_ha, 1),
        "provenance": "real: planting years from the client's ArcGIS attributes",
        "finding": (f"All {len(rows)} blocks reach replant age within "
                    f"{span + 1} years. Replanting on schedule would take "
                    f"{peak['pct_of_estate'] if peak else 0}% of the estate out of "
                    "production in a single year, with three immature years behind "
                    "it. The schedule has to be deliberately staggered."),
    }


def transport_position(estate: str = "EC") -> dict:
    """The Transport domain: haulage efficiency, fleet reliability, quality decay.

    Everything here comes from the trip ledger, which EPMS and SAP hold in
    three separate places and the EC export carries none of. The block bunch
    counts inside it are real; the trips, weights, vehicles and times are not.
    """
    st = _state()
    rows = block_rows(estate) or []
    trips = st["trips_block"]
    if not trips:
        return {"available": False,
                "reason": "No trip ledger. Run python gis/build_synthetic.py."}

    tot_trips = sum(v["trips"] for v in trips.values())
    tot_net = sum(v["net_kg"] for v in trips.values())
    tot_exp = sum(v["expected_kg"] for v in trips.values())
    tot_diesel = sum(v["diesel_l"] for v in trips.values())
    tot_turn = sum(v["turnaround_h"] for v in trips.values())
    tot_queue = sum(v["queue_h"] for v in trips.values())

    # Fleet reliability by class. MTBF in trips is the figure a workshop
    # manager recognises; hours of downtime is what it costs the estate.
    by_class: dict = defaultdict(lambda: {"vehicles": 0, "trips": 0,
                                          "failures": 0, "downtime_h": 0.0,
                                          "cost_idr": 0.0})
    veh_class = {v["vehicle_id"]: v["vehicle_class"] for v in st["vehicles"]}
    for v in st["vehicles"]:
        by_class[v["vehicle_class"]]["vehicles"] += 1
    trips_by_vehicle: dict = defaultdict(int)
    for r in _read("ec_weighbridge.csv"):
        trips_by_vehicle[r["vehicle_id"]] += 1
    for vid, n in trips_by_vehicle.items():
        if vid in veh_class:
            by_class[veh_class[vid]]["trips"] += n
    for o in st["pm_orders"]:
        c = by_class[o["vehicle_class"]]
        c["failures"] += 1
        c["downtime_h"] += _f(o["downtime_h"]) or 0
        c["cost_idr"] += _f(o["cost_idr"]) or 0

    fleet = []
    for cls, v in sorted(by_class.items()):
        fleet.append({
            "vehicle_class": cls,
            "vehicles": v["vehicles"],
            "trips": v["trips"],
            "failures": v["failures"],
            # Mean trips between failures. Null rather than infinity for a
            # class that never failed, so nothing downstream divides by it.
            "mtbf_trips": round(v["trips"] / v["failures"]) if v["failures"] else None,
            "downtime_h": round(v["downtime_h"], 1),
            "downtime_cost_idr": round(v["cost_idr"]),
        })

    worst = [r for r in rows if r.get("shrinkage_pct") is not None]
    worst.sort(key=lambda r: -r["shrinkage_pct"])
    slowest = [r for r in rows if r.get("turnaround_h") is not None]
    slowest.sort(key=lambda r: -r["turnaround_h"])

    return {
        "available": True,
        "estate": estate.upper(),
        "totals": {
            "trips": tot_trips,
            "hauled_t": round(tot_net / 1000, 1),
            "expected_t": round(tot_exp / 1000, 1),
            "shrinkage_t": round((tot_exp - tot_net) / 1000, 1),
            "shrinkage_pct": round(100 * (tot_exp - tot_net) / tot_exp, 2) if tot_exp else None,
            "diesel_l": round(tot_diesel),
            "diesel_l_per_tonne": round(tot_diesel / (tot_net / 1000), 2) if tot_net else None,
            "mean_turnaround_h": round(tot_turn / tot_trips, 2) if tot_trips else None,
            "mean_queue_h": round(tot_queue / tot_trips, 2) if tot_trips else None,
            # Queue is the part of the cycle the estate can fix without buying
            # a vehicle, so its share of turnaround is the actionable number.
            "queue_share_pct": round(100 * tot_queue / tot_turn, 1) if tot_turn else None,
        },
        "fleet": fleet,
        "worst_shrinkage": [{"block_label": r["block_label"],
                             "division_code": r["division_code"],
                             "trips": r["trips"],
                             "shrinkage_pct": r["shrinkage_pct"],
                             "hauled_t": r["hauled_t"]} for r in worst[:10]],
        "slowest_turnaround": [{"block_label": r["block_label"],
                                "division_code": r["division_code"],
                                "km_to_mill": r["km_to_mill"],
                                "turnaround_h": r["turnaround_h"],
                                "queue_h": r["queue_h"]} for r in slowest[:10]],
        "provenance": ("synthetic: the trip ledger, fleet and work orders are "
                       "generated. Block bunch counts and harvest-day counts "
                       "inside them are the client's real figures."),
        "note": ("EPMS holds the field count, SAP PM holds the vehicle and the "
                 "fuel, and the mill holds the weight. No single system joins "
                 "them today, which is why none of this is currently visible."),
    }


def pest_position(estate: str = "EC", top: int = 12) -> dict:
    """The Pest & Disease domain: incidence, spread risk, treatment coverage.

    Entirely invented, and the panel says so in the first line. No table in the
    138-table EPMS schema can hold an infected palm, and the only proxy in the
    export - grading deductions - is degenerate at 0.25% mean across all 291
    blocks. The client currently has no visibility into pest pressure at all.
    """
    rows = block_rows(estate) or []
    have = [r for r in rows if r.get("ganoderma_pct") is not None]
    if not have:
        return {"available": False,
                "reason": "No pest census. Run python gis/build_synthetic.py."}

    insp = sum(r["palms_inspected"] or 0 for r in have)
    conf = sum(r["ganoderma_confirmed"] or 0 for r in have)
    susp = sum(r["ganoderma_suspect"] or 0 for r in have)

    worst = sorted(have, key=lambda r: -(r["ganoderma_pct"] or 0))
    rising = sorted([r for r in have if r.get("ganoderma_trend_pct") is not None],
                    key=lambda r: -(r["ganoderma_trend_pct"] or 0))
    overdue = sorted([r for r in rows if r.get("treatment_overdue")],
                     key=lambda r: -(r["treatment_overdue"] or 0))

    # Spread risk: a clean block beside an infected one is the block to protect.
    risk = _spread_risk(have, _centroids(estate))

    def pack(rs, *fields):
        return [{**{"block_label": r["block_label"],
                    "division_code": r["division_code"]},
                 **{f: r.get(f) for f in fields}} for r in rs[:top]]

    return {
        "available": True,
        "estate": estate.upper(),
        "census": {
            "blocks": len(have),
            "palms_inspected": insp,
            "ganoderma_confirmed": conf,
            "ganoderma_suspect": susp,
            "incidence_pct": round(100 * conf / insp, 2) if insp else None,
            "suspect_pct": round(100 * susp / insp, 2) if insp else None,
            "mean_coverage_pct": round(
                sum(r["census_coverage_pct"] or 0 for r in have) / len(have), 1),
            "rounds": len(have[0].get("pest_rounds") or []),
        },
        "worst_blocks": pack(worst, "ganoderma_pct", "ganoderma_confirmed",
                             "palms_inspected", "ganoderma_trend_pct"),
        "fastest_rising": pack(rising, "ganoderma_trend_pct", "ganoderma_pct"),
        "spread_risk": risk[:top],
        "treatment_overdue": pack(overdue, "treatment_overdue"),
        "blocks_over_5pct": sum(1 for r in have if (r["ganoderma_pct"] or 0) > 5),
        "blocks_untreated": sum(1 for r in rows if not r.get("treatments")),
        "provenance": "synthetic: the entire domain is invented",
        "note": ("Ganoderma is generated as decaying foci rather than scattered "
                 "at random, because basal stem rot spreads from soil inoculum "
                 "and a random map would be visibly false. The shape is "
                 "realistic; the numbers are not the client's."),
    }


def _centroids(estate: str = "EC") -> dict:
    """{block_id: (lon, lat)} from the real polygons."""
    geo = ontology.blocks_geojson(estate)
    if geo is None:
        return {}
    out = {}
    for f in geo["features"]:
        ring = f["geometry"]["coordinates"][0]
        n = len(ring) - 1
        out[f["id"]] = (sum(p[0] for p in ring[:n]) / n,
                        sum(p[1] for p in ring[:n]) / n)
    return out


def _spread_risk(rows, centroids, k: int = 6) -> list[dict]:
    """Clean blocks sitting next to infected ones, ranked by neighbour pressure.

    The operational question a census cannot answer on its own: not where the
    disease is, but where it goes next. Neighbours are the k nearest by
    centroid, which on a regular block grid is a fair stand-in for adjacency
    and avoids a polygon-topology dependency for a demo layer.
    """
    pts = [(r, centroids[r["block_id"]]) for r in rows
           if r["block_id"] in centroids]
    if not pts:
        return []
    out = []
    for r, (x, y) in pts:
        # Sort on distance only; the row is carried alongside because dicts
        # are not orderable and a tie would otherwise raise.
        d = sorted(((x - bx) ** 2 + (y - by) ** 2, i)
                   for i, (other, (bx, by)) in enumerate(pts) if other is not r)
        near = [pts[i][0] for _, i in d[:k]]
        pressure = sum(o.get("ganoderma_pct") or 0 for o in near) / max(len(near), 1)
        own = r.get("ganoderma_pct") or 0
        out.append({
            "block_label": r["block_label"],
            "division_code": r["division_code"],
            "ganoderma_pct": round(own, 3),
            "neighbour_mean_pct": round(pressure, 3),
            # High neighbour pressure on a block that is still clean is the
            # thing worth acting on. A block already infected is a treatment
            # problem, not a containment one.
            "exposure": round(pressure - own, 3),
        })
    out.sort(key=lambda r: -r["exposure"])
    return out


def nutrition_position(estate: str = "EC", top: int = 12) -> dict:
    """The nutrition half of Upkeep: applied against programme, and how late.

    The cost ledger already carries a fertiliser line in rupiah. That cannot
    answer an agronomic question, which is asked in kilograms of nutrient per
    palm against a recommendation.
    """
    st = _state()
    rows = block_rows(estate) or []
    have = [r for r in rows if r.get("nutrient_gap_pct") is not None]
    if not have:
        return {"available": False,
                "reason": "No fertiliser feed. Run python gis/build_synthetic.py."}

    target = sum(r["fert_target_kg"] or 0 for r in have)
    issued = sum(r["fert_issued_kg"] or 0 for r in have)
    reserved = sum(r.get("fert_reserved_kg") or 0 for r in have)
    by_material: dict = defaultdict(lambda: {"target": 0.0, "issued": 0.0, "reserved": 0.0})
    for r in have:
        for m, v in (r.get("fert_materials") or {}).items():
            by_material[m]["target"] += v["target"]
            by_material[m]["issued"] += v["issued"]
            by_material[m]["reserved"] += v.get("reserved", 0.0)

    worst = sorted(have, key=lambda r: -(r["nutrient_gap_pct"] or 0))
    latest = sorted(have, key=lambda r: -(r["application_lag_days"] or 0))

    # The store's own view, where the MM records exist: learned reorder point and
    # order-by date beside the days-of-cover flag this table used to stop at.
    store_rows = {}
    try:
        from gis import stores
        ov = stores.overview("FERT")
        if ov.get("available"):
            store_rows = {r["maktx"]: r for r in ov["materials"]}
    except Exception as exc:          # the stock table never fails for want of the store
        log.warning("[layers] stores unavailable for nutrition: %s", exc)

    stock = []
    for s in st["fert_stock"]:
        prog = _f(s["annual_programme_kg"]) or 0
        on_hand = _f(s["stock_on_hand_kg"]) or 0
        lead = _i(s["lead_time_days"]) or 0
        # Days of cover at the average daily draw implied by the programme.
        daily = prog / 365.0
        cover = on_hand / daily if daily else None
        stock.append({
            "material": s["material"],
            "annual_programme_kg": round(prog),
            "stock_on_hand_kg": round(on_hand),
            "lead_time_days": lead,
            "days_cover": round(cover) if cover else None,
            # The figure that matters: will the store run dry before a
            # replacement order can arrive?
            "short_before_resupply": bool(cover is not None and cover < lead),
        })
        sr = store_rows.get(s["material"])
        if sr:
            stock[-1].update({"matnr": sr["matnr"], "status": sr["status"], "order_by": sr["order_by"],
                              "reorder_point_kg": sr["reorder_point"], "order_qty_kg": sr["order_qty"],
                              "headline": sr["plain"]["headline"]})

    return {
        "available": True,
        "estate": estate.upper(),
        "totals": {
            "blocks": len(have),
            "target_kg": round(target),
            "issued_kg": round(issued),
            "reserved_kg": round(reserved),
            "gap_kg": round(target - issued),
            "gap_pct": round(100 * (target - issued) / target, 2) if target else None,
            "mean_lag_days": round(
                sum(r["application_lag_days"] or 0 for r in have) / len(have), 1),
            "blocks_under_80pct": sum(1 for r in have if (r["nutrient_gap_pct"] or 0) > 20),
        },
        "by_material": [{"material": m,
                         "target_kg": round(v["target"]),
                         "issued_kg": round(v["issued"]),
                         "reserved_kg": round(v["reserved"]),
                         "gap_pct": round(100 * (v["target"] - v["issued"]) / v["target"], 2)
                         if v["target"] else None}
                        for m, v in sorted(by_material.items())],
        "stock": stock,
        "worst_blocks": [{"block_label": r["block_label"],
                          "division_code": r["division_code"],
                          "nutrient_gap_pct": r["nutrient_gap_pct"],
                          "kg_per_palm": r["kg_per_palm"],
                          "bunches_per_ha": r.get("bunches_per_ha")}
                         for r in worst[:top]],
        "latest_applications": [{"block_label": r["block_label"],
                                 "division_code": r["division_code"],
                                 "application_lag_days": r["application_lag_days"]}
                                for r in latest[:top]],
        "provenance": "synthetic: SAP MM holds the goods issues; the export has none",
        "warning": ("Weak blocks are PRESCRIBED more fertiliser, because that is "
                    "how an agronomist assigns a programme. Reading this layer "
                    "as 'more fertiliser, lower yield' inverts cause and effect, "
                    "and it is exactly the confounding the clustering panel has "
                    "to control for."),
    }


def roads_position(estate: str = "EC", top: int = 12) -> dict:
    """Road condition and the grading backlog, with what it costs in haulage."""
    st = _state()
    rows = block_rows(estate) or []
    have = [r for r in rows if r.get("road_condition_score") is not None]
    if not have:
        return {"available": False,
                "reason": "No road feed. Run python gis/build_synthetic.py."}

    by_cond: dict = defaultdict(lambda: {"segments": 0, "km": 0.0})
    for k, rd in st["roads"].items():
        c = by_cond[rd["condition"]]
        c["segments"] += 1
        c["km"] += rd["length_km"] or 0

    worst = sorted(have, key=lambda r: -(r["road_condition_score"] or 0))
    # Does a poor road actually cost anything here? Answered rather than
    # asserted, on the trip ledger that is already loaded.
    poor = [r for r in have if (r["road_condition_score"] or 0) > 0.5
            and r.get("turnaround_h")]
    good = [r for r in have if (r["road_condition_score"] or 0) <= 0.5
            and r.get("turnaround_h")]

    def mean(rs, f):
        vals = [r[f] for r in rs if r.get(f) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "available": True,
        "estate": estate.upper(),
        "totals": {
            "segments": len(st["roads"]),
            "km": round(sum(rd["length_km"] or 0 for rd in st["roads"].values()), 1),
            "mean_days_since_graded": round(
                sum(r["road_days_since_graded"] or 0 for r in have) / len(have)),
            "culvert_repairs_12m": sum(rd["culvert_repairs_12m"] or 0
                                       for rd in st["roads"].values()),
        },
        "by_condition": [{"condition": c, **v, "km": round(v["km"], 1)}
                         for c, v in sorted(by_cond.items())],
        "worst_segments": [{"block_label": r["block_label"],
                            "division_code": r["division_code"],
                            "condition": r["road_condition"],
                            "days_since_graded": r["road_days_since_graded"],
                            "turnaround_h": r.get("turnaround_h")}
                           for r in worst[:top]],
        "haulage_effect": {
            "poor_roads_blocks": len(poor),
            "good_roads_blocks": len(good),
            "turnaround_h_poor": mean(poor, "turnaround_h"),
            "turnaround_h_good": mean(good, "turnaround_h"),
            "diesel_per_t_poor": mean(poor, "diesel_l_per_tonne"),
            "diesel_per_t_good": mean(good, "diesel_l_per_tonne"),
        },
        "provenance": "synthetic: SAP PM holds the work orders; the export has none",
        "note": ("Segment geometry follows the longest edge of each real block "
                 "polygon, so every road lies where one plausibly runs."),
        "caveat": ("Road condition and distance to mill were generated from a "
                   "shared term, so the haulage gap above is partly the far "
                   "blocks being far rather than their roads being bad. Real "
                   "data has the same confounding - the roads that get graded "
                   "last are the distant ones - so separating the two needs "
                   "condition to vary within a distance band, which is exactly "
                   "what the work-order extract would let us test."),
    }


def rotation_plan(estate: str = "EC", top: int = 25) -> dict:
    """UC-01: which blocks are due, and which gang covers each."""
    rows = block_rows(estate) or []
    due = [r for r in rows if r["ripeness_pressure"] is not None]
    due.sort(key=lambda r: -r["ripeness_pressure"])
    overdue = [r for r in due if r["ripeness_pressure"] > 1.0]
    by_gang = defaultdict(lambda: {"blocks": 0, "ha": 0.0, "overdue": 0})
    for r in due:
        g = by_gang[r["gang_code"]]
        g["blocks"] += 1
        g["ha"] += r["planted_ha"] or 0
        if r["ripeness_pressure"] > 1.0:
            g["overdue"] += 1
    return {
        "estate": estate.upper(),
        "overdue_blocks": len(overdue),
        "overdue_ha": round(sum(r["planted_ha"] or 0 for r in overdue), 1),
        "queue": [{
            "block_id": r["block_id"], "block_label": r["block_label"],
            "division_code": r["division_code"], "gang_code": r["gang_code"],
            "days_since_harvest": r["days_since_harvest"],
            "rotation_target_days": r["rotation_target_days"],
            "ripeness_pressure": r["ripeness_pressure"],
            "planted_ha": r["planted_ha"],
            "last_harvest_date": r["last_harvest_date"],
        } for r in due[:top]],
        "by_gang": {k: {**v, "ha": round(v["ha"], 1)} for k, v in sorted(by_gang.items())},
        "provenance": "synthetic: rotation state and gang assignment are invented",
        "note": ("Ripeness pressure is days since harvest over the block's target "
                 "round. It says which blocks are ready. It does not solve the "
                 "gang assignment, which needs real crew capacity and travel time."),
    }
