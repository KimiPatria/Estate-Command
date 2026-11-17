"""
The data contract every source's extract is held to (buildplan_forecast_mlops.md Phase 3).

Files per source and estate, in forecast/<source series_dir>/<CODE>/:

  oph.csv      one row per block per harvest day.
               CORE columns, required from every source, are all the forecast reads:
                 harvest_date   YYYY-MM-DD
                 division_code  as the source spells it
                 block_code     as the source spells it
                 bunches_total  non-negative bunch count
               NATIVE columns follow, declared per adapter (adapters/*.py,
               column_registry) and never renamed into each other or into a core
               name: live keeps its 14 grade columns, staging its harvester count.
  monthly.csv  month, bunches_total, n_harvest_days, days_covered, days_in_month,
               is_partial (a month is partial below COMPLETE_SHARE of its days)
  meta.json    window, row counts, exclusions, gate result, the contract
               version, and a `columns` block: for every declared column, whether
               the source has it, its fill rate at the source's own grain, its
               share of bunches where that applies, the months it is filled, and
               a status (populated / sparse / empty). The page's "what data does
               each source give us" table is read from that block only.

Any source whose extract passes validate() and gate() is a valid source:
a restored backup, a direct database read or a delivered file drop.
"""

import pandas as pd

try:                                   # run from forecast/ (build_series, adapters)
    import estate_config
except ImportError:                    # imported from the repo root (forecast_compare)
    from forecast import estate_config

CONTRACT_VERSION = estate_config.contract_version()
CORE_COLUMNS = ["harvest_date", "division_code", "block_code", "bunches_total"]
COMPLETE_SHARE = 0.9          # as forecast/lab/registry.py: a month counts when >= 90% recorded

# Shared vocabulary for the comparison: each native column names the data item it
# carries, so the two sources line up by item while keeping their own headers.
ITEMS = {
    "bunches_total": "Bunches harvested",
    "bunches_ripe": "Ripe bunches",
    "bunches_overripe": "Overripe bunches",
    "bunches_underripe": "Underripe bunches",
    "bunches_unripe": "Unripe bunches",
    "bunches_wet": "Wet bunches",
    "bunches_rotten": "Rotten bunches",
    "bunches_long_stalk": "Long-stalk bunches",
    "bunches_empty": "Empty bunches",
    "bunches_dirty": "Dirty bunches",
    "bunches_unfresh": "Unfresh bunches",
    "bunches_old": "Old bunches",
    "bunches_pest_damaged_old": "Pest-damaged bunches (old)",
    "bunches_pest_damaged_new": "Pest-damaged bunches (new)",
    "bunches_diseased": "Diseased bunches",
    "bunches_not_sent": "Bunches not sent",
    "loose_fruit": "Loose fruit",
    "tickets": "Harvest tickets",
    "harvest_point": "Harvest point",
    "harvester": "Harvester",
    "harvest_card": "Harvest card",
    "foreman": "Foreman",
    "checker": "Checker",
    "method": "Harvest method",
    "transport": "Carrier and transport distance",
    "gps": "GPS position",
    "rain_flag": "On-site rain",
    "rain_duration": "Rain duration",
    "weighbridge": "Weighbridge tonnes",
    "weather": "Weather (joined from outside)",
}


class ContractError(ValueError):
    """An extract does not meet the contract; the message says how."""


def column(source_name, item, unit="", written=None, agg=None, grain="harvest record"):
    """One declared native column.

    source_name  the header in the source (ZMASAK, oph_tph_code, ...)
    item         key of ITEMS
    written      header in oph.csv when the column is carried into it, else None
                 (measured for the comparison only)
    agg          how block-day rows are formed from source rows: sum | count_distinct | count
    """
    if item not in ITEMS:
        raise ContractError(f"unknown data item {item!r}")
    if written in CORE_COLUMNS and item != "bunches_total":
        raise ContractError(f"native column {source_name!r} may not be written as core column {written!r}")
    return {"source_name": source_name, "item": item, "label": ITEMS[item], "unit": unit,
            "written": written, "agg": agg, "grain": grain}


def status(fill_rate, thresholds=None) -> str:
    t = thresholds or estate_config.thresholds()
    if fill_rate is None:
        return "not available"
    if fill_rate >= t["populated"]:
        return "populated"
    if fill_rate >= t["sparse"]:
        return "sparse"
    return "empty"


def validate(blocks: pd.DataFrame, registry: list) -> None:
    """Raise ContractError unless the block-day frame meets the contract."""
    missing = [c for c in CORE_COLUMNS if c not in blocks.columns]
    if missing:
        raise ContractError(f"core columns missing: {missing}")
    written = [c["written"] for c in registry if c["written"] and c["written"] not in CORE_COLUMNS]
    clash = [w for w in written if w in CORE_COLUMNS]
    if clash:
        raise ContractError(f"native columns written under core names: {clash}")
    absent = [w for w in written if w not in blocks.columns]
    if absent:
        raise ContractError(f"declared native columns missing from the extract: {absent}")
    if blocks["harvest_date"].isna().any():
        raise ContractError("harvest_date has empty values")
    if not pd.api.types.is_numeric_dtype(blocks["bunches_total"]):
        raise ContractError("bunches_total is not numeric")
    if (blocks["bunches_total"] < 0).any():
        raise ContractError("bunches_total has negative values")


def check_version(meta: dict, where: str = "") -> None:
    """Loaders call this: refuse files written under another contract version."""
    v = (meta or {}).get("contract_version")
    if v is not None and int(v) != CONTRACT_VERSION:
        raise ContractError(f"{where or 'extract'} was written under contract version {v}; "
                            f"this code reads version {CONTRACT_VERSION}. Rebuild it.")


def columns_block(registry: list, stats: dict, bunches: float, thresholds=None) -> dict:
    """meta.json `columns`: per declared column, present / fill rate / share / months / status.

    stats[source_name] = {"rows", "filled", "sum" (optional), "first_month", "last_month"}
    measured at the source's own grain (adapter.column_stats)."""
    out = {}
    for c in registry:
        st = stats.get(c["source_name"])
        present = st is not None
        fill = round(st["filled"] / st["rows"], 4) if present and st.get("rows") else (0.0 if present else None)
        share = None
        if present and st.get("sum") is not None and bunches and c["item"].startswith("bunches"):
            share = round(float(st["sum"]) / bunches, 4)
        out[c["source_name"]] = {
            "item": c["item"], "label": c["label"], "unit": c["unit"], "grain": c["grain"],
            "written": c["written"], "present": present, "fill_rate": fill, "share_of_bunches": share,
            "first_month": st.get("first_month") if present else None,
            "last_month": st.get("last_month") if present else None,
            "status": status(fill, thresholds) if present else "not available",
            **({"note": st["note"]} if present and st.get("note") else {}),
        }
    return out


# ── unchanged from forecast/build_staging_series.py (2026-11-10) ───────────

def monthly(daily):
    """Monthly totals with how much of each month the recording window covers.

    `daily` is the estate's daily frame. A month is partial when the window
    (first to last recorded day) covers under COMPLETE_SHARE of its days --
    the registry's rule, so the page and the lab agree on which months count.
    """
    s = daily.set_index("harvest_date")["bunches_total"].sort_index()
    first, last = s.index.min(), s.index.max()
    rows = []
    for p in pd.period_range(first.to_period("M"), last.to_period("M"), freq="M"):
        lo, hi = max(p.start_time, first), min(p.end_time.normalize(), last)
        covered = (hi - lo).days + 1
        part = s.loc[lo:hi]
        rows.append({"month": str(p), "bunches_total": float(part.sum()),
                     "n_harvest_days": int((part > 0).sum()),
                     "days_covered": covered, "days_in_month": p.days_in_month,
                     "is_partial": int(covered / p.days_in_month < COMPLETE_SHARE)})
    return pd.DataFrame(rows)


def gate(blocks, daily, months):
    """The block file, the daily total and the monthly file must be one set of numbers.

    Returns {"ok", "checks": [{"name", "ok", "detail"}]}.
    """
    tol = 0.5                     # counts are whole bunches
    checks = []

    by_day = blocks.groupby("harvest_date")["bunches_total"].sum()
    ref = daily.set_index("harvest_date")["bunches_total"]
    diff = by_day.reindex(ref.index).fillna(0.0) - ref
    bad = diff[diff.abs() > tol]
    checks.append({"name": "block file adds up to the estate total, day for day",
                   "ok": bad.empty and len(by_day) == len(ref),
                   "detail": (f"{len(ref)} days, max difference {float(diff.abs().max()):,.1f}"
                              if bad.empty else
                              f"{len(bad)} days differ, first {bad.index[0].date()} by {float(bad.iloc[0]):,.0f}")})

    m_ref = ref.groupby(ref.index.to_period("M")).sum()
    m_got = months.set_index(pd.PeriodIndex(months["month"], freq="M"))["bunches_total"]
    m_diff = (m_got - m_ref).abs()
    checks.append({"name": "monthly file adds up to the daily totals",
                   "ok": bool((m_diff <= tol).all()) and len(m_got) == len(m_ref),
                   "detail": f"{len(m_got)} months, max difference {float(m_diff.max()):,.1f}"})

    no_div = blocks[blocks["division_code"].isna() | (blocks["division_code"] == "")]
    checks.append({"name": "every block has a division",
                   "ok": no_div.empty,
                   "detail": ("all blocks matched a current BLK row" if no_div.empty else
                              f"{no_div['block_code'].nunique()} blocks without one: "
                              + ", ".join(sorted(no_div['block_code'].dropna().unique())[:8]))})

    keys = blocks[["division_code", "block_code"]].drop_duplicates()
    twice = keys[keys.duplicated("block_code", keep=False)]
    checks.append({"name": "no block sits in two divisions",
                   "ok": twice.empty,
                   "detail": ("each block code maps to one division" if twice.empty else
                              f"{twice['block_code'].nunique()} block codes under more than one division")})

    neg = blocks[blocks["bunches_total"] < 0]
    checks.append({"name": "no negative counts", "ok": neg.empty,
                   "detail": "none" if neg.empty else f"{len(neg)} negative block-days"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
