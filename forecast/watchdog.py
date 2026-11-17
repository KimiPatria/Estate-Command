"""
Data-quality watchdog for the forecast series, both sources.

Reads the extracted series files of the run being served (forecast/runs/<run>/series/
<source>/<CODE>/: meta.json, daily.csv, monthly.csv, oph.csv, weight_monthly.csv) and
says, in plain words, what is wrong with the data and what that does to the forecast.
It never changes a file or a forecast. forecast/monitor.py watches the *model*
(realised error, seasonal shape, column drift); this watches the *data* under it.

    python forecast/watchdog.py                  # print the findings
    python forecast/watchdog.py --out <file>     # write the report (refresh.py does this)

Each finding is
    {"id", "source", "estate", "check", "dimension", "level", "title", "detail",
     "affects", "months", "days"}
level: "alert" (act on it), "warn" (watch it), "ok", "info" (cannot be judged).
dimension: one of DIMENSIONS, the usual data-quality dimensions.

Checks, per source and estate:
  freshness        days between the last harvest recorded and today
  extract_gate     the extract's own reconciliation checks (meta.json gate)
  history_depth    complete months available for the model to learn the season from
  harvest_gaps     runs of calendar days with no harvest inside the recorded window
  short_months     complete months with far fewer harvest days than usual
  daily_spikes     a day far above the days around it (a double load, a backlog posted on one day)
  block_coverage   months where many fewer blocks reported than usual
  duplicates       the same day (daily) or day + block (block file) recorded twice
  ticket_ratio     bunches per harvest ticket moving far from its usual level (a recording change)
  target_fill      share of harvest tickets with no bunch count
  weight_per_trip  staging tonnes per weighbridge trip outside what a truck carries
and, per estate held in both sources:
  cross_source     monthly totals (and the days behind them) that differ between the
                   live estate database and the staging database
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import estate_config as ec   # noqa: E402

DIMENSIONS = ("timeliness", "completeness", "validity", "uniqueness", "consistency",
              "plausibility", "sufficiency")
LEVEL_RANK = {"alert": 3, "warn": 2, "ok": 1, "info": 0}

T = {
    "fresh_ok_days": 40, "fresh_warn_days": 90,
    "history_ok_months": 24, "history_warn_months": 12,
    "gap_warn_days": 7, "gap_alert_days": 14,
    "short_month_share": 0.70,            # harvest days below 70% of the usual month
    "spike_warn": 2.0, "spike_alert": 3.0, "spike_min_share": 0.01,
    "spike_window": 29,                   # centred window of harvest days
    "batch_days_per_month": 1.0,          # more spikes than this a month = batch posting
    "blocks_warn": 0.75, "blocks_alert": 0.50, "blocks_window": 6,
    "ratio_warn": 0.40,                   # bunches per ticket off its median by 40%
    "fill_warn": 0.85, "fill_alert": 0.70,   # ~10% of tickets are loose fruit only on every estate
    "trip_tonnes": (2.0, 15.0), "trip_min_trips": 20,
    "xsrc_ok_pct": 1.0, "xsrc_warn_pct": 5.0, "xsrc_day_pct": 2.0,
}


# ── helpers ─────────────────────────────────────────────────────────────────

def _f(source, estate, check, dimension, level, title, detail="", affects="", months=None, days=None):
    return {"id": f"{source}/{estate}/{check}" + (f"/{(months or days or [''])[0]}" if (months or days) else ""),
            "source": source, "estate": estate, "check": check, "dimension": dimension,
            "level": level, "title": title, "detail": detail, "affects": affects,
            "months": list(months or []), "days": list(days or [])}


def _n(v) -> str:
    return f"{v:,.0f}"


def _read_csv(path, **kw):
    return pd.read_csv(path, **kw) if os.path.exists(path) else None


def _meta(d):
    p = os.path.join(d, "meta.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _months_between(a: str, b: str) -> int:
    """Whole months from period a (YYYY-MM) to period b."""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def _ago(day: str, today: date) -> int:
    return (today - datetime.strptime(day[:10], "%Y-%m-%d").date()).days


# ── per-estate checks ───────────────────────────────────────────────────────

def freshness(s, e, meta, today):
    day = (meta or {}).get("last_day")
    if not day:
        return [_f(s, e, "freshness", "timeliness", "info", "No data-through date in the extract")]
    age = _ago(day, today)
    last_full = meta.get("last_full_month") or day[:7]
    cur = today.strftime("%Y-%m")
    behind = max(0, _months_between(last_full, cur) - 1)
    level = "ok" if age <= T["fresh_ok_days"] else "warn" if age <= T["fresh_warn_days"] else "alert"
    title = f"Data is {age} days old" if level != "ok" else f"Data is current ({age} days old)"
    affects = ("" if level == "ok" else
               f"A forecast built on this data starts after {last_full}, so its first {behind} "
               f"month{'s' if behind != 1 else ''} {'are' if behind != 1 else 'is'} already past and cannot be "
               f"checked against actuals until the data is refreshed.")
    return [_f(s, e, "freshness", "timeliness", level, title, f"Last harvest recorded {day}.", affects)]


def extract_gate(s, e, meta):
    gate = (meta or {}).get("gate") or {}
    checks = gate.get("checks") or []
    if not checks:
        return [_f(s, e, "extract_gate", "consistency", "info", "The extract recorded no checks")]
    bad = [c for c in checks if not c.get("ok")]
    if bad:
        return [_f(s, e, "extract_gate", "consistency", "alert",
                   f"{len(bad)} extract check{'s' if len(bad) > 1 else ''} failed",
                   "; ".join(f"{c['name']}: {c.get('detail', '')}" for c in bad),
                   "The files do not add up to each other, so totals on every page can be wrong.")]
    return [_f(s, e, "extract_gate", "consistency", "ok", f"All {len(checks)} extract checks passed",
               "Blocks add up to the estate, days add up to months, no negative counts.")]


def history_depth(s, e, meta):
    n = int((meta or {}).get("full_months") or 0)
    first = (meta or {}).get("first_full_month") or "?"
    level = ("ok" if n >= T["history_ok_months"] else "warn" if n >= T["history_warn_months"] else "alert")
    if level == "ok":
        return [_f(s, e, "history_depth", "sufficiency", "ok", f"{n} complete months of history",
                   f"From {first}.")]
    affects = ("Less than a year: the yearly harvest pattern has not been seen once, so the model cannot "
               "tell a seasonal peak from a trend." if level == "alert" else
               "Under two years: the yearly pattern has been seen about once, so the likely range is wide "
               "and the accuracy figures rest on few test points.")
    return [_f(s, e, "history_depth", "sufficiency", level,
               f"Only {n} complete month{'s' if n != 1 else ''} of history",
               f"From {first}.", affects)]


HANDLED = ("The model already treats {m} as under-recorded and fills it in from the seasonal "
           "pattern, so the forecast is not pulled down by it.")


def _handle(f, handled):
    """A gap or short month the model already imputes (K3's features file) is information,
    not a warning: the data is still short, the forecast is not misled by it."""
    if f["level"] in ("warn", "alert") and f["months"] and set(f["months"]) <= handled:
        f = dict(f, level="info", affects=HANDLED.format(m=" and ".join(f["months"])))
    return f


def handled_months(source: str, code: str) -> set:
    """Months the model imputes for this estate (is_underrecorded or exclude_from_model in
    the estate's features file); empty for estates served from the series alone."""
    path = ec.estates(source).get(code, {}).get("features")
    if not path:
        return set()
    full = os.path.join(ec.REPO, *path.split("/"))
    f = _read_csv(full, usecols=["month", "is_underrecorded", "exclude_from_model"], dtype={"month": str})
    if f is None:
        return set()
    return set(f.loc[(f["is_underrecorded"] == 1) | (f["exclude_from_model"] == 1), "month"])


def harvest_gaps(s, e, daily):
    if daily is None or daily.empty:
        return []
    days = pd.to_datetime(daily.loc[daily["bunches_total"] > 0, "harvest_date"]).sort_values()
    if len(days) < 2:
        return []
    gap = days.diff().dt.days - 1               # calendar days with no harvest between two harvest days
    out = []
    for i in gap.index[gap >= T["gap_warn_days"]]:
        n = int(gap[i])
        end = days[i]
        start = end - pd.Timedelta(days=n)
        level = "alert" if n >= T["gap_alert_days"] else "warn"
        months = sorted({str(p) for p in pd.period_range(start, end - pd.Timedelta(days=1), freq="M")})
        out.append(_f(s, e, "harvest_gaps", "completeness", level,
                      f"No harvest recorded for {n} days",
                      f"{start.date()} to {(end - pd.Timedelta(days=1)).date()}.",
                      f"{' and '.join(months)} read{'s' if len(months) == 1 else ''} low by those days: "
                      "a holiday stop is real, a missing upload is not, and the model cannot tell them apart.",
                      months=months, days=[str(start.date())]))
    if not out:
        out.append(_f(s, e, "harvest_gaps", "completeness", "ok",
                      f"No gap of {T['gap_warn_days']} days or more"))
    return out


def short_months(s, e, monthly):
    if monthly is None or monthly.empty or "n_harvest_days" not in monthly:
        return []
    full = monthly[monthly["is_partial"] == 0]
    if len(full) < 4:
        return []
    usual = float(full["n_harvest_days"].median())
    low = full[full["n_harvest_days"] < usual * T["short_month_share"]]
    out = []
    for _, r in low.iterrows():
        out.append(_f(s, e, "short_months", "completeness", "warn",
                      f"{r['month']} has {int(r['n_harvest_days'])} harvest days",
                      f"Against {usual:.0f} in a usual month.",
                      f"{r['month']}'s total is counted as a full month, so it reads as a drop in production.",
                      months=[str(r["month"])]))
    return out or [_f(s, e, "short_months", "completeness", "ok", "Every complete month has its usual harvest days")]


def daily_spikes(s, e, daily):
    if daily is None or len(daily) < T["spike_window"]:
        return []
    d = daily[daily["bunches_total"] > 0].copy()
    d["harvest_date"] = pd.to_datetime(d["harvest_date"])
    d = d.set_index("harvest_date")["bunches_total"].astype(float)
    med = d.rolling(T["spike_window"], center=True, min_periods=10).median()
    ratio = d / med
    month_tot = d.groupby(d.index.to_period("M")).transform("sum")
    excess_share = (d - med) / month_tot
    hit = ratio[(ratio >= T["spike_warn"]) & (excess_share >= T["spike_min_share"])]
    n_months = d.index.to_period("M").nunique()
    if len(hit) > T["batch_days_per_month"] * n_months:
        # Harvest posted in batches (K3: a few tickets most days, 300-470 on a few):
        # a "spike" is a posting day, not a double load. Monthly totals are unaffected.
        top = d.groupby(d.index.to_period("M")).apply(lambda x: x.nlargest(max(1, len(x) // 5)).sum() / x.sum())
        return [_f(s, e, "daily_spikes", "plausibility", "info",
                   "Harvest is posted in batches",
                   f"About {len(hit) / n_months:.1f} large posting days a month; the busiest fifth of the days "
                   f"carries {top.median():.0%} of a month's bunches.",
                   "Daily figures are posting days, not harvest days, so one-day spikes cannot be judged and "
                   "any daily or weekly view of this estate is lumpy. Monthly totals, and the monthly "
                   "forecast, are not affected.")]
    out = []
    for day, r in hit.sort_values(ascending=False).head(5).items():
        m = str(day.to_period("M"))
        out.append(_f(s, e, "daily_spikes", "plausibility", "alert" if r >= T["spike_alert"] else "warn",
                      f"{day.date()} is {r:.1f}x a normal day",
                      f"{_n(d[day])} bunches against about {_n(med[day])} on the days around it.",
                      f"If it is a double load or a backlog posted on one day, it adds about "
                      f"{excess_share[day] * 100:.0f}% to {m}'s total.",
                      months=[m], days=[str(day.date())]))
    return out or [_f(s, e, "daily_spikes", "plausibility", "ok", "No day stands far above the days around it")]


def block_coverage(s, e, oph, monthly):
    if oph is None or oph.empty or monthly is None:
        return []
    full = set(monthly.loc[monthly["is_partial"] == 0, "month"].astype(str))
    o = oph[oph["bunches_total"] > 0]
    month = pd.to_datetime(o["harvest_date"]).dt.to_period("M").astype(str)
    n = o.assign(month=month).groupby("month")["block_code"].nunique()
    n = n[n.index.isin(full)].sort_index()
    if len(n) < T["blocks_window"] + 1:
        return []
    usual = n.rolling(T["blocks_window"], min_periods=3).median().shift(1)
    share = n / usual
    out = []
    for m, sh in share.dropna().items():
        if sh < T["blocks_warn"]:
            out.append(_f(s, e, "block_coverage", "completeness", "alert" if sh < T["blocks_alert"] else "warn",
                          f"{m}: {int(n[m])} blocks reported",
                          f"About {usual[m]:.0f} usually.",
                          f"The blocks missing from {m} add nothing to its total, so the month reads low.",
                          months=[m]))
    return out or [_f(s, e, "block_coverage", "completeness", "ok", f"Blocks reporting each month are steady (about {n.iloc[-1]})")]


def duplicates(s, e, daily, oph):
    out = []
    if daily is not None and daily["harvest_date"].duplicated().any():
        dup = daily.loc[daily["harvest_date"].duplicated(), "harvest_date"].astype(str).tolist()
        out.append(_f(s, e, "duplicates", "uniqueness", "alert", f"{len(dup)} day(s) recorded twice in the daily file",
                      ", ".join(dup[:5]), "Those days are counted twice in their month's total.", days=dup[:5]))
    if oph is not None and not oph.empty:
        key = ["harvest_date", "division_code", "block_code"]
        n = int(oph.duplicated(key).sum())
        if n:
            ex = oph.loc[oph.duplicated(key), "harvest_date"].astype(str).unique().tolist()
            out.append(_f(s, e, "duplicates", "uniqueness", "alert", f"{n} block-day rows recorded twice",
                          "First on " + ", ".join(ex[:3]) + ".",
                          "Block totals and the maps double-count those blocks on those days.", days=ex[:3]))
    return out or [_f(s, e, "duplicates", "uniqueness", "ok", "No day or block-day recorded twice")]


def ticket_ratio(s, e, daily, monthly):
    """Bunches per ticket against the same month a year earlier: it moves with the crop
    inside a year (more fruit per round in the peak), so only a year-on-year move says the
    way tickets are written changed. One finding per estate."""
    if daily is None or "tickets" not in daily or monthly is None:
        return []
    full = set(monthly.loc[monthly["is_partial"] == 0, "month"].astype(str))
    d = daily.assign(month=pd.to_datetime(daily["harvest_date"]).dt.to_period("M").astype(str))
    g = d.groupby("month")[["bunches_total", "tickets"]].sum()
    g = g[g.index.isin(full) & (g["tickets"] > 0)]
    r = (g["bunches_total"] / g["tickets"]).to_dict()
    pairs = [(m, v, r[str(pd.Period(m, "M") - 12)]) for m, v in r.items() if str(pd.Period(m, "M") - 12) in r]
    if not pairs:
        return [_f(s, e, "ticket_ratio", "plausibility", "info",
                   "Bunches per ticket not judged yet", "No month has a year-earlier month to compare with.")]
    off = [(m, v, ly) for m, v, ly in pairs if abs(v / ly - 1) > T["ratio_warn"]]
    if not off:
        return [_f(s, e, "ticket_ratio", "plausibility", "ok",
                   f"Bunches per ticket in line with a year earlier ({len(pairs)} months compared)")]
    m, v, ly = max(off, key=lambda x: abs(x[1] / x[2] - 1))
    return [_f(s, e, "ticket_ratio", "plausibility", "warn",
               f"Bunches per ticket changed in {len(off)} of {len(pairs)} months against a year earlier",
               f"Largest {m}: {v:.0f} per ticket against {ly:.0f} in {pd.Period(m, 'M') - 12}.",
               "Either the crop per harvest round really changed, or tickets are now written differently "
               "(split per block, per harvester); worth asking the estate before reading the ticket counts "
               "as effort.", months=sorted(x[0] for x in off))]


def target_fill(s, e, meta):
    cols = (meta or {}).get("columns") or {}
    tgt = next((c for c in cols.values() if c.get("item") == "bunches_total" or c.get("written") == "bunches_total"), None)
    if not tgt or tgt.get("fill_rate") is None:
        return []
    fill = float(tgt["fill_rate"])
    if fill >= T["fill_warn"]:
        return [_f(s, e, "target_fill", "completeness", "ok", f"Bunch count filled on {fill:.0%} of tickets")]
    return [_f(s, e, "target_fill", "completeness", "alert" if fill < T["fill_alert"] else "warn",
               f"{1 - fill:.0%} of harvest tickets have no bunch count",
               f"Bunch count filled on {fill:.0%} of tickets.",
               "If those tickets carried bunches (rather than loose fruit only), every total is understated "
               "by up to that share.")]


def weight_per_trip(s, e, weight):
    if weight is None or weight.empty:
        return []
    out = []
    lo, hi = T["trip_tonnes"]
    w = weight[weight["trips"] > 0].copy()
    w["tpt"] = w["tonnes"] / w["trips"]
    bad = w[(w["tpt"] < lo) | (w["tpt"] > hi)]
    for _, r in bad.iterrows():
        out.append(_f(s, e, "weight_per_trip", "validity", "alert",
                      f"{r['month']}: {r['tpt']:.1f} tonnes per trip",
                      f"{_n(r['tonnes'])} t over {int(r['trips'])} trips; a truck carries {lo:.0f}-{hi:.0f} t.",
                      "Tonnes look summed per row instead of once per trip (the weighbridge repeats a trip's "
                      "weight on every row). Tonnes are not on the forecast yet; this decides whether they can be.",
                      months=[str(r["month"])]))
    thin = w[w["trips"] < T["trip_min_trips"]]
    if len(thin):
        months = thin["month"].astype(str).tolist()
        out.append(_f(s, e, "weight_per_trip", "completeness", "info",
                      f"Weighbridge data thin in {len(months)} month(s)",
                      "Fewer than " + str(T["trip_min_trips"]) + " trips in " + ", ".join(months[:4])
                      + ("…" if len(months) > 4 else "") + ".",
                      "Tonnes for those months are not usable.", months=months))
    if not out:
        out.append(_f(s, e, "weight_per_trip", "validity", "ok",
                      f"Tonnes per trip plausible ({w['tpt'].median():.1f} t median)"))
    return out


def check_estate(source: str, code: str, d: str, today: date | None = None,
                 handled: set | None = None) -> list:
    """Every per-estate check on one series folder. `handled`: months the model imputes."""
    today = today or date.today()
    handled = set(handled or ())
    e = code.lower()
    meta = _meta(d)
    if meta is None:
        return [_f(source, e, "extract_gate", "completeness", "alert", "No extract for this estate",
                   f"Nothing in {d}.", "No forecast can be built.")]
    daily = _read_csv(os.path.join(d, "daily.csv"))
    monthly = _read_csv(os.path.join(d, "monthly.csv"), dtype={"month": str})
    oph = _read_csv(os.path.join(d, "oph.csv"),
                    usecols=["harvest_date", "division_code", "block_code", "bunches_total"],
                    dtype={"division_code": str, "block_code": str})
    weight = _read_csv(os.path.join(d, "weight_monthly.csv"), dtype={"month": str})
    out = []
    out += freshness(source, e, meta, today)
    out += extract_gate(source, e, meta)
    out += history_depth(source, e, meta)
    out += [_handle(f, handled) for f in harvest_gaps(source, e, daily)]
    out += [_handle(f, handled) for f in short_months(source, e, monthly)]
    out += daily_spikes(source, e, daily)
    out += block_coverage(source, e, oph, monthly)
    out += duplicates(source, e, daily, oph)
    out += ticket_ratio(source, e, daily, monthly)
    out += target_fill(source, e, meta)
    out += weight_per_trip(source, e, weight)
    return out


# ── across sources ──────────────────────────────────────────────────────────

def cross_source(code: str, live_dir: str, staging_dir: str) -> list:
    """Monthly totals of one estate held in both sources, on the complete months both cover.
    Reported under both sources so each card shows it."""
    e = code.lower()
    lm = _read_csv(os.path.join(live_dir, "monthly.csv"), dtype={"month": str})
    sm = _read_csv(os.path.join(staging_dir, "monthly.csv"), dtype={"month": str})
    if lm is None or sm is None:
        return []
    a = lm[lm["is_partial"] == 0].set_index("month")["bunches_total"].astype(float)
    b = sm[sm["is_partial"] == 0].set_index("month")["bunches_total"].astype(float)
    common = a.index.intersection(b.index)
    if not len(common):
        f = _f("both", e, "cross_source", "consistency", "info",
               "No complete month held in both databases",
               f"Live {a.index.min() if len(a) else '-'} to {a.index.max() if len(a) else '-'}; "
               f"staging {b.index.min() if len(b) else '-'} to {b.index.max() if len(b) else '-'}.")
        return [dict(f, source=s, id=f"{s}/{e}/cross_source") for s in ("live", "staging")]
    pct = ((a[common] - b[common]) / b[common].where(b[common] != 0) * 100).round(1)
    off = pct[pct.abs() > T["xsrc_ok_pct"]]
    worst = pct.abs().idxmax()
    span = f"{common.min()} to {common.max()}"
    if off.empty:
        f = _f("both", e, "cross_source", "consistency", "ok",
               f"Live and staging agree on all {len(common)} shared months",
               f"{span}, within {T['xsrc_ok_pct']:.0f}%.")
    else:
        level = "alert" if (off.abs() > T["xsrc_warn_pct"]).any() else "warn"
        # the days behind the worst month
        ld = _read_csv(os.path.join(live_dir, "daily.csv"))
        sd = _read_csv(os.path.join(staging_dir, "daily.csv"))
        days = []
        if ld is not None and sd is not None:
            j = (ld.set_index("harvest_date")["bunches_total"].astype(float)
                 .to_frame("live").join(sd.set_index("harvest_date")["bunches_total"].astype(float)
                                        .rename("staging"), how="outer").fillna(0))
            j = j[j.index.str[:7] == worst]
            dd = j[(j["live"] - j["staging"]).abs() > j["staging"].abs() * T["xsrc_day_pct"] / 100]
            days = [f"{i}: live {_n(r['live'])}, staging {_n(r['staging'])}" for i, r in dd.head(3).iterrows()]
        f = _f("both", e, "cross_source", "consistency", level,
               f"{len(off)} of {len(common)} shared months differ between live and staging",
               f"Largest {worst}: live {pct[worst]:+.1f}% against staging"
               + (f" ({'; '.join(days)})." if days else "."),
               "A gap between the live and staging forecasts in those months comes from the data, "
               "not from the model.", months=sorted(off.index.tolist()))
    return [dict(f, source=s, id=f"{s}/{e}/cross_source") for s in ("live", "staging")]


# ── the report ──────────────────────────────────────────────────────────────

def run(today: date | None = None) -> dict:
    """Findings for every configured estate of every enabled source, in the served run."""
    today = today or date.today()
    findings = []
    dirs = {}
    for sid in ec.sources():
        for code in ec.estates(sid):
            d = ec.series_dir(sid, code)
            dirs[(sid, code)] = d
            findings += check_estate(sid, code, d, today, handled_months(sid, code))
    codes = {c for (_s, c) in dirs}
    for code in sorted(codes):
        if ("live", code) in dirs and ("staging", code) in dirs:
            findings += cross_source(code, dirs[("live", code)], dirs[("staging", code)])
    root = ec.run_root()                 # inside a refresh: the run being built
    return {"generated_at": datetime.now().isoformat(timespec="seconds"), "today": today.isoformat(),
            "run_id": os.path.basename(os.path.normpath(root)) if root else None,
            "thresholds": T, "findings": findings,
            "summary": summary(findings)}


def summary(findings: list) -> dict:
    """{"<source>/<estate>": {"level", "alert", "warn", "ok", "info"}} and totals."""
    per: dict = {}
    tot = {"alert": 0, "warn": 0, "ok": 0, "info": 0}
    for f in findings:
        k = f"{f['source']}/{f['estate']}"
        r = per.setdefault(k, {"level": "info", "alert": 0, "warn": 0, "ok": 0, "info": 0})
        r[f["level"]] += 1
        tot[f["level"]] += 1
        if LEVEL_RANK[f["level"]] > LEVEL_RANK[r["level"]]:
            r["level"] = f["level"]
    return {"estates": per, "totals": tot}


def worst(findings: list) -> str:
    lv = "info"
    for f in findings:
        if LEVEL_RANK[f["level"]] > LEVEL_RANK[lv]:
            lv = f["level"]
    return lv


def _main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="write the report as JSON here")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    args = ap.parse_args(argv)
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else None
    report = run(today)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for f in report["findings"]:
        if f["level"] in ("alert", "warn"):
            print(f"{f['level'].upper():5} {f['source']}/{f['estate']} {f['check']}: {f['title']}. {f['detail']}")
    t = report["summary"]["totals"]
    print(f"{t['alert']} alert, {t['warn']} warn, {t['ok']} ok, {t['info']} info")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
