"""
Every forecast at a glance, for the /model-health overview.

One card per forecast the pages serve (each estate of each source, plus the staging
group), each with a status and the checks behind it. Built only from what is
already cached or on disk -- the page payload (forecast_router.page_payload), the
served run's monitoring rows (health.json, forecast/monitor.py) and the data-quality
watchdog (forecast/watchdog.py) -- so it never refits, and never builds the slow
per-estate scoreboard; that stays on the detail view.

Statuses use the same thresholds as the detail view's KPI tiles, so a number reads
the same way on both: alert = Action, warn = Watch, ok = Healthy.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import statistics
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent
RANK = {"alert": 3, "warn": 2, "ok": 1, "info": 0, "na": 0}
STATUS = {"alert": "action", "warn": "watch", "ok": "healthy", "info": "healthy", "na": "healthy"}

# The health matrix's columns, in order. `group` is how the page heads them.
CHECKS = [
    {"id": "accuracy",  "label": "Accuracy",     "group": "Model", "rule": "3-month MASE: good <= 0.80, watch <= 1.00, above 1.00 the benchmark wins"},
    {"id": "error",     "label": "Error",        "group": "Model", "rule": "3-month sMAPE: good <= 10%, watch <= 20%"},
    {"id": "bias",      "label": "Bias",         "group": "Model", "rule": "mean signed error over steps 1-3: good <= 2%, watch <= 5% of the mean month"},
    {"id": "evidence",  "label": "Evidence",     "group": "Model", "rule": "backtest origins: good >= 12, otherwise watch (more data is the only fix)"},
    {"id": "track",     "label": "Track record", "group": "Model", "rule": "last 12 one-month-ahead forecasts within 10%: good >= 75%, watch >= 50%"},
    {"id": "live",      "label": "Live errors",  "group": "Live",  "rule": "resolved forecast months from the shadow log (forecast/monitor.py)"},
    {"id": "drift",     "label": "Drift",        "group": "Live",  "rule": "seasonal shape, declared columns and volume against the previous run"},
    {"id": "serving",   "label": "Serving",      "group": "Live",  "rule": "fallback, stale artifact or a manual pick that could not be served = action; a manual pick = watch"},
    {"id": "freshness", "label": "Freshness",    "group": "Data",  "rule": "days since the last harvest recorded: good <= 40, watch <= 90"},
    {"id": "data",      "label": "Data quality", "group": "Data",  "rule": "worst of the watchdog's checks on the series (gaps, spikes, blocks, duplicates, history)"},
    {"id": "sources",   "label": "Live vs staging", "group": "Data", "rule": "monthly totals of an estate held in both databases"},
]
_MONITOR = {"realised_error": "live", "chronos_vs_naive": "live", "seasonal_shape": "drift",
            "column_drift": "drift", "volume": "drift", "manual_pick": "serving"}

_wd_mod = None
_wd_lock = threading.Lock()
_wd_cache: dict = {"sig": None, "report": None}


# ── the watchdog, cached on the series files it reads ───────────────────────

_load_lock = threading.Lock()


def _watchdog():
    """Load forecast/watchdog.py once. Published only after it has fully run, under a
    lock: the start-up warm-up and the first page request load it at the same time."""
    global _wd_mod
    if _wd_mod is None:
        with _load_lock:
            if _wd_mod is None:
                spec = importlib.util.spec_from_file_location("epms_watchdog", str(_ROOT / "forecast" / "watchdog.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _wd_mod = mod
    return _wd_mod


def _signature(wd) -> tuple:
    """Changes when any series file of the served run changes, or the day turns."""
    ec = wd.ec
    parts = [datetime.now().date().isoformat(), str((ec.current_run() or {}).get("run_id"))]
    for sid in ec.sources():
        for code in ec.estates(sid):
            d = Path(ec.series_dir(sid, code))
            for name in ("meta.json", "daily.csv", "monthly.csv", "oph.csv", "weight_monthly.csv"):
                p = d / name
                parts.append(f"{p}:{p.stat().st_mtime_ns if p.exists() else 0}")
    return tuple(parts)


def watchdog_report(force: bool = False) -> dict:
    """The watchdog's report for the served run (about 1 s to build; cached until a
    series file changes or the date turns)."""
    wd = _watchdog()
    sig = _signature(wd)
    with _wd_lock:
        if force or _wd_cache["sig"] != sig or _wd_cache["report"] is None:
            _wd_cache["report"] = wd.run()
            _wd_cache["sig"] = sig
        return _wd_cache["report"]


def findings_for(report: dict, source: str, estates: list) -> list:
    return [f for f in report["findings"] if f["source"] == source and f["estate"] in estates]


# ── banding ─────────────────────────────────────────────────────────────────

def _band(v, good, warn, higher_is_better=False):
    if v is None:
        return "na"
    if higher_is_better:
        return "ok" if v >= good else "warn" if v >= warn else "alert"
    v = abs(v)
    return "ok" if v <= good else "warn" if v <= warn else "alert"


def _worst(levels) -> str:
    out = "na"
    for lv in levels:
        if RANK.get(lv, 0) > RANK[out] or (out == "na" and lv in ("ok", "info")):
            out = lv
    return out


def _cell(level, value="", note=""):
    return {"level": level, "value": value, "note": note}


def _health_rows() -> list:
    """The served run's monitoring rows (health.json), or [] before the first run with them."""
    import forecast_sources as fs
    root = fs.estate_config.run_root()
    if not root:
        return []
    try:
        return json.loads((Path(root) / "health.json").read_text(encoding="utf-8")).get("monitoring", [])
    except Exception:
        return []


# ── one card ────────────────────────────────────────────────────────────────

def _model_cells(p: dict) -> dict:
    acc = p.get("accuracy_multih") or {}
    mase, smape = p.get("headline_mase"), p.get("headline_smape")
    bias = acc.get("pooled_bias_pct")
    origins = acc.get("n_origins") or p.get("n_folds")
    tr = p.get("track_record") or {}
    flags = {f.get("kind") for f in p.get("flags") or []}
    # Few origins is a caution, not something to act on: it caps at "watch".
    ev = _band(origins, 12, 6, higher_is_better=True)
    if ev == "alert" or ("low_evidence" in flags and ev == "ok"):
        ev = "warn"
    share = (tr["within"] / tr["n"]) if tr.get("n") else None
    return {
        "accuracy": _cell(_band(mase, 0.8, 1.0), f"{mase:.2f}" if mase is not None else "N/A",
                          "3-month MASE against " + str(p.get("benchmark_label") or "the benchmark")),
        "error": _cell(_band(smape, 10, 20), f"{smape:.1f}%" if smape is not None else "N/A", "3-month sMAPE"),
        "bias": _cell(_band(bias, 2, 5), (f"{bias:+.1f}%" if bias is not None else "N/A"),
                      "+ means the forecast runs high"),
        "evidence": _cell(ev, f"{origins} origins" if origins else "N/A",
                          "low evidence flagged by the selection" if "low_evidence" in flags else ""),
        "track": _cell(_band(share, 0.75, 0.5, higher_is_better=True),
                       f"{tr['within']}/{tr['n']}" if tr.get("n") else "N/A",
                       f"one-month-ahead within {tr.get('tol_pct', 10)}%" if tr.get("n") else ""),
    }


def _serving_cell(p: dict) -> dict:
    sv = p.get("serving") or {}
    if p.get("fallback"):
        return _cell("alert", "fallback", "served by the ensemble: " + str((p["fallback"] or {}).get("reason", "")))
    if p.get("stale"):
        return _cell("alert", "stale", p.get("stale_note") or "")
    if sv.get("errors"):
        return _cell("alert", "pick failed", "; ".join(sv["errors"]))
    mode = sv.get("mode") or "rule"
    if mode == "override":
        return _cell("warn", "manual pick", "picked on Model health, not by the rule")
    return _cell("ok", "promotion" if mode == "promotion" else "rule", "chosen by the " + mode)


def _monitor_cells(rows: list) -> dict:
    out = {}
    for cid in ("live", "drift"):
        mine = [r for r in rows if _MONITOR.get(r["check"]) == cid]
        lv = _worst([r["level"] for r in mine])
        bad = [r for r in mine if r["level"] == "alert"]
        note = "; ".join(f"{r['check'].replace('_', ' ')}: {r['detail']}" for r in (bad or mine)[:2])
        val = {"alert": f"{len(bad)} alert{'s' if len(bad) != 1 else ''}", "ok": "ok"}.get(lv, "not yet")
        out[cid] = _cell(lv if lv != "info" else "na", val, note or "no rows in this run")
    return out


def _data_cells(findings: list) -> dict:
    fresh = [f for f in findings if f["check"] == "freshness"]
    xsrc = [f for f in findings if f["check"] == "cross_source"]
    rest = [f for f in findings if f["check"] not in ("freshness", "cross_source")]

    def one(fs, empty_value="n/a"):
        if not fs:
            return _cell("na", empty_value, "")
        lv = _worst([f["level"] for f in fs])
        top = sorted(fs, key=lambda f: -RANK[f["level"]])[0]
        n_bad = sum(f["level"] in ("alert", "warn") for f in fs)
        return {"level": lv if lv != "info" else "ok", "top": top["title"], "issues": n_bad}

    fr = one(fresh)
    if fresh:
        fr["value"] = fresh[0]["title"].replace("Data is ", "").replace("current (", "").rstrip(")")
        fr["note"] = fresh[0]["detail"]
    d = one(rest)
    if rest:
        d["value"] = f"{d['issues']} issue{'s' if d['issues'] != 1 else ''}" if d["issues"] else "clean"
        d["note"] = d["top"]
    x = one(xsrc, "single source")
    if xsrc:
        x["value"] = "agree" if x["level"] == "ok" else "differ" if x["level"] in ("warn", "alert") else "n/a"
        x["note"] = xsrc[0]["title"]
    else:
        x["note"] = "this estate is held in one database only"
    return {"freshness": fr, "data": d, "sources": x}


def _issues(cells: dict, findings: list, rows: list, group: bool = False) -> list:
    """Up to three plain lines, worst first: what someone should look at. A group's
    lines name the estate, since several members can report the same thing."""
    lines = []
    who = (lambda e: e.upper() + ": ") if group else (lambda e: "")
    for f in sorted(findings, key=lambda f: -RANK[f["level"]]):
        if f["level"] in ("alert", "warn"):
            lines.append({"level": f["level"], "text": who(f["estate"]) + f["title"]})
    for r in rows:
        if r["level"] == "alert" and r["check"] != "freshness":   # the watchdog reports freshness
            lines.append({"level": "alert", "text": who(r["estate"]) + r["check"].replace("_", " ").capitalize()
                          + ": " + r["detail"]})
    for cid in ("accuracy", "error", "serving"):
        c = cells.get(cid) or {}
        if c.get("level") == "alert":
            label = next(x["label"] for x in CHECKS if x["id"] == cid)
            lines.append({"level": "alert", "text": f"{label}: {c.get('value')}" + (f" ({c['note']})" if c.get("note") else "")})
    seen, out = set(), []
    for ln in sorted(lines, key=lambda l: -RANK[l["level"]]):
        if ln["text"] not in seen:
            seen.add(ln["text"])
            out.append(ln)
    return out[:3]


def card(source: str, estate: str, label: str, members: list, report: dict, rows: list) -> dict:
    """One forecast's card. `members`: the page estate ids it covers (one, or the group's)."""
    import forecast_router as fr
    import forecast_sources as fs
    findings = findings_for(report, source, members)
    mrows = [r for r in rows if r["source"] == source and r["estate"] in members]
    base = {"source": source, "estate": estate, "label": label, "members": members,
            "group": estate == fs.GROUP,
            "href": f"/model-health?db={source}&estate={estate}",
            "watchdog": {"alert": sum(f["level"] == "alert" for f in findings),
                         "warn": sum(f["level"] == "warn" for f in findings)}}
    cells = {**_data_cells(findings)}
    try:
        p = fr.page_payload(estate, horizon=3, source=source)
    except Exception as exc:                      # fs.Unavailable, or a broken artifact
        reason = str(exc) or type(exc).__name__
        for c in CHECKS:
            cells.setdefault(c["id"], _cell("na", "", ""))
        cells["serving"] = _cell("alert", "not forecast", reason)
        return {**base, "status": "not_forecast", "reason": reason, "cells": cells,
                "issues": _issues(cells, findings, mrows, estate == fs.GROUP), "metrics": {}, "spark": None}
    cells.update(_model_cells(p))
    cells["serving"] = _serving_cell(p)
    cells.update(_monitor_cells(mrows))
    for c in CHECKS:
        cells.setdefault(c["id"], _cell("na", "", ""))
    worst = _worst(c["level"] for c in cells.values())
    acc = p.get("accuracy_multih") or {}
    tr = p.get("track_record") or {}
    try:
        spark = fs.tile(p).get("spark")
    except Exception:
        spark = None
    sv = p.get("serving") or {}
    return {
        **base,
        "status": STATUS[worst],
        "cells": cells,
        "issues": _issues(cells, findings, mrows, estate == fs.GROUP),
        "model": {"label": p.get("model_label") or p.get("model_name"), "id": p.get("model_id"),
                  "mode": "fallback" if p.get("fallback") else (sv.get("mode") or "rule")},
        "anchor": (p.get("anchor") or {}).get("month"),
        "data_through": p.get("data_through"),
        "generated_at": p.get("generated_at"),
        "metrics": {"mase": p.get("headline_mase"), "smape": p.get("headline_smape"),
                    "bias_pct": acc.get("pooled_bias_pct"), "origins": acc.get("n_origins"),
                    "per_horizon": acc.get("per_horizon") or [],
                    "benchmark": p.get("benchmark_label"),
                    "track": {"n": tr.get("n"), "within": tr.get("within"), "hits": tr.get("hits") or [],
                              "months": tr.get("months") or [], "tol_pct": tr.get("tol_pct")},
                    "total_3m": p.get("total_3m")},
        "spark": spark,
    }


# ── the fleet ───────────────────────────────────────────────────────────────

def forecasts() -> list:
    """(source, estate, label, members) for every forecast the pages serve, in page
    order: each source's estates, the staging group first among the staging ones."""
    import forecast_sources as fs
    out = []
    for source in fs.SOURCES:
        ids = list(fs.estate_config.estates(source))
        pids = [c.lower() for c in ids]
        if source == fs.STAGING and len(pids) > 1:
            out.append((source, fs.GROUP, " + ".join(c for c in ids), pids))
        out += [(source, pid, code, [pid]) for code, pid in zip(ids, pids)]
    return out


def fleet() -> dict:
    report = watchdog_report()
    rows = _health_rows()
    cards = [card(s, e, label, members, report, rows) for s, e, label, members in forecasts()]
    counts = {k: sum(c["status"] == k for c in cards) for k in ("action", "watch", "healthy", "not_forecast")}
    mases = [c["metrics"]["mase"] for c in cards if not c["group"] and c["metrics"].get("mase") is not None]
    ages = [f for f in report["findings"] if f["check"] == "freshness" and f["level"] != "info"]
    oldest = max(ages, key=lambda f: int(''.join(ch for ch in f["title"] if ch.isdigit()) or 0), default=None)
    import forecast_sources as fs
    run = fs.estate_config.current_run() or {}
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run": {"id": run.get("run_id"), "published_at": run.get("published_at")},
        "checks": CHECKS,
        "cards": cards,
        "summary": {
            "forecasts": len(cards), "estates": sum(not c["group"] for c in cards),
            "counts": counts,
            "median_mase": round(statistics.median(mases), 2) if mases else None,
            "beating_benchmark": sum(m < 1 for m in mases), "scored": len(mases),
            "oldest_data": ({"source": oldest["source"], "estate": oldest["estate"], "title": oldest["title"],
                             "detail": oldest["detail"]} if oldest else None),
            "watchdog": report["summary"]["totals"],
        },
        "watchdog": {"generated_at": report["generated_at"], "run_id": report.get("run_id"),
                     "findings": [f for f in report["findings"] if f["level"] in ("alert", "warn", "info")]},
    }
