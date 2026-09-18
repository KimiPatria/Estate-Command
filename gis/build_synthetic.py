"""Synthetic data for the capabilities EPMS does not record.

Run once:  python gis/build_synthetic.py
Writes:    gis/data/synthetic/*.csv

Why CSV, and why generated rather than hand-written
---------------------------------------------------
Every file here is a stand-in for a feed the client either has somewhere we
cannot reach or does not collect at all. Writing them as CSV keeps the shape
of the eventual real extract visible: when the client hands over their cost
ledger or their vendor master, the swap is a file replacement, not a rewrite.

Everything is derived from the REAL EC blocks - real hectares, real palm
counts, real planting years, real recorded harvest - so every synthetic row
joins to a real block and moves when the real numbers move. Nothing is drawn
from thin air where a real number was available.

The latent field
----------------
Block quality is not random. Neighbouring blocks share soil, drainage and
terrain, so a random per-block draw produces a map that looks like static and
reads as fake immediately. Instead a smooth 2D field over the estate drives
soil quality, and that one field feeds vegetation index, fertiliser demand and
cost. It is also anchored to each block's REAL yield relative to its planting
cohort, so the synthetic vegetation layer agrees with the real harvest layer -
which is what makes an anomaly map credible rather than decorative.

The bunch-weight calibration - read this before quoting tonnage
---------------------------------------------------------------
EC records 2,870 bunches/ha/yr. At the 12-16 kg average bunch weight normal
for 7-11 year palm that implies 34-46 t/ha/yr, well above the ~28 t/ha/yr
ceiling of a very good estate. Something in the export does not add up:
most likely the OPH rows double-count, since they are per-harvester tallies.

Rather than print an impossible tonnage, ABW here is back-solved so the estate
lands at ~23 t/ha/yr, which puts the mean near 8 kg. That is a CALIBRATION,
not an agronomic estimate, and the low implied weight is a genuine question to
put to the client. Every downstream number - cost per tonne, margin, contract
position - inherits it. See the abw_note column.
"""

import csv
import json
import logging
import math
import random
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("estate-command.synthetic")

OUT = Path(__file__).parent / "data" / "synthetic"
SEED = 20260910

# -- calibration ------------------------------------------------------------
TARGET_T_HA_YR = 23.0     # see the module docstring
FFB_PRICE_IDR_KG = 2600   # Indonesian FFB farmgate, 2025-26 band is 2,300-3,200

# Estate cost structure, IDR per planted hectare per year. Sums to 30.0M,
# roughly USD 1,875/ha/yr, inside the 1,500-2,200 band quoted for Indonesian
# estates. At 23 t/ha that is ~1,300 IDR/kg against a 2,600 IDR/kg price.
COST_IDR_HA_YR = {
    "harvest": 7_000_000,
    "upkeep": 5_500_000,
    "fertilizer": 9_000_000,
    "overhead": 6_000_000,
    "infrastructure": 2_500_000,
}

# Upkeep rotations for mature palm, in days.
UPKEEP_INTERVALS = {
    "pruning": 240,
    "circle_weeding": 75,
    "path_upkeep": 110,
    "spraying": 100,
}

HARVESTER_HA = 13.0       # one harvester covers ~13 ha on a 7-14 day round
FORWARD_MONTHS = 6

# The last day of the real harvest export. Day placement inside the final
# month must stop here: May carries 23 days of records, not 31, and a cut
# placed on the 29th would be a row the client can show never happened.
WINDOW_END = date(2025, 5, 23)


# ── the latent field ───────────────────────────────────────────────────────

def _centroid(ring):
    n = len(ring) - 1
    return (sum(p[0] for p in ring[:n]) / n, sum(p[1] for p in ring[:n]) / n)


def _latent_field(blocks, rng):
    """Smooth spatially-autocorrelated soil quality, anchored to real yield.

    Two components:
      * a smooth surface built from a handful of Gaussian bumps, so
        neighbouring blocks resemble each other
      * the block's own real yield relative to its planting cohort, so the
        synthetic layers corroborate the real harvest instead of contradicting it
    Returned per block id, roughly centred on 0, usable as a z-score.
    """
    pts = {f["id"]: _centroid(f["geometry"]["coordinates"][0]) for f in blocks}
    xs = [p[0] for p in pts.values()]
    ys = [p[1] for p in pts.values()]
    w, e, s, n = min(xs), max(xs), min(ys), max(ys)

    bumps = [(rng.uniform(w, e), rng.uniform(s, n),
              rng.uniform(0.012, 0.030), rng.uniform(-1.0, 1.0))
             for _ in range(7)]

    # Real yield relative to planting cohort.
    cohort: dict[int, list] = {}
    per_ha = {}
    for f in blocks:
        p = f["properties"]
        if p.get("bunches_total") and p.get("planted_ha"):
            v = p["bunches_total"] / p["planted_ha"]
            per_ha[f["id"]] = v
            cohort.setdefault(p.get("planted_year"), []).append(v)
    med = {}
    for yr, vals in cohort.items():
        vals = sorted(vals)
        m = len(vals)
        med[yr] = vals[m // 2] if m % 2 else (vals[m // 2 - 1] + vals[m // 2]) / 2

    out = {}
    for f in blocks:
        x, y = pts[f["id"]]
        smooth = sum(amp * math.exp(-((x - bx) ** 2 + (y - by) ** 2) / (2 * sd ** 2))
                     for bx, by, sd, amp in bumps)
        yr = f["properties"].get("planted_year")
        real = 0.0
        if f["id"] in per_ha and med.get(yr):
            real = (per_ha[f["id"]] / med[yr]) - 1.0
        # Real signal leads; the smooth field supplies the spatial texture.
        out[f["id"]] = round(2.2 * real + 0.6 * smooth, 4)
    return out


def _write(name, rows, header, note=None):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    with path.open("w", encoding="utf-8", newline="") as fh:
        if note:
            fh.write(f"# SYNTHETIC. {note}\n")
        wr = csv.DictWriter(fh, fieldnames=header)
        wr.writeheader()
        wr.writerows(rows)
    log.info("[synthetic] %s: %d rows", name, len(rows))
    return len(rows)


# ── generators ─────────────────────────────────────────────────────────────

def _abw_map(blocks, latent, rng_seed=SEED):
    """The same per-block bunch weights gen_abw writes, as a dict.

    The trip ledger needs them to compute an expected weight, and re-deriving
    them from the same seed keeps one definition rather than two that can
    drift apart.
    """
    import random as _r
    rng = _r.Random(rng_seed)
    tot_b = sum(f["properties"]["bunches_total"] or 0 for f in blocks)
    tot_ha = sum(f["properties"]["planted_ha"] or 0 for f in blocks)
    mean_abw = TARGET_T_HA_YR * 1000.0 / (tot_b / tot_ha / 4.73 * 12)
    out = {}
    for f in blocks:
        p = f["properties"]
        age = p.get("palm_age_years") or 10
        abw = (mean_abw * (1.0 + 0.035 * (age - 9))
               * (1.0 + 0.10 * latent[f["id"]]) * rng.uniform(0.97, 1.03))
        out[_bk(p)] = round(abw, 2)
    return out


def _bk(props) -> str:
    return f'{int(props["division_code"])}|{int(props["block_code"])}'


def gen_abw(blocks, latent, rng):
    """Average bunch weight per block. The keystone: unlocks every tonnage."""
    tot_b = sum(f["properties"]["bunches_total"] or 0 for f in blocks)
    tot_ha = sum(f["properties"]["planted_ha"] or 0 for f in blocks)
    months = 4.73
    mean_abw = TARGET_T_HA_YR * 1000.0 / (tot_b / tot_ha / months * 12)

    rows = []
    for f in blocks:
        p = f["properties"]
        age = p.get("palm_age_years") or 10
        # Bunch weight climbs with palm age and with block condition.
        age_f = 1.0 + 0.035 * (age - 9)
        cond = 1.0 + 0.10 * latent[f["id"]]
        abw = mean_abw * age_f * cond * rng.uniform(0.97, 1.03)
        rows.append({
            "division_code": p["division_code"],
            "block_code": p["block_code"],
            "abw_kg": round(abw, 2),
            "palm_age_years": age,
            "abw_note": "calibrated to 23 t/ha/yr, not an agronomic estimate",
        })
    return _write("ec_abw.csv", rows,
                  ["division_code", "block_code", "abw_kg", "palm_age_years", "abw_note"],
                  "Average bunch weight. Back-solved so the estate lands at "
                  f"{TARGET_T_HA_YR} t/ha/yr (mean ~{mean_abw:.2f} kg). The real "
                  "counts imply 34-46 t/ha/yr at a normal 12-16 kg ABW, which is "
                  "impossible - the OPH export likely double-counts. Ask the client.")


def gen_forward_forecast(blocks, latent, rng, months):
    """Forward monthly forecast per block, with a widening band.

    The scrubber needs months past 2025-05 to run into. Level comes from each
    block's own recorded mean; seasonality follows the southern Papua pattern;
    the interval widens with horizon the way a conformal band does.
    """
    last = months[-1]
    y, m = int(last[:4]), int(last[5:])
    forward = []
    for i in range(1, FORWARD_MONTHS + 1):
        mm = m + i
        forward.append(f"{y + (mm - 1) // 12:04d}-{(mm - 1) % 12 + 1:02d}")

    # Southern Papua: drier Jun-Oct, wetter Dec-Mar. Palm responds at a lag,
    # so the production dip trails the dry season rather than matching it.
    seasonal = {1: 1.06, 2: 1.04, 3: 1.00, 4: 0.96, 5: 0.93, 6: 0.90,
                7: 0.89, 8: 0.92, 9: 0.97, 10: 1.04, 11: 1.09, 12: 1.09}

    rows = []
    for f in blocks:
        p = f["properties"]
        by_month = p.get("bunches_by_month") or {}
        obs = [v for v in by_month.values() if v]
        if not obs:
            continue
        base = sum(obs) / len(obs)
        drift = 1.0 + 0.02 * latent[f["id"]]
        # Each block's own month-to-month variability. A conformal band is wide
        # where the block's history is erratic and tight where it is steady, so
        # driving the band only off the horizon makes every block equally
        # uncertain, which is both wrong and useless to look at.
        sd = (sum((v - base) ** 2 for v in obs) / len(obs)) ** 0.5
        cv = min(0.9, sd / base) if base else 0.3
        for i, mo in enumerate(forward, start=1):
            mi = int(mo[5:])
            p50 = base * seasonal[mi] * (drift ** i) * rng.uniform(0.96, 1.04)
            # Widens with horizon and with the block's own noise.
            spread = (0.09 + 0.035 * (i - 1)) * (1.0 + 2.2 * cv)
            rows.append({
                "division_code": p["division_code"],
                "block_code": p["block_code"],
                "month": mo,
                "horizon": i,
                "p10": int(p50 * (1 - spread)),
                "p50": int(p50),
                "p90": int(p50 * (1 + spread)),
            })
    _write("ec_forecast_forward.csv", rows,
           ["division_code", "block_code", "month", "horizon", "p10", "p50", "p90"],
           "Forward block forecast in bunches. The real export ends 2025-05-23; "
           "these months are generated so the time scrubber has a forward half.")
    return forward


def gen_costs(blocks, latent, rng, months, forward):
    """Per-block monthly cost by category, in IDR."""
    mill = _mill_point(blocks)
    rows = []
    allm = list(months) + list(forward)
    for f in blocks:
        p = f["properties"]
        ha = p.get("planted_ha") or 0
        if not ha:
            continue
        cx, cy = _centroid(f["geometry"]["coordinates"][0])
        dist_km = math.hypot((cx - mill[0]) * 111.32 * math.cos(math.radians(cy)),
                             (cy - mill[1]) * 111.32)
        q = latent[f["id"]]
        for mo in allm:
            share = 1.0 / 12.0
            bunches = (p.get("bunches_by_month") or {}).get(mo)
            # Harvesting tracks what was actually cut; the rest is calendar cost.
            harvest_f = (bunches / max(sum((p.get("bunches_by_month") or {}).values()), 1)
                         * len(months)) if bunches else 1.0
            row = {
                "division_code": p["division_code"],
                "block_code": p["block_code"],
                "month": mo,
                # A poor block still gets harvested, just yields less per pass.
                "harvest_idr": int(COST_IDR_HA_YR["harvest"] * ha * share * harvest_f),
                "upkeep_idr": int(COST_IDR_HA_YR["upkeep"] * ha * share
                                  * (1.0 - 0.08 * q) * rng.uniform(0.94, 1.06)),
                # Weak blocks get more fertiliser, not less - that is how
                # agronomists actually assign programmes, and it is exactly the
                # confounding that makes naive response analysis wrong.
                "fertilizer_idr": int(COST_IDR_HA_YR["fertilizer"] * ha * share
                                      * (1.0 - 0.15 * q) * rng.uniform(0.92, 1.08)),
                "transport_idr": int(COST_IDR_HA_YR["overhead"] * 0.0 + ha * share
                                     * 260_000 * (0.6 + dist_km / 6.0)),
                "overhead_idr": int(COST_IDR_HA_YR["overhead"] * ha * share),
                "infrastructure_idr": int(COST_IDR_HA_YR["infrastructure"] * ha * share),
                "km_to_mill": round(dist_km, 2),
            }
            rows.append(row)
    return _write("ec_block_costs.csv", rows,
                  ["division_code", "block_code", "month", "harvest_idr", "upkeep_idr",
                   "fertilizer_idr", "transport_idr", "overhead_idr",
                   "infrastructure_idr", "km_to_mill"],
                  "Per-block monthly cost in IDR. EPMS routes cost through "
                  "m_cost_control_mapping but the EC export carries none of it. "
                  "Structure sums to ~30M IDR/ha/yr.")


def _mill_point(blocks):
    """Mill location: the estate's south-west corner, where the road leaves."""
    pts = [p for f in blocks for p in f["geometry"]["coordinates"][0]]
    return (min(p[0] for p in pts) - 0.012, min(p[1] for p in pts) - 0.010)


def gen_gangs_and_rotation(blocks, rng, months):
    """Harvest gangs and each block's rotation state.

    Gang territories are contiguous runs of blocks rather than a round-robin,
    because that is how an estate actually divides a division and because a
    scheduler that rewards contiguity has to start from a roster that has it.

    The rotation state is READ BACK from the harvest days that every other
    feed uses: last_harvest_date is the block's last placed cutting day, and
    the target round is the interval the block actually held over the first
    two months of the export, clamped to the 5-14 day band. So "days past the
    round" at the anchor date is measured against the block's own early-year
    rhythm, and the stretch through April and May is the client's own count.
    """
    by_div: dict[str, list] = {}
    for f in blocks:
        by_div.setdefault(f["properties"]["division_code"], []).append(f)

    gangs, rot = [], []
    first_two = list(months)[:2]
    days_first_two = sum(
        (date(int(m[:4]) + (int(m[5:7]) == 12), (int(m[5:7]) % 12) + 1, 1)
         - date(int(m[:4]), int(m[5:7]), 1)).days for m in first_two)
    for div in sorted(by_div, key=int):
        blks = by_div[div]
        ha = sum(b["properties"].get("planted_ha") or 0 for b in blks)
        n_gangs = max(2, round(ha / 260.0))
        div_gangs = []
        for g in range(1, n_gangs + 1):
            code = f"G{div}-{g:02d}"
            div_gangs.append(code)
            gangs.append({
                "gang_code": code,
                "name": f"Gang {div}-{g:02d}",
                "division_code": div,
                "headcount": rng.randint(18, 28),
                "harvesters": rng.randint(12, 20),
                "kerani": 1,
                "mandor": 1,
            })
        chunk = math.ceil(len(blks) / len(div_gangs))
        for i, b in enumerate(blks):
            p = b["properties"]
            hd = p.get("harvest_days_by_month") or {}
            n_early = sum(hd.get(m) or 0 for m in first_two)
            target = (max(5, min(14, round(days_first_two / n_early)))
                      if n_early else rng.choice([7, 8, 9, 10, 12, 14]))
            last_day = None
            for m in reversed(list(months)):
                days = _trip_days(p, m)
                if days:
                    last_day = days[-1]
                    break
            days_since = (WINDOW_END - last_day).days if last_day else int(target * 1.5)
            rot.append({
                "division_code": div,
                "block_code": p["block_code"],
                "gang_code": div_gangs[min(i // chunk, len(div_gangs) - 1)],
                "rotation_target_days": target,
                "last_harvest_date": (last_day or (WINDOW_END - timedelta(days=days_since))).isoformat(),
                "days_since_harvest": days_since,
            })

    _write("ec_gangs.csv", gangs,
           ["gang_code", "name", "division_code", "headcount", "harvesters",
            "kerani", "mandor"],
           "Harvest gangs. EPMS has m_gang_employee but the EC export carries none. "
           "Territories are contiguous runs of blocks within a division.")
    _write("ec_rotation.csv", rot,
           ["division_code", "block_code", "gang_code", "rotation_target_days",
            "last_harvest_date", "days_since_harvest"],
           "Rotation state as at 2025-05-23. last_harvest_date is the block's "
           "last placed cutting day, consistent with the trip ledger and the "
           "work orders; the target round is the interval the block held over "
           "January-February, clamped to 5-14 days. Gang assignment is invented.")
    return len(gangs)


def gen_vegetation(blocks, latent, rng, months, forward):
    """NDRE per block per month, plus the block's own rolling baseline.

    NDRE rather than NDVI: a closed mature palm canopy saturates NDVI and the
    variance that matters disappears. Values sit in the 0.25-0.45 band typical
    of mature palm, and track the latent field so a stressed block reads
    stressed on both the vegetation layer and the yield layer.
    """
    rows = []
    for f in blocks:
        p = f["properties"]
        q = latent[f["id"]]
        base = 0.345 + 0.045 * q
        for i, mo in enumerate(list(months) + list(forward)):
            mi = int(mo[5:])
            # Canopy dips through the dry season and recovers after the rains.
            seas = 0.012 * math.sin((mi - 4) / 12.0 * 2 * math.pi)
            ndre = base + seas + rng.uniform(-0.008, 0.008)
            rows.append({
                "division_code": p["division_code"],
                "block_code": p["block_code"],
                "month": mo,
                "ndre": round(max(0.05, min(0.65, ndre)), 4),
                "ndre_baseline": round(base, 4),
                "cloud_pct": rng.randint(0, 72),
            })
    return _write("ec_vegetation.csv", rows,
                  ["division_code", "block_code", "month", "ndre",
                   "ndre_baseline", "cloud_pct"],
                  "NDRE per block per month. Sentinel-2 L2A is free over Merauke "
                  "and this layer could be computed for real; it is generated here "
                  "so the layer works offline. Correlated with real yield.")


def gen_vendors_and_orders(blocks, rng, months, forward):
    """Third-party FFB vendors with coordinates, and monthly commitments."""
    mill = _mill_point(blocks)
    names = ["KUD Sumber Makmur", "PT Rimba Sawit Papua", "KUD Tani Jaya",
             "CV Merauke Agro", "KUD Karya Bersama", "PT Selatan Palma"]
    # Distance is stratified rather than drawn freely. UC-09 only says anything
    # if the cheapest vendor is not the best one, and that needs a near vendor
    # asking a high price alongside a far vendor asking a low one. An unlucky
    # uniform draw put all six in a 52-72 km band and flattened the decision.
    bands = [(12, 22), (18, 30), (26, 40), (38, 55), (50, 68), (62, 85)]
    vendors = []
    for i, nm in enumerate(names):
        brg = rng.uniform(0, 2 * math.pi)
        dist = rng.uniform(*bands[i])
        lat = mill[1] + dist * math.cos(brg) / 111.32
        lon = mill[0] + dist * math.sin(brg) / (111.32 * math.cos(math.radians(mill[1])))
        vendors.append({
            "vendor_code": f"V{i + 1:03d}",
            "name": nm,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "distance_km": round(dist, 1),
            # Distant vendors shade their price, but by less per km than the
            # haulage costs. That is what makes landed cost a different ranking
            # from headline price, and the whole reason UC-09 exists. Quality
            # (OER) and reliability (fill rate, lateness) then reorder it again,
            # so no single column picks the winner.
            "price_idr_per_kg": int(2700 - 2.5 * dist + rng.randint(-80, 80)),
            "oer_pct": round(rng.uniform(18.5, 23.0), 1),
            "fill_rate": round(rng.uniform(0.72, 0.99), 3),
            "avg_days_late": round(rng.uniform(0, 9.0), 1),
            "capacity_t_month": rng.randrange(400, 3200, 100),
        })
    _write("vendors.csv", vendors,
           ["vendor_code", "name", "lat", "lon", "distance_km", "price_idr_per_kg",
            "oer_pct", "fill_rate", "avg_days_late", "capacity_t_month"],
           "Third-party FFB vendors. ZEPMS_EM_VENDOR_OUT carries only LIFNR, "
           "NAME1 and WERKS - no address and no coordinates. Locations invented.")

    # Commitments sized against the estate's own production so the gap moves.
    tot_b = sum(f["properties"]["bunches_total"] or 0 for f in blocks)
    monthly_t = tot_b / len(months) * 0.008  # bunches -> tonnes at ~8 kg
    orders = []
    customers = ["PT Nusantara Refinery", "PT Bintang Oleo", "PT Papua Mill Co"]
    for i, mo in enumerate(list(months) + list(forward)):
        # Commitments drift up, so the forward months open a real gap.
        factor = 0.95 + 0.02 * i
        orders.append({
            "order_id": f"SO-2025-{i + 101:04d}",
            "customer": customers[i % len(customers)],
            "month": mo,
            "committed_tonnes": int(monthly_t * factor),
            "price_idr_per_kg": FFB_PRICE_IDR_KG + rng.randint(-90, 120),
            "incoterm": rng.choice(["EXW", "FOB", "DAP"]),
        })
    _write("sales_orders.csv", orders,
           ["order_id", "customer", "month", "committed_tonnes",
            "price_idr_per_kg", "incoterm"],
           "FFB delivery commitments. ZEPMS_SD_SORD_OUT exists in EPMS but the "
           "EC export carries no orders.")
    return len(vendors), len(orders)


def gen_labour(blocks, rng, months, forward):
    """Harvester supply against demand, per division per month."""
    by_div: dict[str, float] = {}
    for f in blocks:
        p = f["properties"]
        by_div[p["division_code"]] = by_div.get(p["division_code"], 0) + (p.get("planted_ha") or 0)

    rows = []
    for div in sorted(by_div, key=int):
        need = by_div[div] / HARVESTER_HA
        establishment = int(need * rng.uniform(0.94, 1.12))
        for mo in list(months) + list(forward):
            mi = int(mo[5:])
            # Ramadan/Lebaran fell in March-April 2025; migration thins the
            # workforce for several weeks and it is the single biggest
            # predictable labour shock in the Indonesian calendar.
            lebaran = 0.82 if mi in (3, 4) else 1.0
            att = min(0.97, rng.uniform(0.80, 0.93) * lebaran)
            rows.append({
                "division_code": div,
                "month": mo,
                "planted_ha": round(by_div[div], 2),
                "harvesters_required": int(round(need)),
                "harvesters_on_roll": establishment,
                "attendance_rate": round(att, 3),
                "harvesters_effective": int(round(establishment * att)),
                "deficit": int(round(need)) - int(round(establishment * att)),
            })
    return _write("ec_labour.csv", rows,
                  ["division_code", "month", "planted_ha", "harvesters_required",
                   "harvesters_on_roll", "attendance_rate", "harvesters_effective",
                   "deficit"],
                  "Harvester supply vs demand. EPMS has t_attendance and "
                  "m_gang_employee but the EC export carries neither. "
                  "One harvester per 13 ha; Lebaran dip applied to Mar-Apr.")


def gen_upkeep(blocks, rng):
    """Days overdue per block per activity, against the standard rotation."""
    end = date(2025, 5, 23)
    rows = []
    for f in blocks:
        p = f["properties"]
        for act, interval in UPKEEP_INTERVALS.items():
            # Most blocks are roughly on programme; a tail has slipped badly.
            slip = rng.choice([rng.uniform(0.3, 0.95)] * 6 + [rng.uniform(1.0, 2.1)])
            since = int(interval * slip)
            rows.append({
                "division_code": p["division_code"],
                "block_code": p["block_code"],
                "activity": act,
                "last_done": (end - timedelta(days=since)).isoformat(),
                "interval_days": interval,
                "days_since": since,
                "days_overdue": max(0, since - interval),
            })
    return _write("ec_upkeep.csv", rows,
                  ["division_code", "block_code", "activity", "last_done",
                   "interval_days", "days_since", "days_overdue"],
                  "Upkeep rotation state. This is 'days overdue against the "
                  "standard rotation', NOT block condition - EPMS records that an "
                  "activity happened, never what the block looks like.")


# ── transport: the trip ledger and the fleet ───────────────────────────────
#
# The centrepiece feed. Field-to-mill shrinkage needs both sides of the ledger
# in one row: what the field says was collected, and what the mill says was
# weighed. EPMS has the first, SAP and the mill have the second, and the EC
# export carries neither.
#
# What makes this worth building rather than hand-waving: the discrepancy is
# already real. Summed as recorded, EC runs 2,870 bunches per hectare per year,
# which at a normal 12-16 kg bunch implies 34-46 t/ha/yr against a ~28 t/ha/yr
# ceiling for a very good estate. Count and weight already disagree somewhere
# in the client's own data, and neither side can be checked without the trips.

VEHICLE_CLASSES = [
    # class, count, capacity tonnes, litres per 100 km laden, failure rate/1000 trips
    ("tractor-trailer", 8, 4.5, 32.0, 11.0),
    ("dump-truck", 5, 7.0, 41.0, 7.0),
    ("mini-truck", 4, 2.5, 22.0, 5.0),
]

# Baseline field-to-mill loss: loose fruit left at the collection point,
# moisture, and a little spillage. Real estates run 1-3%.
BASE_SHRINK = 0.018

# The planted anomaly. One driver, one fortnight, one route, losing far more
# than the baseline. The detector is scored against exactly this set, which is
# what turns a synthetic demonstration into a measurement - see gis/models.
ANOMALY_DRIVER = "D-07"
ANOMALY_FROM = date(2025, 3, 10)
ANOMALY_TO = date(2025, 3, 23)
ANOMALY_SHRINK = (0.085, 0.16)

# Free fatty acid climbs once the bunch is cut. This is the one quality loss
# the estate controls directly, and it is a function of delay.
FFA_BASE = 1.8
FFA_PER_HOUR = 0.12


def _trip_days(props, month, rng=None):
    """The dates in a month a block was actually cut, from its REAL day count.

    The export records how many distinct days each block was harvested in each
    month but not which ones. The COUNT is the client's; the placement is ours,
    and three things about the placement matter:

      * it is deterministic per block and month, seeded from the block key
        rather than drawn from the running generator, so the trip ledger, the
        worker-day feed, the work orders and the rotation state all put the
        same block on the same days. Two feeds that disagree about when a
        block was cut are worse than one feed missing.
      * each block carries its own phase through the round, so at any date
        the estate's blocks sit at different points of their cycle. Spread
        evenly from the 1st, every block would have been cut on the same
        days and the rotation map would read as one flat colour.
      * the final month is clamped to the export's last day.

    The `rng` argument is accepted and ignored, for the callers that predate
    this rule.
    """
    n = (props.get("harvest_days_by_month") or {}).get(month) or 0
    if not n:
        return []
    y, m = int(month[:4]), int(month[5:7])
    last = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)).day
    if date(y, m, 1) <= WINDOW_END <= date(y, m, last):
        last = WINDOW_END.day
    key = _bk(props)
    phase = random.Random(f"{SEED}:{key}:phase").random()
    r = random.Random(f"{SEED}:{key}:{month}")
    step = last / n
    out = []
    for i in range(n):
        d = int(round(phase * step + i * step)) + r.randint(-1, 1)
        out.append(date(y, m, max(1, min(last, d))))
    return sorted(set(out))


def gen_transport(blocks, latent, rng, months):
    """Vehicles, drivers, trips, weighbridge tickets and breakdowns.

    Writes three files that a real extract would arrive as three files:
    the fleet master, the trip-level weighbridge ledger, and the maintenance
    work orders.
    """
    mill = _mill_point(blocks)
    abw = _abw_map(blocks, latent)

    # -- fleet master ------------------------------------------------------
    vehicles = []
    for cls, n, cap, l100, fail in VEHICLE_CLASSES:
        for i in range(1, n + 1):
            vehicles.append({
                "vehicle_id": f"{cls[:2].upper()}-{i:02d}",
                "vehicle_class": cls,
                "capacity_t": cap,
                "litres_per_100km": round(l100 * rng.uniform(0.92, 1.12), 1),
                "year_acquired": rng.randint(2013, 2023),
                "odometer_km": rng.randrange(40_000, 320_000, 1_000),
                "failures_per_1000_trips": round(fail * rng.uniform(0.7, 1.4), 1),
            })
    drivers = [f"D-{i:02d}" for i in range(1, 21)]

    # -- per-block haulage geometry ---------------------------------------
    km, route = {}, {}
    for f in blocks:
        p = f["properties"]
        cx, cy = _centroid(f["geometry"]["coordinates"][0])
        km[_bk(p)] = round(math.hypot(
            (cx - mill[0]) * 111.32 * math.cos(math.radians(cy)),
            (cy - mill[1]) * 111.32), 2)
        # Routes follow divisions, because that is how estate roads run.
        route[_bk(p)] = f"R-{int(p['division_code']):02d}"

    trips, orders = [], []
    tid = 0
    for f in blocks:
        p = f["properties"]
        k = _bk(p)
        w = abw.get(k) or 8.0
        for mo in months:
            bunches_m = (p.get("bunches_by_month") or {}).get(mo) or 0
            days = _trip_days(p, mo, rng)
            if not bunches_m or not days:
                continue
            # Split the month's real bunch count across its real harvest days.
            per_day = [bunches_m // len(days)] * len(days)
            per_day[-1] += bunches_m - sum(per_day)
            for d, bunches in zip(days, per_day):
                if bunches <= 0:
                    continue
                # A day's cut is split across as many loads as the vehicle can
                # carry, which is what makes this a trip ledger rather than a
                # daily summary.
                veh = rng.choice(vehicles)
                cap_bunches = max(1, int(veh["capacity_t"] * 1000 / w))
                loads = max(1, math.ceil(bunches / cap_bunches))
                for li in range(loads):
                    tid += 1
                    b = bunches // loads + (bunches % loads if li == 0 else 0)
                    drv = rng.choice(drivers)
                    expected_kg = b * w

                    anomalous = (drv == ANOMALY_DRIVER
                                 and ANOMALY_FROM <= d <= ANOMALY_TO
                                 and route[k] in ("R-03", "R-04"))
                    if anomalous:
                        shrink = rng.uniform(*ANOMALY_SHRINK)
                    else:
                        shrink = max(0.0, rng.gauss(BASE_SHRINK, 0.010))

                    net = expected_kg * (1 - shrink)
                    tare = veh["capacity_t"] * 1000 * rng.uniform(0.55, 0.72)

                    # Haulage time: loading, the run in, and a mill queue that
                    # lengthens through the afternoon.
                    dist = km[k]
                    depart_h = rng.uniform(7.0, 15.0)
                    travel_h = dist / rng.uniform(16.0, 26.0)
                    queue_h = max(0.0, rng.gauss(0.9, 0.55)) * (1.4 if depart_h > 12 else 1.0)
                    turn_h = travel_h + queue_h + rng.uniform(0.3, 0.9)

                    trips.append({
                        "trip_id": f"T{tid:06d}",
                        "date": d.isoformat(),
                        "month": mo,
                        "division_code": p["division_code"],
                        "block_code": p["block_code"],
                        "route_code": route[k],
                        "vehicle_id": veh["vehicle_id"],
                        "vehicle_class": veh["vehicle_class"],
                        "driver_id": drv,
                        "km_to_mill": dist,
                        # The field side of the ledger, from EPMS.
                        "bunches_epms": b,
                        "loose_fruit_kg": round(b * w * rng.uniform(0.004, 0.021), 1),
                        # The mill side, from the weighbridge.
                        "gross_kg": round(net + tare),
                        "tare_kg": round(tare),
                        "net_kg": round(net),
                        "depart_time": f"{int(depart_h):02d}:{int(depart_h % 1 * 60):02d}",
                        "turnaround_h": round(turn_h, 2),
                        "queue_h": round(queue_h, 2),
                        "mill_dockage_pct": round(max(0.0, rng.gauss(1.1, 0.6)), 2),
                        "ffa_pct": round(FFA_BASE + FFA_PER_HOUR * turn_h
                                         + rng.uniform(-0.15, 0.15), 2),
                        "diesel_l": round(dist * 2 * veh["litres_per_100km"] / 100.0
                                          * rng.uniform(0.9, 1.15), 2),
                        # Kept ONLY so the detector can be scored. A real
                        # extract has no such column; gis/models/shrinkage.py
                        # must never read it as a feature.
                        "is_planted_anomaly": int(anomalous),
                    })

    # -- breakdown work orders --------------------------------------------
    by_vehicle: dict[str, int] = {}
    for t in trips:
        by_vehicle[t["vehicle_id"]] = by_vehicle.get(t["vehicle_id"], 0) + 1
    faults = ["hydraulic hose", "clutch plate", "tyre carcass", "alternator",
              "brake shoe", "radiator", "injector pump", "wheel bearing"]
    oid = 0
    for v in vehicles:
        n_trips = by_vehicle.get(v["vehicle_id"], 0)
        n_fail = int(round(n_trips / 1000.0 * v["failures_per_1000_trips"]))
        for _ in range(n_fail):
            oid += 1
            d = date(2025, 1, 1) + timedelta(days=rng.randint(0, 142))
            down = round(max(1.5, rng.lognormvariate(1.6, 0.7)), 1)
            orders.append({
                "order_id": f"PM-{oid:05d}",
                "vehicle_id": v["vehicle_id"],
                "vehicle_class": v["vehicle_class"],
                "raised_date": d.isoformat(),
                "fault": rng.choice(faults),
                "downtime_h": down,
                "cost_idr": int(down * rng.uniform(180_000, 620_000)),
            })

    _write("ec_vehicles.csv", vehicles,
           ["vehicle_id", "vehicle_class", "capacity_t", "litres_per_100km",
            "year_acquired", "odometer_km", "failures_per_1000_trips"],
           "Fleet master. SAP Plant Maintenance holds this; the EC export "
           "carries no vehicle of any kind.")

    _write("ec_weighbridge.csv", trips,
           ["trip_id", "date", "month", "division_code", "block_code", "route_code",
            "vehicle_id", "vehicle_class", "driver_id", "km_to_mill",
            "bunches_epms", "loose_fruit_kg", "gross_kg", "tare_kg", "net_kg",
            "depart_time", "turnaround_h", "queue_h", "mill_dockage_pct",
            "ffa_pct", "diesel_l", "is_planted_anomaly"],
           "Trip-level ledger joining the EPMS field count to the mill "
           "weighbridge ticket. Bunch counts per block-month and the number of "
           "harvest days are the client's REAL figures; the split into trips, "
           "the weights, vehicles, drivers and times are generated. "
           "is_planted_anomaly marks the injected cohort and exists only to "
           "score the detector - a real extract has no such column.")

    _write("ec_pm_orders.csv", orders,
           ["order_id", "vehicle_id", "vehicle_class", "raised_date", "fault",
            "downtime_h", "cost_idr"],
           "Breakdown work orders. SAP PM holds these; the EC export has none.")

    planted = sum(t["is_planted_anomaly"] for t in trips)
    log.info("[synthetic] weighbridge: %d trips, %d planted anomalies (%.2f%%)",
             len(trips), planted, 100 * planted / max(len(trips), 1))
    return {"vehicles": len(vehicles), "trips": len(trips),
            "pm_orders": len(orders), "planted_anomalies": planted}


# ── pest and disease ───────────────────────────────────────────────────────
#
# The whole domain is invented, and it has to be, because nothing in the
# 138-table schema can hold an infected palm and the only proxy in the export -
# grading deductions - is degenerate at 0.25% mean across all 291 blocks.
#
# That makes the spatial design load-bearing. Ganoderma does not scatter at
# random: it spreads from inoculum in the soil, usually old infected stumps, so
# it appears as expanding foci and neighbouring palms fall in sequence. A map
# of randomly-sprinkled incidence reads as fake in about two seconds to anyone
# who has walked an estate, and once the client disbelieves this layer they
# disbelieve the rest.
#
# So incidence here is built from three things that a real outbreak has:
#   * a handful of foci, with incidence decaying by distance from each
#   * the latent soil-quality field, because stressed palms succumb sooner
#   * palm age, because basal stem rot is cumulative
#
# The other two pests follow their own real geography. Rhinoceros beetle breeds
# in rotting material and attacks the spear, so damage tracks canopy gaps. Rats
# come in from outside, so damage is an edge effect.

GANODERMA_FOCI = 4
# Quarterly census rounds. A real estate censuses once or twice a year; this is
# generous so the panel has a trend to show.
CENSUS_ROUNDS = [("R1", date(2024, 11, 15)), ("R2", date(2025, 2, 15)),
                 ("R3", date(2025, 5, 15))]
PEST_TREATMENTS = {
    "ganoderma": ("soil mounding + trunk injection", 180),
    "rhinoceros_beetle": ("pheromone trap + prophylactic", 90),
    "rat": ("bait station rotation", 60),
}


def gen_pest(blocks, latent, rng):
    """Palm census, scouting damage and treatment records.

    Returns the per-block incidence so the router and the spread model do not
    have to re-derive it from the CSV.
    """
    pts = {f["id"]: _centroid(f["geometry"]["coordinates"][0]) for f in blocks}
    xs = [p[0] for p in pts.values()]
    ys = [p[1] for p in pts.values()]
    w, e, s, n = min(xs), max(xs), min(ys), max(ys)
    cx, cy = (w + e) / 2, (s + n) / 2

    # Disease foci. Placed on real block centroids rather than free
    # coordinates, so every focus sits on ground that exists.
    focus_ids = rng.sample([f["id"] for f in blocks], GANODERMA_FOCI)
    foci = [(pts[i], rng.uniform(0.055, 0.115), rng.uniform(0.008, 0.018))
            for i in focus_ids]

    census, treatments, incidence = [], [], {}
    tid = 0
    for f in blocks:
        p = f["properties"]
        k = _bk(p)
        x, y = pts[f["id"]]
        palms = p.get("palms") or 0
        age = p.get("palm_age_years") or 10
        q = latent[f["id"]]

        # Distance-decayed pressure from every focus, taking the strongest.
        gano = 0.0
        for (fx, fy), peak, scale in foci:
            d2 = (x - fx) ** 2 + (y - fy) ** 2
            gano = max(gano, peak * math.exp(-d2 / (2 * scale ** 2)))
        # A stressed block succumbs sooner, and incidence accumulates with age.
        gano *= (1.0 - 0.22 * q) * (1.0 + 0.05 * (age - 9))
        gano = max(0.0, gano + rng.gauss(0.0015, 0.0025))

        # Beetle breeds in rotting material and goes for the spear; gappy
        # canopy is both cause and symptom.
        beetle = max(0.0, 0.012 * (1.0 - 0.35 * q) + rng.gauss(0, 0.004))
        # Rats work in from the perimeter, so damage is an edge effect.
        edge = max(abs(x - cx) / max(e - w, 1e-9), abs(y - cy) / max(n - s, 1e-9)) * 2
        rat = max(0.0, 0.010 * min(1.6, edge ** 1.7) + rng.gauss(0, 0.003))

        incidence[k] = {"ganoderma": gano, "beetle": beetle, "rat": rat}

        for ri, (rnd, when) in enumerate(CENSUS_ROUNDS):
            # Coverage is never complete. A census that claims every palm was
            # inspected is the first thing an agronomist disbelieves.
            cover = rng.uniform(0.62, 0.96)
            inspected = int(palms * cover)
            if inspected <= 0:
                continue
            # Incidence climbs between rounds: the disease does not pause.
            growth = 1.0 + 0.14 * ri
            conf = int(inspected * gano * growth)
            # Suspect always exceeds confirmed - field diagnosis of basal stem
            # rot is uncertain until the fruiting body appears.
            susp = conf + int(inspected * gano * growth * rng.uniform(0.4, 1.1))
            census.append({
                "round": rnd,
                "census_date": when.isoformat(),
                "division_code": p["division_code"],
                "block_code": p["block_code"],
                "palms_planted": palms,
                "palms_inspected": inspected,
                "coverage_pct": round(100 * cover, 1),
                "ganoderma_confirmed": conf,
                "ganoderma_suspect": susp,
                "beetle_damaged": int(inspected * beetle * growth),
                "rat_damaged": int(inspected * rat),
                "palms_felled": int(conf * rng.uniform(0.0, 0.45)),
            })

        # Treatment follows the worst pest on the block, and not every block
        # that needs one has had one. The gap is the operational finding.
        for pest, rate in (("ganoderma", gano), ("rhinoceros_beetle", beetle),
                           ("rat", rat)):
            if rate < 0.012 or rng.random() > 0.72:
                continue
            tid += 1
            method, interval = PEST_TREATMENTS[pest]
            done = date(2025, 5, 23) - timedelta(days=rng.randint(10, 320))
            treatments.append({
                "treatment_id": f"TR-{tid:05d}",
                "division_code": p["division_code"],
                "block_code": p["block_code"],
                "pest": pest,
                "method": method,
                "treated_date": done.isoformat(),
                "palms_treated": int(palms * rate * rng.uniform(1.1, 2.4)),
                "interval_days": interval,
                "followup_due": (done + timedelta(days=interval)).isoformat(),
                "days_overdue": max(0, (date(2025, 5, 23) - done).days - interval),
            })

    _write("ec_pest_census.csv", census,
           ["round", "census_date", "division_code", "block_code", "palms_planted",
            "palms_inspected", "coverage_pct", "ganoderma_confirmed",
            "ganoderma_suspect", "beetle_damaged", "rat_damaged", "palms_felled"],
           "Palm census. NO table in the 138-table EPMS schema can hold an "
           "infected palm, and the only proxy in the export - grading "
           "deductions - is degenerate at 0.25% mean. This entire domain is "
           "invented. Ganoderma is generated as decaying foci rather than "
           "scattered at random, because that is how basal stem rot actually "
           "spreads and a random map would be obviously false.")

    _write("ec_pest_treatment.csv", treatments,
           ["treatment_id", "division_code", "block_code", "pest", "method",
            "treated_date", "palms_treated", "interval_days", "followup_due",
            "days_overdue"],
           "Treatment records and follow-up dates. Invented; nothing in EPMS "
           "records a pest treatment.")

    worst = max(incidence.items(), key=lambda kv: kv[1]["ganoderma"])
    log.info("[synthetic] pest: %d census rows, %d treatments, %d foci, "
             "worst block %s at %.1f%% ganoderma", len(census), len(treatments),
             GANODERMA_FOCI, worst[0], 100 * worst[1]["ganoderma"])
    return {"pest_census": len(census), "pest_treatments": len(treatments)}


# ── nutrition: goods issues against an agronomy programme ──────────────────
#
# The existing cost ledger already carries a fertiliser line, but in rupiah,
# and an agronomic question cannot be answered in currency. Nutrition is judged
# in kilograms of nutrient per palm against a recommended programme, so this
# feed carries materials, quantities, dates and the target window they were
# meant to land in.
#
# The confounding in gen_costs is preserved deliberately: weak blocks get MORE
# fertiliser, not less, because that is how agronomists actually assign a
# programme. Any naive "more fertiliser, lower yield" reading of this data is
# wrong, and the clustering panel has to be built knowing that.

# Annual programme per mature palm, in kilograms of product.
AGRONOMY_PROGRAMME = {
    "NPK 12-12-17-2": 6.5,
    "Urea": 1.6,
    "Kieserite": 1.2,
    "Borate": 0.15,
}
# Nutrient fraction, for converting product to kg of N-P-K-Mg per palm.
NUTRIENT_PCT = {
    "NPK 12-12-17-2": {"N": .12, "P": .12, "K": .17, "Mg": .02},
    "Urea": {"N": .46, "P": 0, "K": 0, "Mg": 0},
    "Kieserite": {"N": 0, "P": 0, "K": 0, "Mg": .16},
    "Borate": {"N": 0, "P": 0, "K": 0, "Mg": 0},
}
# Two application rounds a year is standard for mature palm.
APPLICATION_WINDOWS = [("2024-10", date(2024, 10, 15)), ("2025-03", date(2025, 3, 15))]


def gen_fertiliser(blocks, latent, rng):
    """MM goods issues per block, against the agronomy programme."""
    rows, stock = [], []
    for f in blocks:
        p = f["properties"]
        palms = p.get("palms") or 0
        q = latent[f["id"]]
        if not palms:
            continue
        for win, target_date in APPLICATION_WINDOWS:
            for material, annual in AGRONOMY_PROGRAMME.items():
                target_kg = palms * annual / len(APPLICATION_WINDOWS)
                # Weak blocks are prescribed more. Delivery then falls short of
                # the prescription more often on the blocks furthest out, which
                # is the compounding failure worth showing.
                target_kg *= (1.0 - 0.15 * q)
                fulfil = min(1.12, max(0.35, rng.gauss(0.88, 0.17)))
                issued = target_kg * fulfil
                lag = int(max(0, rng.gauss(16, 26)))
                issue_date = target_date + timedelta(days=lag)
                rows.append({
                    "division_code": p["division_code"],
                    "block_code": p["block_code"],
                    "window": win,
                    "material": material,
                    "target_kg": round(target_kg, 1),
                    "issued_kg": round(issued, 1),
                    "target_date": target_date.isoformat(),
                    "issue_date": issue_date.isoformat(),
                    "application_lag_days": lag,
                    "palms": palms,
                    "kg_per_palm": round(issued / palms, 3),
                })

    for material, annual in AGRONOMY_PROGRAMME.items():
        rows_m = [r for r in rows if r["material"] == material]
        issued = sum(r["issued_kg"] for r in rows_m)
        stock.append({
            "material": material,
            "annual_programme_kg": round(sum(r["target_kg"] for r in rows_m)),
            "issued_kg": round(issued),
            "stock_on_hand_kg": round(issued * rng.uniform(0.08, 0.42)),
            "lead_time_days": rng.randint(21, 75),
            "supplier": rng.choice(["PT Pupuk Kaltim", "PT Petrokimia Gresik",
                                    "PT Meroke Tetap Jaya"]),
        })

    _write("ec_fertiliser.csv", rows,
           ["division_code", "block_code", "window", "material", "target_kg",
            "issued_kg", "target_date", "issue_date", "application_lag_days",
            "palms", "kg_per_palm"],
           "Fertiliser goods issues against the agronomy programme. SAP MM "
           "holds these; the EC export carries only a rupiah cost line, which "
           "cannot answer an agronomic question. NOTE the deliberate "
           "confounding: weak blocks are PRESCRIBED more, so a naive reading "
           "of 'more fertiliser, lower yield' is backwards.")

    _write("ec_fertiliser_stock.csv", stock,
           ["material", "annual_programme_kg", "issued_kg", "stock_on_hand_kg",
            "lead_time_days", "supplier"],
           "Warehouse stock and supplier lead times. SAP MM holds these.")
    return {"fertiliser": len(rows), "fertiliser_stock": len(stock)}


# ── roads ──────────────────────────────────────────────────────────────────

ROAD_CONDITIONS = ["good", "fair", "poor", "impassable-when-wet"]


def gen_roads(blocks, latent, rng):
    """Road segments along block edges, with condition and repair history.

    Geometry is taken from the shared edges of real block polygons, so every
    segment lies where a road plausibly runs on this estate rather than being
    drawn across the middle of a field.
    """
    mill = _mill_point(blocks)
    rows = []
    for i, f in enumerate(blocks):
        p = f["properties"]
        ring = f["geometry"]["coordinates"][0]
        # One segment per block, along its longest edge.
        best, blen = None, -1.0
        for a, b in zip(ring, ring[1:]):
            d = math.hypot(a[0] - b[0], a[1] - b[1])
            if d > blen:
                best, blen = (a, b), d
        cxy = _centroid(ring)
        dist_km = math.hypot(
            (cxy[0] - mill[0]) * 111.32 * math.cos(math.radians(cxy[1])),
            (cxy[1] - mill[1]) * 111.32)
        q = latent[f["id"]]
        # Low-lying, poorly-drained ground breaks up first, and the far end of
        # the estate gets graded last. Centred so the estate reads as one that
        # is actually operating: mostly good and fair, a real poor tail, and
        # only a handful impassable. An estate with 40% of its roads impassable
        # would not be harvesting at all, which is what the first calibration
        # produced and why the constant is low.
        score = 0.30 - 0.22 * q + 0.009 * dist_km + rng.gauss(0, 0.11)
        cond = ROAD_CONDITIONS[min(3, max(0, int(score * 4)))]
        last = date(2025, 5, 23) - timedelta(days=int(max(5, rng.gauss(210, 130))))
        rows.append({
            "segment_id": f"RS-{i + 1:04d}",
            "division_code": p["division_code"],
            "block_code": p["block_code"],
            "lon_a": round(best[0][0], 6), "lat_a": round(best[0][1], 6),
            "lon_b": round(best[1][0], 6), "lat_b": round(best[1][1], 6),
            "length_km": round(blen * 111.32, 3),
            "condition": cond,
            "condition_score": round(min(1.0, max(0.0, score)), 3),
            "last_graded": last.isoformat(),
            "days_since_graded": (date(2025, 5, 23) - last).days,
            "culvert_repairs_12m": rng.randint(0, 3) if score > 0.5 else 0,
        })
    _write("ec_roads.csv", rows,
           ["segment_id", "division_code", "block_code", "lon_a", "lat_a",
            "lon_b", "lat_b", "length_km", "condition", "condition_score",
            "last_graded", "days_since_graded", "culvert_repairs_12m"],
           "Estate road segments with condition and grading history. SAP PM "
           "holds the work orders; the export carries none. Segment geometry "
           "follows the longest edge of each real block polygon.")
    return {"roads": len(rows)}


# ── harvester-day output ───────────────────────────────────────────────────
#
# EPMS records harvest per worker in t_oph. The EC export is aggregated to
# block and month, so worker-level output has to be generated - but it is
# generated DOWN from the real block totals, not made up freely, so every
# worker-day sums back to a bunch count the client would recognise.

HARVESTER_BASE_BUNCHES = 95     # a fair day on flat ground, mature palm


def gen_harvester_days(blocks, latent, rng, months):
    """Per-worker daily output, summing back to the real block totals.

    The productivity model reads this against real terrain and real palm age.
    What is invented is who cut what on which day; the totals they add up to
    are the client's own.
    """
    from gis import environment
    terrain = environment.terrain_by_block("EC")

    # A roster per division, sized off planted area like the labour feed.
    by_div: dict[str, list] = {}
    for f in blocks:
        by_div.setdefault(f["properties"]["division_code"], []).append(f)
    workers: dict[str, list] = {}
    wid = 0
    for div, blks in sorted(by_div.items(), key=lambda kv: int(kv[0])):
        ha = sum(b["properties"].get("planted_ha") or 0 for b in blks)
        n = max(4, int(ha / HARVESTER_HA))
        roster = []
        for _ in range(n):
            wid += 1
            roster.append({
                "worker_id": f"W-{wid:04d}",
                # Persistent ability, which is what the model should recover
                # after terrain and stand are controlled for.
                "skill": round(rng.gauss(1.0, 0.13), 3),
            })
        workers[div] = roster

    rows = []
    for f in blocks:
        p = f["properties"]
        k = _bk(p)
        div = p["division_code"]
        slope = (terrain.get(k) or {}).get("slope_deg") or 2.5
        age = p.get("palm_age_years") or 10
        ha = p.get("planted_ha") or 0
        for mo in months:
            bunches_m = (p.get("bunches_by_month") or {}).get(mo) or 0
            days = _trip_days(p, mo, rng)
            if not bunches_m or not days:
                continue
            # Split the month's REAL total across its days exactly: the last
            # day takes the remainder, so the worker rows sum back to the
            # client's figure to the bunch rather than to within a rounding.
            per_day_list = [bunches_m // len(days)] * len(days)
            per_day_list[-1] += bunches_m - sum(per_day_list)
            per_day = per_day_list[0]
            # How hard this block is to cut. This is the effect the
            # productivity model is meant to recover, so it has to be in the
            # data: a cutter on a slope carries fruit further and uphill, tall
            # palms need the pole and slow the cut, and a thin stand means
            # walking between bunches.
            #
            # It is applied through crew SIZE. An earlier version put slope on
            # the row without letting it affect output, and the regression
            # still found a slope coefficient - an artifact of crew-size
            # quantisation. A synthetic feed that does not contain the effect
            # its model claims to find is worse than no feed at all.
            difficulty = (1.0
                          - 0.055 * (slope - 2.5)          # per degree
                          - 0.022 * (age - 10)             # palm height
                          + 0.10 * min(1.0, (bunches_m / ha if ha else 0) / 250.0))
            difficulty = max(0.55, min(1.45, difficulty))
            fair_day = HARVESTER_BASE_BUNCHES * difficulty

            for d, day_total in zip(days, per_day_list):
                # Crew sized so each cutter does a fair day for THIS block.
                crew_n = max(1, min(9, round(per_day / fair_day)))
                crew = rng.sample(workers[div], min(crew_n, len(workers[div])))
                # Split the REAL day total across the crew by skill, so the
                # worker rows always reconstruct the client's figure. Largest
                # remainder rounding, so the split sums to the day exactly.
                weights = [w["skill"] * rng.uniform(0.85, 1.15) for w in crew]
                tot_w = sum(weights) or 1.0
                shares = [day_total * wt / tot_w for wt in weights]
                cut = [int(x) for x in shares]
                for i in sorted(range(len(cut)), key=lambda i: shares[i] - cut[i],
                                reverse=True)[:day_total - sum(cut)]:
                    cut[i] += 1
                for w, b in zip(crew, cut):
                    if b <= 0:
                        continue
                    rows.append({
                        "worker_id": w["worker_id"],
                        "date": d.isoformat(),
                        "month": mo,
                        "division_code": div,
                        "block_code": p["block_code"],
                        "bunches_cut": b,
                        "loose_fruit_kg": round(b * rng.uniform(0.05, 0.32), 1),
                        "man_days": 1.0,
                        "slope_deg": slope,
                        "palm_age_years": age,
                        "bunch_density_per_ha": round(bunches_m / ha, 1) if ha else None,
                        "activity_code": "HARVEST",
                    })
    _write("ec_harvester_day.csv", rows,
           ["worker_id", "date", "month", "division_code", "block_code",
            "bunches_cut", "loose_fruit_kg", "man_days", "slope_deg",
            "palm_age_years", "bunch_density_per_ha", "activity_code"],
           "Per-worker daily output. EPMS records this in t_oph at employee "
           "grain; the EC export is aggregated to block and month. Generated "
           "DOWN from the real block totals, so every worker-day sums back to "
           "a bunch count the client would recognise. Terrain and palm age on "
           "each row are REAL.")
    return {"harvester_days": len(rows), "workers": wid}


# ── the long harvest history ───────────────────────────────────────────────
#
# The single most important synthetic feed, because it is the one the client
# most obviously already owns. EPMS holds t_oph going back years; the EC export
# was cut to 2025-01-01 to 2025-05-23. Under five months cannot identify a lag
# structure that is 24 months deep, so the lagged forecast is built on
# generated history and the LENGTH of that history is the ask.
#
# Construction, so the panel can say exactly what is real in it:
#   * the five recorded months are the client's own figures, carried through
#     unchanged and flagged observed=1
#   * earlier months are back-cast from them using REAL rainfall at the
#     agronomic lags, a seasonal term, and a slow yield trend with palm age
#
# The rainfall driving it is real. That is the point: the generated target is
# a plausible response to weather that actually happened, so the fitted lag
# importances land where an agronomist expects rather than on noise.

HISTORY_MONTHS = 36
# Oil palm sets inflorescence sex 20-24 months out and aborts bunches 8-10
# months out. These are the windows the back-cast responds to, and the same
# windows the model is later asked to rediscover.
SEX_LAG = (20, 24)
ABORT_LAG = (8, 10)


def _month_add(month: str, n: int) -> str:
    t = int(month[:4]) * 12 + int(month[5:7]) - 1 + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def gen_harvest_history(blocks, latent, rng, months):
    """36 months of block-level bunch counts, ending on the real window."""
    from gis import environment
    rain = environment.rainfall_by_month("EC")
    if not rain:
        log.warning("[synthetic] no rainfall pulled; history will have no "
                    "weather term. Run gis/build_rainfall.py first.")

    first_real = months[0]
    series = [_month_add(first_real, -n) for n in range(HISTORY_MONTHS - len(months), 0, -1)]
    series += list(months)

    # Estate-wide rainfall response at the two agronomic windows, computed once.
    def rain_term(mo):
        if not rain:
            return 0.0
        def mean(lo, hi):
            vals = [rain[_month_add(mo, -n)]["rain_mm"]
                    for n in range(lo, hi + 1) if _month_add(mo, -n) in rain]
            return sum(vals) / len(vals) if vals else None
        sex = mean(*SEX_LAG)
        abort = mean(*ABORT_LAG)
        if sex is None or abort is None:
            return 0.0
        # Centred on the estate's own long-run monthly mean so the term is a
        # deviation, not a level.
        base = sum(v["rain_mm"] for v in rain.values()) / len(rain)
        return 0.16 * (sex - base) / base + 0.11 * (abort - base) / base

    terms = {mo: rain_term(mo) for mo in series}

    rows = []
    for f in blocks:
        p = f["properties"]
        by_month = p.get("bunches_by_month") or {}
        real_mean = (sum(by_month.values()) / len(by_month)) if by_month else 0
        if not real_mean:
            continue
        age_now = p.get("palm_age_years") or 10
        q = latent[f["id"]]
        for mo in series:
            observed = mo in by_month
            if observed:
                bunches = by_month[mo]
            else:
                back = (int(first_real[:4]) * 12 + int(first_real[5:7])
                        - int(mo[:4]) * 12 - int(mo[5:7]))
                # Yield climbs towards the plateau, so earlier months sit lower.
                age_then = age_now - back / 12.0
                age_f = min(1.0, 0.72 + 0.031 * (age_then - 6))
                seas = 0.11 * math.sin((int(mo[5:7]) - 3) / 12.0 * 2 * math.pi)
                val = real_mean * age_f * (1 + seas) * (1 + terms.get(mo, 0.0))
                val *= (1 + 0.04 * q) * rng.uniform(0.90, 1.10)
                bunches = max(0, int(val))
            rows.append({
                "division_code": p["division_code"],
                "block_code": p["block_code"],
                "month": mo,
                "bunches": bunches,
                "planted_ha": p.get("planted_ha"),
                "palm_age_years": round(age_now - (len(series) - series.index(mo) - 1) / 12.0, 2),
                "observed": int(observed),
            })

    n_obs = sum(r["observed"] for r in rows)
    _write("ec_harvest_history.csv", rows,
           ["division_code", "block_code", "month", "bunches", "planted_ha",
            "palm_age_years", "observed"],
           f"{HISTORY_MONTHS} months of block-level harvest, {series[0]} to "
           f"{series[-1]}. The {len(months)} months flagged observed=1 are the "
           "client's REAL recorded figures. The rest are back-cast from them "
           "using REAL rainfall at the agronomic lags (sex determination "
           f"{SEX_LAG[0]}-{SEX_LAG[1]} months, abortion {ABORT_LAG[0]}-"
           f"{ABORT_LAG[1]} months), a seasonal term and an age trend. EPMS "
           "holds the real history; the export was cut to five months.")
    log.info("[synthetic] history: %d rows, %d observed, %d back-cast",
             len(rows), n_obs, len(rows) - n_obs)
    return {"harvest_history": len(rows), "history_observed": n_obs}


# ── entry point ────────────────────────────────────────────────────────────

def build() -> dict:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gis import ontology

    rng = random.Random(SEED)
    blocks = ontology.blocks_geojson("EC")["features"]
    months = ontology.estate_index()
    months = next(e["harvest_window"]["months"] for e in months
                  if e["estate_code"] == "EC")

    latent = _latent_field(blocks, rng)
    counts = {}
    counts["abw"] = gen_abw(blocks, latent, rng)
    forward = gen_forward_forecast(blocks, latent, rng, months)
    counts["costs"] = gen_costs(blocks, latent, rng, months, forward)
    counts["gangs"] = gen_gangs_and_rotation(blocks, rng, months)
    counts["vegetation"] = gen_vegetation(blocks, latent, rng, months, forward)
    v, o = gen_vendors_and_orders(blocks, rng, months, forward)
    counts["vendors"], counts["orders"] = v, o
    counts["labour"] = gen_labour(blocks, rng, months, forward)
    counts["upkeep"] = gen_upkeep(blocks, rng)
    counts.update(gen_transport(blocks, latent, rng, months))
    counts.update(gen_pest(blocks, latent, rng))
    counts.update(gen_fertiliser(blocks, latent, rng))
    counts.update(gen_roads(blocks, latent, rng))
    counts.update(gen_harvester_days(blocks, latent, rng, months))
    counts.update(gen_harvest_history(blocks, latent, rng, months))

    # The supply side: crews, attendance and the work-order ledger. Lives in
    # its own module because it simulates the estate day by day rather than
    # drawing columns, and reads the feeds above off disk to do it.
    from gis import build_operations
    ops = build_operations.build()
    counts.update({k: v for k, v in ops.items() if k != "checks"})
    counts["operations_checks"] = ops["checks"]

    # The store: SAP MM records reconciled to the ledger above. Last, on its own
    # seed, so nothing it draws can move a feed written before it.
    from gis import build_materials
    mat = build_materials.build()
    counts.update({k: v for k, v in mat.items() if k != "checks"})
    counts["materials_checks"] = mat["checks"]

    manifest = {
        "generated_for": "EC",
        "seed": SEED,
        "blocks": len(blocks),
        "observed_months": months,
        "forward_months": forward,
        "calibration": {
            "target_t_ha_yr": TARGET_T_HA_YR,
            "ffb_price_idr_kg": FFB_PRICE_IDR_KG,
            "cost_idr_ha_yr": sum(COST_IDR_HA_YR.values()),
        },
        "files": counts,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    m = build()
    print(f"\n  blocks {m['blocks']}, observed {len(m['observed_months'])} months, "
          f"forward {len(m['forward_months'])} months")
    for k, v in m["files"].items():
        print(f"  {k:12s} {v}")
    print(f"  -> {OUT}")
