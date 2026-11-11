"""
Forecast pages: the staging source.

The forecast page can be pointed at two sources, the same two the chat page
offers:

  live     the estate files the page has always read (K3, EC) and the
           SARIMAX + Trailing-3 + LightGBM ensemble fitted on them.
           forecast_router.py serves this one.
  staging  EA, EB and EC cut from the EPMS staging database by
           forecast/build_staging_series.py. This module serves it.

Neither source touches a database in a request: both read files built offline.

What staging serves
-------------------
17-22 months of history is too short for the live ensemble, so staging serves
one of the Forecast Lab's zero-shot arms: the one forecast/lab/select_staging.py
picked from the walk-forward backtests by its dated rule. Everything here is
read from the lab's artifacts (forecast/experiments/lab/) through the registry;
nothing is fitted in the request.

Payload
-------
payload() returns the SAME shape forecast_router._compute returns, so the page
draws either source with one render path. Differences are data, not structure:
no per-member columns (`components` is empty), no SHAP drivers, the benchmark is
Naive last-3 mean (a seasonal naive needs a year before each target, which
staging does not have yet) and the horizon stops at 6 months.

The band is the same kind the live page draws: +/- the sMAPE measured at each
step on the walk-forward backtest of the served model. It is a typical-error
band, not a prediction interval, and with 5-16 scored rows per step it is a
rough one; every step carries its row count.

The group view ("all") is a true sum: EA + EB + EC over the months all three
have, with the backtest summed row by row so the group's error is measured on
the group's own series.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock

log = logging.getLogger("epms-forecast")

_ROOT = Path(__file__).parent
_STAGING_DIR = _ROOT / "forecast" / "staging"

LIVE, STAGING = "live", "staging"
SOURCES = (LIVE, STAGING)

# page estate id -> Forecast Lab registry id
ESTATES = {"ea": "stg_ea", "eb": "stg_eb", "ec": "stg_ec"}
GROUP = "all"
HORIZON_CHOICES = (3, 6)
HORIZON_DEFAULT = 3
HEADLINE_STEPS = 3
BENCHMARK = "naive_trailing3"
BENCHMARK_LABEL = "naive last-3 mean"
GRAIN = "month"

BUILD_STEPS = ("python forecast/build_staging_series.py, then forecast/lab/build_artifacts.py "
               "--source staging (Chronos env), then python forecast/lab/select_staging.py")

_LOCK = Lock()
_CACHE: dict = {}          # (estate, horizon) -> (signature, payload)
_SEL_MOD = None


class Unavailable(Exception):
    """Staging cannot serve this request; the message says what is missing."""


def _lab():
    """(registry, live-model function, selection module), shared with the Forecast Lab."""
    global _SEL_MOD
    import experiment_router as er
    if _SEL_MOD is None:
        _SEL_MOD = er._load_lab("select_staging")
    return er.reg, er._live, _SEL_MOD


def _signature(reg, sel_mod) -> tuple:
    """Changes whenever the staging series, the artifacts or the selection are rebuilt."""
    paths = [Path(sel_mod.selection_path(reg)), Path(reg.ARTIFACT_DIR) / "manifest.json"]
    paths += [_STAGING_DIR / code.upper() / "oph.csv" for code in ESTATES]
    return tuple(p.stat().st_mtime if p.exists() else None for p in paths)


def _meta(page_id: str) -> dict:
    path = _STAGING_DIR / page_id.upper() / "meta.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _harvest_days(page_id: str) -> dict:
    """month -> recorded harvest days, from the extract's monthly file."""
    import csv
    path = _STAGING_DIR / page_id.upper() / "monthly.csv"
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return {r["month"]: int(r["n_harvest_days"]) for r in csv.DictReader(fh)}
    except Exception:
        return {}


# ── one estate's series, forecast and backtests ─────────────────────────────

def _scored(reg, model: str, rid: str):
    """Scoreable walk-forward rows of a model, indexed by (origin, step)."""
    df, _why = reg.backtest(model, rid, GRAIN)
    if df is None or df.empty:
        return None
    df = df[df["scoreable"]]
    return df.set_index(["origin", "step"])[["actual", "pred"]].astype(float)


def _bundle(reg, live_fn, model: str, page_id: str) -> dict:
    rid = ESTATES[page_id]
    label = reg.ESTATES[rid]["label"]
    try:
        h = reg.month_history(rid)
    except FileNotFoundError:
        raise Unavailable(f"No staging series for {label} yet. Build it: {BUILD_STEPS}.")
    ok, why = reg.static_availability(model, rid, GRAIN)
    if not ok:
        raise Unavailable(f"{label}: {why}.")
    spec = reg.model_spec(model)
    fwd = live_fn(model, rid, GRAIN) if spec["source"] == "live" else reg.read_artifact(model, rid, GRAIN)
    if fwd is None:
        raise Unavailable(f"The staging forecast for {label} has not been built. Build it: {BUILD_STEPS}.")

    periods = [str(p) for p in h["period"]]
    anchor = fwd["anchor"]["period"]
    if anchor not in periods:
        raise Unavailable(f"The staging forecast for {label} was built on data through {anchor}, "
                          f"which the series no longer covers. Rebuild it: {BUILD_STEPS}.")
    cut = periods.index(anchor) + 1          # history stops where the forecast starts
    days = _harvest_days(page_id)
    history = [{"month": periods[i], "value": round(float(h["value"].iloc[i])),
                "underrecorded": bool(h["underrecorded"].iloc[i]),
                "n_harvest_days": days.get(periods[i])} for i in range(cut)]
    bt = _scored(reg, model, rid)
    bn = _scored(reg, BENCHMARK, rid)
    if bt is None or bn is None:
        raise Unavailable(f"No walk-forward backtest for {label} on staging. Build it: {BUILD_STEPS}.")
    return {
        "id": page_id, "label": label, "history": history,
        "steps": [(s["period"], float(s["value"])) for s in fwd["steps"]],
        "bt": bt, "bn": bn,
        "stale_note": (f"forecast built on data through {anchor}; the staging series now runs to "
                       f"{periods[-1]}") if cut < len(periods) else "",
        "generated_at": fwd.get("generated_at"),
        "last_day": _meta(page_id).get("last_day"),
    }


def _sum_bundles(parts: list) -> dict:
    """EA + EB + EC as one series: months, forecast steps and backtest rows all three share."""
    anchors = {p["history"][-1]["month"] for p in parts}
    if len(anchors) != 1:
        raise Unavailable("The staging estates end on different months ("
                          + ", ".join(f"{p['label']} {p['history'][-1]['month']}" for p in parts)
                          + "), so a group total cannot be drawn. Rebuild all three together.")
    by_month = [{h["month"]: h for h in p["history"]} for p in parts]
    months = [m for m in by_month[0] if all(m in b for b in by_month)]
    history = [{"month": m, "value": sum(b[m]["value"] for b in by_month),
                "underrecorded": any(b[m]["underrecorded"] for b in by_month),
                "n_harvest_days": None} for m in months]
    steps = []
    for rows in zip(*(p["steps"] for p in parts)):
        if len({r[0] for r in rows}) != 1:
            raise Unavailable("The staging forecasts do not cover the same months; rebuild all three together.")
        steps.append((rows[0][0], sum(r[1] for r in rows)))

    def _sum(key):
        idx = parts[0][key].index
        for p in parts[1:]:
            idx = idx.intersection(p[key].index)
        out = parts[0][key].loc[idx].copy()
        for p in parts[1:]:
            out = out + p[key].loc[idx]
        return out

    days = [p["last_day"] for p in parts if p["last_day"]]
    gens = [p["generated_at"] for p in parts if p["generated_at"]]
    return {"id": GROUP, "label": " + ".join(p["label"] for p in parts), "history": history,
            "steps": steps, "bt": _sum("bt"), "bn": _sum("bn"),
            "stale_note": next((p["stale_note"] for p in parts if p["stale_note"]), ""),
            "generated_at": min(gens) if gens else None,
            "last_day": min(days) if days else None}


# ── accuracy, measured on the walk-forward rows ─────────────────────────────

def _smape(df) -> float:
    d = df["actual"].abs() + df["pred"].abs()
    return float((2 * (df["pred"] - df["actual"]).abs() / d.where(d != 0, 1)).mean() * 100)


def _mae(df) -> float:
    return float((df["pred"] - df["actual"]).abs().mean())


def _at_step(df, step: int):
    return df[df.index.get_level_values("step") == step]


def _upto(df, step: int):
    return df[df.index.get_level_values("step") <= step]


def _accuracy(bt, bn, max_step: int) -> dict:
    """Pooled and per-step error of the served model against the benchmark, on
    the (origin, step) rows both have -- so the ratio compares like with like."""
    common = bt.index.intersection(bn.index)
    b, n = _upto(bt.loc[common], max_step), _upto(bn.loc[common], max_step)
    if b.empty:
        return {}
    per_h = []
    for s in range(1, max_step + 1):
        bs, ns = _at_step(b, s), _at_step(n, s)
        if len(bs):
            den = _mae(ns)
            per_h.append({"step": s, "smape": round(_smape(bs), 1),
                          "mase": round(_mae(bs) / den, 2) if den else None, "n": int(len(bs))})
    den = _mae(n)
    return {
        "pooled_smape": round(_smape(b), 1),
        "pooled_mase": round(_mae(b) / den, 2) if den else None,
        "per_horizon": per_h, "n_rows": int(len(b)),
        "n_origins": int(b.index.get_level_values("origin").nunique()),
        "max_step": max_step,
        "source": "Forecast Lab walk-forward backtest on the staging series",
        "benchmark": "Naive last-3 mean on the same walk-forward rows (MASE denominator)",
    }


def _step_bands(bt, horizon: int) -> dict:
    """step -> {"band_pct", "n", "coverage"}: the sMAPE measured at that step,
    and how often the actual fell inside pred +/- that share of pred."""
    out, widest = {}, None
    for s in range(1, horizon + 1):
        rows = _at_step(bt, s)
        if len(rows):
            band = round(_smape(rows), 1)
            inside = ((rows["actual"] - rows["pred"]).abs() <= rows["pred"].abs() * band / 100.0)
            out[s] = {"band_pct": band, "n": int(len(rows)), "coverage": round(float(inside.mean()), 3)}
            widest = band if widest is None else max(widest, band)
    for s in range(1, horizon + 1):          # a step nothing was scored at takes the widest measured
        out.setdefault(s, {"band_pct": widest, "n": 0, "coverage": None})
    return out


def tile(payload: dict) -> dict:
    """What an estate tile shows, from that estate's own payload (either source)."""
    fc = payload.get("forecast") or []
    if not fc:
        return {}
    hist = {h["month"]: h["value"] for h in payload.get("history") or []}
    total = sum(f["ensemble"] for f in fc)
    last_year = [hist.get(f"{int(f['month'][:4]) - 1}{f['month'][4:]}") for f in fc]
    ly = sum(last_year) if all(v is not None for v in last_year) else None
    return {"total": total, "yoy_total": ly,
            "yoy_pct": round((total - ly) / ly * 100, 1) if ly else None,
            "band_pct": payload.get("avg_band_pct"),
            "first_month": fc[0]["month"], "last_month": fc[-1]["month"]}


# ── payloads ────────────────────────────────────────────────────────────────

def _selection():
    reg, live_fn, sel_mod = _lab()
    sel = sel_mod.read(reg)
    if not sel or not sel.get("selected"):
        raise Unavailable(f"No model has been selected for the staging source yet. Build it: {BUILD_STEPS}.")
    return reg, live_fn, sel


def _series(estate: str) -> tuple:
    """(bundle, registry, selection) for one estate or the group."""
    reg, live_fn, sel = _selection()
    if estate == GROUP:
        bundle = _sum_bundles([_bundle(reg, live_fn, sel["selected"], e) for e in ESTATES])
    elif estate in ESTATES:
        bundle = _bundle(reg, live_fn, sel["selected"], estate)
    else:
        raise Unavailable(f"'{estate}' is not an estate in the staging source.")
    return bundle, reg, sel


def _base(estate: str, horizon: int) -> dict:
    """The page payload for one staging scope, without the estate tiles."""
    bundle, reg, sel = _series(estate)
    spec = reg.model_spec(sel["selected"])
    bands = _step_bands(bundle["bt"], horizon)
    history = bundle["history"]
    anchor = {"month": history[-1]["month"], "value": history[-1]["value"]}

    forecast = []
    for i, (month, value) in enumerate(bundle["steps"][:horizon]):
        step = i + 1
        band = bands[step]["band_pct"] or 0.0
        forecast.append({"month": month, "step": step, "ensemble": round(value),
                         "lower": round(value * (1 - band / 100)),
                         "upper": round(value * (1 + band / 100)), "band_pct": band})

    acc = _accuracy(bundle["bt"], bundle["bn"], HEADLINE_STEPS)
    step1 = next((h for h in acc.get("per_horizon", []) if h["step"] == 1), {})
    avg_band = round(sum(f["band_pct"] for f in forecast) / len(forecast), 1) if forecast else None
    return {
        "source": STAGING,
        "history": history, "anchor": anchor, "forecast": forecast,
        "interval": {
            "method": "measured_smape",
            "conformal_nominal_pct": None, "conformal_coverage_h1_3": None,
            "per_step": {str(s): {"n_scored": b["n"], "band_pct": b["band_pct"],
                                  "coverage": b["coverage"], "coverage_n": b["n"]}
                         for s, b in bands.items()},
        },
        "drivers": {"note": "", "months": []},
        "components": [],
        "benchmark_label": BENCHMARK_LABEL,
        "horizon_choices": list(HORIZON_CHOICES),
        "capabilities": {"intelligence": False, "investigate": False},
        "smape": step1.get("smape"), "smape_1step": step1.get("smape"),
        "headline_smape": acc.get("pooled_smape"), "headline_mase": acc.get("pooled_mase"),
        "accuracy_multih": acc,
        "mase": step1.get("mase"), "n_folds": acc.get("n_origins"),
        "model_id": spec["id"], "model_name": spec["label"],
        "model_label": spec["label"] + " (zero-shot)",
        "trained_through": anchor["month"], "data_through": bundle["last_day"],
        "stale_note": bundle["stale_note"],
        "selection": {"rule": sel.get("rule"), "reason": sel.get("reason"),
                      "decided_at": sel.get("decided_at")},
        "horizon": horizon,
        "total_forecast": sum(f["ensemble"] for f in forecast),
        "total_3m": sum(f["ensemble"] for f in forecast[:3]),
        "avg_band_pct": avg_band,
        "last_band_pct": forecast[-1]["band_pct"] if forecast else None,
        "band_pct": step1.get("smape"),
        "series_label": bundle["label"],
        "generated_at": bundle["generated_at"] or datetime.now().isoformat(timespec="seconds"),
    }


def _cached(estate: str, horizon: int) -> dict:
    reg, _live, sel_mod = _lab()
    sig = _signature(reg, sel_mod)
    with _LOCK:
        if any(v[0] != sig for v in _CACHE.values()):
            _CACHE.clear()
            reg.clear_cache()          # the series files changed under the registry's cache
        hit = _CACHE.get((estate, horizon))
        if hit is None:
            hit = (sig, _base(estate, horizon))
            _CACHE[(estate, horizon)] = hit
        return hit[1]


def _scope(estate: str, horizon: int) -> dict:
    reg = _lab()[0]
    estates = []
    for pid, rid in ESTATES.items():
        row = {"id": pid, "label": reg.ESTATES[rid]["label"], "modeled": True, "has_blocks": False}
        try:
            row["tile"] = tile(_cached(pid, horizon))
        except Unavailable as exc:
            row.update({"modeled": False, "reason": str(exc)})
        estates.append(row)
    modeled = [e for e in estates if e["modeled"]]
    single = next((e for e in estates if e["id"] == estate), None)
    return {
        "levels": ["group", "estate", "division", "block"],
        "available_levels": ["group", "estate"],
        "estates": estates, "modeled_count": len(modeled), "estate_count": len(estates),
        "series_estate": estate if single else None,
        "series_label": single["label"] if single else " + ".join(e["label"] for e in modeled),
        "group_is_sum": True,
        "complete": len(modeled) == len(estates),
    }


def payload(estate: str = GROUP, horizon: int = HORIZON_DEFAULT, refresh: bool = False) -> dict:
    """Forecast page payload for a staging estate id (ea/eb/ec) or the group ("all")."""
    horizon = horizon if horizon in HORIZON_CHOICES else HORIZON_DEFAULT
    if refresh:
        with _LOCK:
            _CACHE.clear()
        _lab()[0].clear_cache()
    out = dict(_cached(estate, horizon))
    out["scope"] = _scope(estate, horizon)
    return out


def modeled_estates() -> list:
    """Staging estate ids that can be served right now."""
    out = []
    for pid in ESTATES:
        try:
            _cached(pid, HORIZON_DEFAULT)
            out.append(pid)
        except Unavailable:
            pass
        except Exception as exc:               # a broken artifact is "not available", not a 500
            log.warning("[forecast] staging estate %s unavailable: %s", pid, exc)
    return out


def sources() -> list:
    """The two sources, for the page's Database control (labels as on the chat page)."""
    labels = {LIVE: ("Live estates", "K3 and EC, from the estate files"),
              STAGING: ("EPMS staging", "EA, EB and EC, from the staging database")}
    try:
        import data_sources as ds
        for sid in SOURCES:
            # the chat's name for the database, without its list of estates:
            # the forecast covers a different set (said in the description)
            labels[sid] = (ds.get(sid).label.split(" (")[0], labels[sid][1])
    except Exception:
        pass
    out = [{"id": LIVE, "label": labels[LIVE][0], "description": labels[LIVE][1],
            "available": True, "data_through": None}]
    row = {"id": STAGING, "label": labels[STAGING][0], "description": labels[STAGING][1],
           "available": False, "data_through": None}
    try:
        ready = modeled_estates()
        if ready:
            days = [d for d in (_meta(p).get("last_day") for p in ready) if d]
            row.update({"available": True, "data_through": min(days) if days else None})
        else:
            _selection()                       # raises with the precise reason
            row["reason"] = f"Not built yet. {BUILD_STEPS}."
    except Unavailable as exc:
        row["reason"] = str(exc)
    except Exception as exc:
        log.warning("[forecast] staging source check failed: %s", exc)
        row["reason"] = "The staging forecast files could not be read."
    out.append(row)
    return out


# ── model health ────────────────────────────────────────────────────────────

def _board(reg, estate: str) -> list:
    """Step-1 scoreboard of every lab model with a monthly backtest on this scope,
    all scored on the rows they share."""
    ids = list(ESTATES) if estate == GROUP else [estate]
    frames = {}
    for m in reg.MODELS:
        parts = [_scored(reg, m["id"], ESTATES[e]) for e in ids]
        if any(p is None for p in parts):
            continue
        idx = parts[0].index
        for p in parts[1:]:
            idx = idx.intersection(p.index)
        total = parts[0].loc[idx].copy()
        for p in parts[1:]:
            total = total + p.loc[idx]
        frames[m["label"]] = _at_step(total, 1)
    bench_label = reg.model_spec(BENCHMARK)["label"]
    if bench_label not in frames:
        return []
    common = None
    for df in frames.values():
        common = df.index if common is None else common.intersection(df.index)
    bench = frames[bench_label].loc[common]
    den = _mae(bench)
    rows = []
    for label, df in frames.items():
        d = df.loc[common]
        err = d["pred"] - d["actual"]
        rows.append({"model": label, "mae": round(float(err.abs().mean())),
                     "rmse": round(float((err ** 2).mean() ** 0.5)),
                     "bias": round(float(err.mean())), "smape": round(_smape(d), 1),
                     "mase": round(_mae(d) / den, 3) if den else None})
    return sorted(rows, key=lambda r: (r["mase"] is None, r["mase"]))


def health(estate: str = GROUP) -> dict:
    """Model-health payload for the staging source, in /forecast/model-health's shape."""
    reg, _live, sel_mod = _lab()
    fc = payload(estate, max(HORIZON_CHOICES))
    bundle, _reg, sel = _series(estate)
    acc_mh = fc.get("accuracy_multih") or {}
    history = fc["history"]
    mean_actual = sum(h["value"] for h in history) / len(history) if history else None
    board = _board(reg, estate)
    served = next((r for r in board if r["model"] == fc["model_name"]), None)

    per_step = [{"step": int(s), "smape_pct": b["band_pct"], "n_scored": b["n_scored"],
                 "split_coverage": None, "aci_coverage": None,
                 "served_width": None, "served_width_pct": None,
                 "chart_band_pct": b["band_pct"],
                 "chart_width_pct": round(2 * b["band_pct"], 1) if b["band_pct"] is not None else None,
                 "chart_coverage": b["coverage"], "chart_coverage_n": b["coverage_n"]}
                for s, b in sorted(fc["interval"]["per_step"].items(), key=lambda kv: int(kv[0]))]

    under = [h for h in history if h["underrecorded"]]
    last = fc["trained_through"]
    now = datetime.now()
    fresh = (now.year - int(last[:4])) * 12 + (now.month - int(last[5:7])) if last else None

    def _src(name, path):
        p = Path(path)
        return {"name": name, "present": p.exists(),
                "modified": (datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
                             if p.exists() else None)}

    ids = list(ESTATES) if estate == GROUP else [estate]
    files = [_src(f"{reg.ESTATES[ESTATES[e]]['label']} harvest series (staging)",
                  _STAGING_DIR / e.upper() / "oph.csv") for e in ids]
    files += [_src("Forecast Lab artifacts", Path(reg.ARTIFACT_DIR) / "manifest.json"),
              _src("Model selection", sel_mod.selection_path(reg))]
    gates = [(_meta(e).get("gate") or {}) for e in ids]
    return {
        "source": STAGING,
        "scope": fc["scope"],
        "series_label": fc["series_label"],
        "benchmark_label": BENCHMARK_LABEL,
        "lifecycle": {
            "served_model": fc["model_name"], "served_ensemble": fc["model_name"],
            "served_label": fc["model_label"], "trained_through": last,
            "artifact_built": bundle["generated_at"], "version": sel.get("rule"),
            "next_retrain": None,
            "registry_note": ("Zero-shot model: nothing is trained. It was chosen by rule "
                              f"{sel.get('rule')} ({sel.get('reason')}). After each staging refresh "
                              f"rebuild and re-select: {BUILD_STEPS}."),
        },
        "accuracy": {
            "mase": served["mase"] if served else fc.get("mase"),
            "mase_h1_3": acc_mh.get("pooled_mase"),
            "smape": served["smape"] if served else fc.get("smape"),
            "smape_1step": served["smape"] if served else fc.get("smape"),
            "smape_h1_3": acc_mh.get("pooled_smape"),
            "per_horizon": acc_mh.get("per_horizon") or [],
            "multih_n": acc_mh.get("n_rows"), "multih_origins": acc_mh.get("n_origins"),
            "mae": served["mae"] if served else None, "rmse": served["rmse"] if served else None,
            "bias": served["bias"] if served else None,
            "bias_pct": (round(served["bias"] / mean_actual * 100, 1)
                         if served and mean_actual else None),
            "n_folds": acc_mh.get("n_origins"),
            "benchmark": "Naive last-3 mean (MASE denominator)",
        },
        "scoreboard": board,
        "interval": {"served_method": None, "nominal_pct": None, "coverage_h1_3": None,
                     "coverage_ok": None, "chart_method": "measured_smape", "per_step": per_step},
        "data_quality": {
            "months_history": len(history),
            "first_month": history[0]["month"] if history else None,
            "last_complete_month": last, "underrecorded_months": len(under),
            "underrecorded_pct": round(len(under) / len(history) * 100, 1) if history else None,
            "freshness_months": fresh, "weather": None, "sources": files,
            "extract_gate_ok": all(g.get("ok") for g in gates) if gates else None,
        },
        "drift": {"available": False,
                  "reason": "No reference-vs-live distribution monitor is wired up for the staging series.",
                  "features": ["Production (target)", "Harvest days"]},
        "research": {"available": False,
                     "reason": "The research arms were benchmarked on K3 only. For the staging "
                               "estates, every model is compared in the Forecast Lab."},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
