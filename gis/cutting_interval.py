"""Realised cutting interval against target.

Answers "is the round actually being kept, block by block?" from the client's
own harvest export. The EPMS OPH snapshot (gis/data/estates/EC/oph.csv, from
gis/build_estate_data.py) carries one or more rows per block per harvest
date, so the distinct dates per block are the block's real cutting days, and
the interval between successive cuts is the block's realised round. That
figure is REAL.

Two things about the raw days matter and are handled here rather than left to
the reader:

  * A block is not cut in one day. A gang works a block over two or three
    consecutive days and comes back a week or more later, so the raw
    day-to-day gaps are bimodal: 1-2 days (the same visit continuing) and
    7-15 days (the round). The round is therefore measured between VISITS - a
    visit being a run of cutting days with at most one rest day between them -
    from the first day of one visit to the first day of the next.

  * The synthetic target in ec_rotation.csv was built on a different basis:
    calendar days per cutting day over January-February, clamped to 5-14. A
    block worked two consecutive days every ten gets a target of 5 while its
    round is 10. The target is still reported and compared as asked, but the
    like-for-like baseline is the block's own January-February round, which is
    real, and the split is explicit in the payload.

Loaded once and cached; the endpoint answers from memory after the first call.
"""
from __future__ import annotations

import csv
import logging
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

from gis import layers

log = logging.getLogger("estate-command.cutting_interval")

_ESTATES_DIR = Path(__file__).resolve().parent / "data" / "estates"
_CACHE: dict = {}
_LOCK = Lock()

# A cutting day within `REST_DAYS` of the previous one belongs to the same
# visit. One allows the Sunday break (Saturday -> Monday) without splitting a
# visit; the gap distribution has its valley at 4-5 days, so anything wider is
# the gang coming back for the next round.
REST_DAYS = 1
# The early-year window the synthetic target claims to reproduce, and the
# late window the trend is measured against.
EARLY_TO = date(2025, 2, 28)
LATE_FROM = date(2025, 4, 1)
# The round ceiling the synthetic target band tops out at. Not a client
# standard; used only to count rounds that ran long on the real data.
LONG_ROUND = 14

_MONTH_NAMES = {1: "January", 2: "February", 3: "March", 4: "April", 5: "May",
                6: "June", 7: "July", 8: "August", 9: "September",
                10: "October", 11: "November", 12: "December"}


# ── loading ────────────────────────────────────────────────────────────────

def _cutting_days(estate: str) -> dict[str, list[date]] | None:
    """Real cutting days per block, from the client's OPH export.

    Cut at the synthetic window's end: the target it is compared against, and
    the early/late windows above, belong to that window. The snapshot itself
    now runs well past it.
    """
    code = estate.upper()
    path = _ESTATES_DIR / code / "oph.csv"
    if not path.exists():
        return None
    through = layers.EXPORT_END
    days: dict[str, set] = defaultdict(set)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        head = next(rd)
        # EPMS spells this column 'divison_code' in the export. Kept as-is.
        i_date = head.index("harvest_date")
        i_div = head.index("divison_code")
        i_blk = head.index("block_code")
        for row in rd:
            if row[i_date] > through:
                continue
            days[layers._key(row[i_div], row[i_blk])].add(row[i_date])
    return {k: sorted(date.fromisoformat(d) for d in s) for k, s in days.items()}


def _visits(days: list[date]) -> list[tuple[date, date]]:
    """Runs of cutting days with at most REST_DAYS idle days between them."""
    out: list[list[date]] = [[days[0], days[0]]]
    for d in days[1:]:
        if (d - out[-1][1]).days <= REST_DAYS + 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


def _median(vals):
    return round(statistics.median(vals), 1) if vals else None


def _p90(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[max(0, -(-9 * len(s) // 10) - 1)]   # nearest-rank P90


def _month_label(m: str) -> str:
    return _MONTH_NAMES[int(m[5:7])]


def _compute(estate: str) -> dict:
    days_by_block = _cutting_days(estate)
    if not days_by_block:
        return {"available": False,
                "reason": f"No harvest export for {estate.upper()}: expected "
                          f"gis/data/estates/{estate.upper()}/oph.csv - run "
                          f"gis/build_estate_data.py."}

    rows = layers.block_rows(estate, synthetic_world=True) or []
    meta = {layers._key(r["division_code"], r["block_code"]): r for r in rows}
    rot = layers._state()["rotation"]
    anchor = date.fromisoformat(layers.EXPORT_END)

    lo = min(d[0] for d in days_by_block.values())
    hi = max(d[-1] for d in days_by_block.values())

    # Estate-wide closure: calendar days on which no block anywhere was cut.
    cut_any = {d for ds in days_by_block.values() for d in ds}
    closed = []
    d = lo
    while d <= hi:
        if d not in cut_any:
            closed.append(d)
        d += timedelta(days=1)
    # The longest closure is the one that stretches rounds. In 2025 that is
    # Lebaran: Idul Fitri fell on 31 March and the estate stopped 27-31 March.
    runs: list[list[date]] = []
    for c in closed:
        if runs and (c - runs[-1][-1]).days == 1:
            runs[-1].append(c)
        else:
            runs.append([c])
    longest = max(runs, key=len) if runs else []

    blocks = []
    all_iv: list[dict] = []
    for k, days in days_by_block.items():
        m = meta.get(k, {})
        r = rot.get(k) or {}
        vis = _visits(days)
        ivs = []
        for (a0, a1), (b0, b1) in zip(vis, vis[1:]):
            ivs.append({"days": (b0 - a0).days, "from": a0, "to": b0,
                        "straddles": bool(longest) and a1 < longest[0] and b0 > longest[-1]})
        n = [iv["days"] for iv in ivs]
        early = [iv["days"] for iv in ivs if iv["to"] <= EARLY_TO]
        late = [iv["days"] for iv in ivs if iv["to"] >= LATE_FROM]
        target = r.get("target")
        early_med = _median(early) if len(early) >= 2 else None
        late_med = _median(late) if len(late) >= 1 else None
        med = _median(n)
        last_cut = days[-1]
        blocks.append({
            "block_id": m.get("block_id"),
            "block_label": m.get("block_label") or k.replace("|", "-"),
            "division_code": m.get("division_code") or k.split("|")[0],
            "block_code": k.split("|")[1],
            "planted_ha": m.get("planted_ha"),
            "gang_code": r.get("gang_code"),
            "cutting_days": len(days),
            "visits": len(vis),
            "median_visit_days": _median([(b - a).days + 1 for a, b in vis]),
            "intervals": len(n),
            "median_round": med,
            "p90_round": _p90(n),
            "max_round": max(n) if n else None,
            "early_round": early_med,
            "late_round": late_med,
            "stretch_vs_own": (round(late_med / early_med, 2)
                               if early_med and late_med else None),
            "target": target,
            "stretch_vs_target": (round(med / target, 2) if med and target else None),
            "beyond_target_pct": (round(100.0 * sum(1 for x in n if x > target) / len(n), 1)
                                  if n and target else None),
            "beyond_14_pct": (round(100.0 * sum(1 for x in n if x > LONG_ROUND) / len(n), 1)
                              if n else None),
            "last_cut": last_cut.isoformat(),
            "gap_at_anchor": (anchor - last_cut).days,
            "synthetic_days_since": r.get("days_since"),
            "synthetic_last_cut": r.get("last_harvest_date"),
        })
        for iv in ivs:
            all_iv.append({**iv, "k": k, "div": blocks[-1]["division_code"],
                           "target": target})

    blocks.sort(key=lambda b: (b["division_code"], int(b["block_code"])))

    # ── by month: the round closed in that month ─────────────────────────
    months = sorted({iv["to"].strftime("%Y-%m") for iv in all_iv})
    series = []
    for mo in months:
        ivm = [iv for iv in all_iv if iv["to"].strftime("%Y-%m") == mo]
        vals = [iv["days"] for iv in ivm]
        # The buildplan's basis, kept so its figure can be checked: calendar
        # days in the month over the block's cutting days in it.
        y, mm = int(mo[:4]), int(mo[5:7])
        m_start = date(y, mm, 1)
        m_end = min(hi, date(y + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1))
        dpc = []
        for ds in days_by_block.values():
            c = sum(1 for x in ds if m_start <= x <= m_end)
            if c:
                dpc.append(((m_end - m_start).days + 1) / c)
        straddle = [iv["days"] for iv in ivm if iv["straddles"]]
        series.append({
            "month": mo,
            "label": _month_label(mo),
            "intervals": len(vals),
            "blocks": len({iv["k"] for iv in ivm}),
            "median": _median(vals),
            "p90": _p90(vals),
            "max": max(vals) if vals else None,
            "beyond_target_pct": round(100.0 * sum(1 for iv in ivm if iv["target"] and iv["days"] > iv["target"]) / len(ivm), 1) if ivm else None,
            "beyond_14_pct": round(100.0 * sum(1 for x in vals if x > LONG_ROUND) / len(vals), 1) if vals else None,
            "days_per_cutting_day": _median(dpc),
            "straddling_closure": len(straddle),
            "median_straddling": _median(straddle),
            "median_not_straddling": _median([iv["days"] for iv in ivm if not iv["straddles"]]),
            "partial": m_end < date(y + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1),
        })

    divs = sorted({b["division_code"] for b in blocks}, key=int)
    by_division = []
    for dv in divs:
        ivd = [iv for iv in all_iv if iv["div"] == dv]
        e = [iv["days"] for iv in ivd if iv["to"] <= EARLY_TO]
        l = [iv["days"] for iv in ivd if iv["to"] >= LATE_FROM]
        em, lm = _median(e), _median(l)
        by_division.append({
            "division_code": dv,
            "blocks": sum(1 for b in blocks if b["division_code"] == dv),
            "months": {mo: _median([iv["days"] for iv in ivd
                                    if iv["to"].strftime("%Y-%m") == mo]) for mo in months},
            "early_round": em,
            "late_round": lm,
            "stretch": round(lm / em, 2) if em and lm else None,
            "beyond_target_pct": round(100.0 * sum(1 for iv in ivd if iv["target"] and iv["days"] > iv["target"]) / len(ivd), 1) if ivd else None,
            "gap_over_14": sum(1 for b in blocks if b["division_code"] == dv
                               and b["gap_at_anchor"] > LONG_ROUND),
        })

    # Gang attribution rides on the invented roster; kept apart and labelled.
    by_gang: dict = defaultdict(lambda: {"blocks": 0, "early": [], "late": []})
    for b in blocks:
        if not b["gang_code"]:
            continue
        g = by_gang[b["gang_code"]]
        g["blocks"] += 1
        if b["early_round"]:
            g["early"].append(b["early_round"])
        if b["late_round"]:
            g["late"].append(b["late_round"])
    gangs = []
    for code, g in sorted(by_gang.items()):
        em, lm = _median(g["early"]), _median(g["late"])
        gangs.append({"gang_code": code, "blocks": g["blocks"], "early_round": em,
                      "late_round": lm, "stretch": round(lm / em, 2) if em and lm else None})
    gangs.sort(key=lambda g: -(g["stretch"] or 0))

    # ── totals ───────────────────────────────────────────────────────────
    all_days = [iv["days"] for iv in all_iv]
    early_all = [iv["days"] for iv in all_iv if iv["to"] <= EARLY_TO]
    late_all = [iv["days"] for iv in all_iv if iv["to"] >= LATE_FROM]
    with_target = [iv for iv in all_iv if iv["target"]]
    targets = [b["target"] for b in blocks if b["target"]]
    early_rounds = [b["early_round"] for b in blocks if b["early_round"]]
    em, lm = _median(early_all), _median(late_all)
    stretch = round(lm / em, 2) if em and lm else None
    beyond_target = (round(100.0 * sum(1 for iv in with_target if iv["days"] > iv["target"])
                           / len(with_target), 1) if with_target else None)
    beyond_14 = round(100.0 * sum(1 for x in all_days if x > LONG_ROUND) / len(all_days), 1)
    gap_over_14 = [b for b in blocks if b["gap_at_anchor"] > LONG_ROUND]
    target_med = _median(targets)
    early_block_med = _median(early_rounds)
    at_floor = sum(1 for t in targets if t == 5)
    last_mismatch = sum(1 for b in blocks if b["synthetic_last_cut"]
                        and b["synthetic_last_cut"] != b["last_cut"])
    latest = series[-1] if series else None
    first = series[0] if series else None

    most_stretched = sorted([b for b in blocks if b["stretch_vs_own"]],
                            key=lambda b: (-b["stretch_vs_own"], -(b["late_round"] or 0)))
    longest_gap = sorted(blocks, key=lambda b: (-b["gap_at_anchor"], -(b["median_round"] or 0)))

    summary = (
        f"The round has stretched. Blocks were cut about every {em:g} days in "
        f"January-February and every {lm:g} days in April-May, "
        f"{stretch:g} times as long; {len(gap_over_14)} of {len(blocks)} blocks "
        f"had gone more than {LONG_ROUND} days without a cut at "
        f"{anchor.strftime('%d %B %Y').lstrip('0')}."
        if em and lm and stretch and stretch >= 1.15 else
        f"The round is holding. Blocks were cut about every {em:g} days in "
        f"January-February and every {lm:g} days in April-May; "
        f"{len(gap_over_14)} of {len(blocks)} blocks had gone more than "
        f"{LONG_ROUND} days without a cut at {anchor.strftime('%d %B %Y').lstrip('0')}."
    )

    closure = None
    if longest:
        straddlers = [iv for iv in all_iv if iv["straddles"]]
        closure = {
            "from": longest[0].isoformat(), "to": longest[-1].isoformat(),
            "days": len(longest),
            "intervals_straddling": len(straddlers),
            "median_straddling": _median([iv["days"] for iv in straddlers]),
            "note": (f"No block anywhere was cut {longest[0].strftime('%d %b').lstrip('0')}-"
                     f"{longest[-1].strftime('%d %b').lstrip('0')} {longest[-1].year}: the "
                     f"Lebaran break (Idul Fitri fell on 31 March 2025). "
                     f"{len(straddlers)} rounds straddle it and are up to "
                     f"{len(longest)} days longer for that reason alone; the stretch "
                     f"persists through April and May after the break, so the "
                     f"holiday explains the spike, not the trend."),
        }

    return {
        "available": True,
        "estate": estate.upper(),
        "summary": summary,
        "window": {"from": lo.isoformat(), "to": hi.isoformat(), "anchor": anchor.isoformat(),
                   "rest_days_within_visit": REST_DAYS,
                   "early": f"{lo.isoformat()}..{EARLY_TO.isoformat()}",
                   "late": f"{LATE_FROM.isoformat()}..{hi.isoformat()}"},
        "totals": {
            "blocks": len(blocks),
            "blocks_with_target": len(targets),
            "cutting_days": sum(b["cutting_days"] for b in blocks),
            "visits": sum(b["visits"] for b in blocks),
            "intervals": len(all_days),
            "median_round": _median(all_days),
            "p90_round": _p90(all_days),
            "max_round": max(all_days) if all_days else None,
            "median_visit_days": _median([b["median_visit_days"] for b in blocks
                                          if b["median_visit_days"]]),
            "early_round": em,
            "late_round": lm,
            "stretch": stretch,
            "first_month_median": first["median"] if first else None,
            "latest_month_median": latest["median"] if latest else None,
            "latest_month": latest["label"] if latest else None,
            "beyond_target_pct": beyond_target,
            "beyond_14_pct": beyond_14,
            "gap_over_14_blocks": len(gap_over_14),
            "gap_over_14_ha": round(sum(b["planted_ha"] or 0 for b in gap_over_14), 1),
            "median_gap_at_anchor": _median([b["gap_at_anchor"] for b in blocks]),
            "closed_days": [c.isoformat() for c in closed],
        },
        "target": {
            "source": "synthetic: gis/data/synthetic/ec_rotation.csv rotation_target_days",
            "basis": ("calendar days per cutting day over January-February, "
                      "clamped to 5-14"),
            "median": target_med,
            "at_floor_5": at_floor,
            "distribution": {str(t): targets.count(t) for t in sorted(set(targets))},
            "early_round_same_blocks": early_block_med,
            "runs_low_by_days": (round(early_block_med - target_med, 1)
                                 if early_block_med and target_med else None),
            "last_cut_disagrees_blocks": last_mismatch,
            "note": (f"The target counts calendar days per cutting day, not days "
                     f"between visits, so a block worked two days running every "
                     f"ten gets a target of 5 while its round is 10. Median target "
                     f"{target_med:g} against a median realised January-February "
                     f"round of {early_block_med:g} on the same blocks; "
                     f"{at_floor} of {len(targets)} targets sit on the 5-day floor. "
                     f"Read 'beyond target' with that in mind; the like-for-like "
                     f"test is each block against its own early-year round."),
        },
        "series": series,
        "by_division": by_division,
        "by_gang": gangs,
        "_most_stretched": most_stretched,
        "_longest_gap": longest_gap,
        "closure": closure,
        "buildplan_check": {
            "claimed": "median realised interval 5.2 days in January and 7.7 in May",
            "days_per_cutting_day": {s["label"]: s["days_per_cutting_day"] for s in series},
            "round_between_visits": {s["label"]: s["median"] for s in series},
            "verdict": ("The 5.2 and 7.7 reproduce exactly as calendar days per "
                        "cutting day, which is a work-rate figure. The round a "
                        "palm sees, visit to visit, is roughly twice that."),
        },
        "learns": {
            "on_own_data": [
                "The realised round per block, visit to visit, and how it drifted from January to May.",
                "Which blocks have gone longest without a cut at the export's last day.",
                f"The {len(longest) if longest else 0}-day Lebaran stop and how many rounds it stretched.",
                "Which divisions stretched most - the trend is real down to division level.",
            ],
            "synthetic_adds": [
                "A per-block target round to grade against. Invented, and built on a basis that runs low.",
                "A gang on each block, so the stretch can be attributed to a crew. Invented.",
                "A 'days since harvest' that disagrees with the client's own last cutting day on "
                f"{last_mismatch} of {len(blocks)} blocks; this panel uses the real day.",
            ],
        },
        "provenance": ("real: cutting days and every interval from the client's EPMS OPH "
                       "export; synthetic: the target round and the gang on each block"),
        "note": ("A visit is a run of cutting days with at most one rest day between "
                 "them; the round is measured from the first day of one visit to the "
                 "first day of the next. Each interval is filed under the month the "
                 "return cut fell in."),
        "caveat": ("May is cut short at the export's last day, so a round still open on "
                   f"{anchor.strftime('%d %B').lstrip('0')} is not in the May median; those "
                   "blocks show in the longest-gap list instead, which understates May. "
                   "The target's basis runs low against a visit-to-visit round, so "
                   "'beyond target' overstates; the block's own January-February round "
                   "is the fair yardstick. Sunday cutting runs at under half the weekday "
                   "rate, which the one-rest-day rule absorbs."),
    }


def position(estate: str = "EC", top: int = 12) -> dict:
    """Realised cutting interval per block against its target round."""
    code = estate.upper()
    with _LOCK:
        if code not in _CACHE:
            try:
                _CACHE[code] = _compute(code)
            except Exception as exc:  # a broken export must not take the panel down
                log.exception("[cutting_interval] %s failed", code)
                return {"available": False, "reason": f"cutting interval failed: {exc}"}
        d = _CACHE[code]
    if not d.get("available"):
        return d
    top = max(1, min(50, int(top or 12)))
    out = dict(d)
    # The cached lists are full; the endpoint trims to `top`.
    out["most_stretched"] = d["_most_stretched"][:top]
    out["longest_gap"] = d["_longest_gap"][:top]
    return {k: v for k, v in out.items() if not k.startswith("_")}


def reload() -> None:
    with _LOCK:
        _CACHE.clear()
