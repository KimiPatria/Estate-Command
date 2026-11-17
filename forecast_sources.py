"""
Forecast pages: the Chronos-2 forecast on both sources.

The forecast page can be pointed at two sources, the same two the chat page
offers:

  live     the per-estate EPMS databases (snapshots of EC, EB and EA from
           gis/build_estate_data.py) and K3's export.
  staging  EA, EB and EC cut from the EPMS staging database by
           forecast/build_staging_series.py.

Both serve Chronos-2 zero-shot from the Forecast Lab's artifacts (user decision
2026-11-12, buildplan_forecast_mlops.md D1). The arm per source is picked by
forecast/lab/select_source.py; K3 is a recorded manual promotion
(forecast/lab/promotion.py MANUAL_PROMOTIONS). forecast_router.py keeps the live
page's roster and block-map scope, falls back to the SARIMAX + Trailing-3 +
LightGBM ensemble for K3 when its Chronos forecast is missing or stale, and asks
this module for every other live forecast.

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

Bands (build plan D2). The drawn band is +/- the sMAPE measured at each step on
the served model's walk-forward backtest -- a typical-error band, not a
prediction interval. It is measured on the regime that applies now: origins with
at least the active rule's context (12 months under chronos-ctx12-*), falling
back to every origin when fewer than MIN_BAND_ORIGINS qualify. Its measured
coverage is printed beside it. A lighter outer band is the 80% range: Chronos's
own 10th-90th percentile when that covered 70-90% of the backtest actuals on
>= 10 rows, otherwise the 80th percentile of |actual - forecast| / forecast at
each step. The headline accuracy is scored on the same rows as the band.

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

from forecast import estate_config   # noqa: E402  (forecast/ is a namespace package)
from forecast import overrides       # noqa: E402  manual picks from Model health

# page estate id -> Forecast Lab registry id, per source (forecast/estates.yaml)
SOURCE_ESTATES = {sid: estate_config.page_estates(sid) for sid in (LIVE, STAGING)}
ESTATES = SOURCE_ESTATES[STAGING]          # the staging table (kept for older callers)
GROUP = "all"
HORIZON_CHOICES = (3, 6)
HORIZON_DEFAULT = 3
HEADLINE_STEPS = 3
BENCHMARK = "naive_trailing3"
BENCHMARK_LABEL = "naive last-3 mean"
GRAIN = "month"
MIN_BAND_ORIGINS = 3        # below this, the band uses every backtest origin
OUTER_NOMINAL = 0.80
NATIVE_COVERAGE_OK = (0.70, 0.90)
NATIVE_MIN_ROWS = 10

BUILD_STEPS = ("python forecast/build_series.py --source staging, then forecast/lab/build_artifacts.py "
               "--source staging (Chronos env), then python forecast/lab/select_source.py "
               "--source staging")
BUILD_STEPS_LIVE = ("python forecast/build_series.py --source live, then forecast/lab/build_artifacts.py "
                    "--source live (Chronos env), then python forecast/lab/select_source.py "
                    "--source live")


def _build_steps(source: str) -> str:
    return BUILD_STEPS_LIVE if source == LIVE else BUILD_STEPS


_LOCK = Lock()
_CACHE: dict = {}          # (source, estate, horizon) -> (signature, payload)
_SEL_MOD = None


class Unavailable(Exception):
    """The source cannot serve this request; the message says what is missing."""


def _lab():
    """(registry, live-model function, selection module), shared with the Forecast Lab."""
    global _SEL_MOD
    import experiment_router as er
    if _SEL_MOD is None:
        _SEL_MOD = er._load_lab("select_source")
    return er.reg, er._live, _SEL_MOD


def _signature(reg, sel_mod, source: str) -> tuple:
    """Changes whenever a source's series, the artifacts or its selection are rebuilt."""
    paths = [Path(sel_mod.selection_path(reg, source)), Path(reg.ARTIFACT_DIR) / "manifest.json",
             Path(overrides.path())]
    for rid in SOURCE_ESTATES[source].values():
        spec = reg.ESTATES[rid]
        if spec["blocks"] in reg.bw.SOURCES:
            paths.append(Path(reg.bw.source_path(spec["blocks"])))
        if spec.get("features"):
            paths.append(Path(spec["features"]))
    # the path is part of the signature: publishing a run moves every file
    return tuple((str(p), p.stat().st_mtime if p.exists() else None) for p in paths)


def _series_dir(page_id: str, source: str) -> Path:
    return Path(estate_config.series_dir(source, page_id.upper()))


def _meta(page_id: str, source: str = STAGING) -> dict:
    """meta.json of an estate's extract (forecast/build_series.py), either source;
    {} when missing or written under another contract version."""
    path = _series_dir(page_id, source) / "meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    v = meta.get("contract_version")
    if v is not None and int(v) != estate_config.contract_version():
        log.warning("[forecast] %s ignored: contract version %s, expected %s",
                    path, v, estate_config.contract_version())
        return {}
    return meta


def _harvest_days(page_id: str, source: str = STAGING) -> dict:
    """month -> recorded harvest days, from the extract's monthly file (either source)."""
    import csv
    path = _series_dir(page_id, source) / "monthly.csv"
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return {r["month"]: int(r["n_harvest_days"]) for r in csv.DictReader(fh)}
    except Exception:
        return {}


# ── one estate's series, forecast and backtests ─────────────────────────────

_SCORED: dict = {}          # (model, lab id) -> scored frame; cleared with the registry's cache


def _clear_scored():
    _SCORED.clear()


def _scored(reg, model: str, rid: str):
    """Scoreable walk-forward rows of a model, indexed by (origin, step); with the
    model's own lower/upper bounds when its backtest carries them.

    Cached per process: derived models (the seasonal-profile combinations) rebuild
    their backtest from dozens of fits, and Model health's scoreboard asks for every
    model on every request. Cleared wherever the registry's cache is (the series
    files changed). Callers must not modify the frame in place."""
    key = (model, rid)
    if key in _SCORED:
        return _SCORED[key]
    df, _why = reg.backtest(model, rid, GRAIN)
    if df is None or df.empty:
        out = None
    else:
        df = df[df["scoreable"]]
        cols = ["actual", "pred"] + [c for c in ("lower", "upper") if c in df.columns]
        out = df.set_index(["origin", "step"])[cols].astype(float)
    _SCORED[key] = out
    return out


def _context_months(reg, rid: str) -> dict:
    """origin period -> complete months of context at that origin."""
    h = reg.month_history(rid)
    n = h["context_ok"].cumsum().to_numpy()
    return {str(p): int(k) for p, k in zip(h["period"], n)}


def _last_day(reg, rid: str, page_id: str, source: str):
    day = _meta(page_id, source).get("last_day")
    if day:
        return day
    try:
        return str(reg.daily_blocks(rid).index.max().date())
    except Exception:
        return None


def _bundle(reg, live_fn, model: str, page_id: str, source: str = STAGING) -> dict:
    rid = SOURCE_ESTATES[source][page_id]
    label = reg.ESTATES[rid]["label"]
    steps_hint = _build_steps(source)
    try:
        h = reg.month_history(rid)
    except FileNotFoundError:
        raise Unavailable(f"No {source} series for {label} yet. Build it: {steps_hint}.")
    ok, why = reg.static_availability(model, rid, GRAIN)
    if not ok:
        raise Unavailable(f"{label}: {why}.")
    spec = reg.model_spec(model)
    fwd = (live_fn(model, rid, GRAIN) if spec["source"] in ("live", "derived")
           else reg.read_artifact(model, rid, GRAIN))
    if fwd is None:
        raise Unavailable(f"The {source} forecast for {label} has not been built. Build it: {steps_hint}.")

    periods = [str(p) for p in h["period"]]
    anchor = fwd["anchor"]["period"]
    if anchor not in periods:
        raise Unavailable(f"The {source} forecast for {label} was built on data through {anchor}, "
                          f"which the series no longer covers. Rebuild it: {steps_hint}.")
    cut = periods.index(anchor) + 1          # history stops where the forecast starts
    days = _harvest_days(page_id, source)
    history = [{"month": periods[i], "value": round(float(h["value"].iloc[i])),
                "underrecorded": bool(h["underrecorded"].iloc[i]),
                "n_harvest_days": days.get(periods[i])} for i in range(cut)]
    bt = _scored(reg, model, rid)
    bn = _scored(reg, BENCHMARK, rid)
    if bt is None or bn is None:
        raise Unavailable(f"No walk-forward backtest for {label} on {source}. Build it: {steps_hint}.")
    return {
        "id": page_id, "label": label, "history": history,
        "steps": [(s["period"], float(s["value"])) for s in fwd["steps"]],
        "bounds": [(s.get("lower"), s.get("upper")) for s in fwd["steps"]],
        "bt": bt, "bn": bn, "ctx": _context_months(reg, rid),
        "stale": cut < len(periods),
        "stale_note": (f"forecast built on data through {anchor}; the {source} series now runs to "
                       f"{periods[-1]}") if cut < len(periods) else "",
        "generated_at": fwd.get("generated_at"),
        "last_day": _last_day(reg, rid, page_id, source),
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
        cols = ["actual", "pred"]           # quantiles do not add up across estates
        out = parts[0][key].loc[idx, cols].copy()
        for p in parts[1:]:
            out = out + p[key].loc[idx, cols]
        return out

    days = [p["last_day"] for p in parts if p["last_day"]]
    gens = [p["generated_at"] for p in parts if p["generated_at"]]
    return {"id": GROUP, "label": " + ".join(p["label"] for p in parts), "history": history,
            "steps": steps, "bt": _sum("bt"), "bn": _sum("bn"),
            "bounds": [(None, None)] * len(steps),
            "ctx": {h["month"]: i + 1 for i, h in enumerate(history)},
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
    mean_actual = float(b["actual"].abs().mean())
    return {
        "pooled_smape": round(_smape(b), 1),
        "pooled_mase": round(_mae(b) / den, 2) if den else None,
        # mean signed error over the same rows, as a share of the mean actual; + = over-forecast
        "pooled_bias_pct": (round(float((b["pred"] - b["actual"]).mean()) / mean_actual * 100, 1)
                            if mean_actual else None),
        "per_horizon": per_h, "n_rows": int(len(b)),
        "n_origins": int(b.index.get_level_values("origin").nunique()),
        "max_step": max_step,
        "source": "Forecast Lab walk-forward backtest on the staging series",
        "benchmark": "Naive last-3 mean on the same walk-forward rows (MASE denominator)",
    }


def _band_rows(bt, ctx: dict = None, min_context: int = 0):
    """(rows, info): the backtest rows the band and the headline are measured on --
    origins with >= min_context months of context, or every origin when fewer than
    MIN_BAND_ORIGINS qualify."""
    origins = list(dict.fromkeys(bt.index.get_level_values("origin")))
    if not min_context or ctx is None:
        return bt, {"min_context_months": None, "n_origins": len(origins), "low_evidence": False,
                    "rule": f"all {len(origins)} backtest origins"}
    mature = [o for o in origins if ctx.get(o, 0) >= min_context]
    if len(mature) < MIN_BAND_ORIGINS:
        return bt, {"min_context_months": min_context, "n_origins": len(origins), "low_evidence": True,
                    "rule": (f"all {len(origins)} backtest origins (only {len(mature)} with "
                             f">= {min_context} months of context)")}
    rows = bt[bt.index.get_level_values("origin").isin(mature)]
    return rows, {"min_context_months": min_context, "n_origins": len(mature), "low_evidence": False,
                  "rule": f"{len(mature)} origins with >= {min_context} months of context"}


def _step_bands(bt, horizon: int) -> dict:
    """step -> {"band_pct", "n", "coverage", "band80_pct"}: the sMAPE measured at that
    step, how often the actual fell inside pred +/- that share of pred, and the 80th
    percentile of |actual - pred| / pred (the empirical 80% half-width)."""
    out, widest, widest80 = {}, None, None
    for s in range(1, horizon + 1):
        rows = _at_step(bt, s)
        if len(rows):
            band = round(_smape(rows), 1)
            rel = (rows["actual"] - rows["pred"]).abs() / rows["pred"].abs().where(rows["pred"] != 0, 1)
            inside = (rows["actual"] - rows["pred"]).abs() <= rows["pred"].abs() * band / 100.0
            b80 = round(float(rel.quantile(OUTER_NOMINAL)) * 100, 1)
            out[s] = {"band_pct": band, "n": int(len(rows)), "coverage": round(float(inside.mean()), 3),
                      "band80_pct": b80}
            widest = band if widest is None else max(widest, band)
            widest80 = b80 if widest80 is None else max(widest80, b80)
    for s in range(1, horizon + 1):          # a step nothing was scored at takes the widest measured
        out.setdefault(s, {"band_pct": widest, "n": 0, "coverage": None, "band80_pct": widest80})
    return out


def _outer_method(rows, bounds, horizon: int) -> dict:
    """Which 80% range the outer band draws: Chronos's own 10th-90th percentile when it
    held 70-90% of the backtest actuals on enough rows, else the empirical one."""
    have = {"lower", "upper"} <= set(rows.columns) and bounds and all(
        lo is not None and hi is not None for lo, hi in bounds[:horizon])
    if have:
        r = rows.dropna(subset=["lower", "upper"])
        inside = (r["actual"] >= r["lower"]) & (r["actual"] <= r["upper"])
        cov, n = (round(float(inside.mean()), 3), int(len(r))) if len(r) else (None, 0)
        lo_ok, hi_ok = NATIVE_COVERAGE_OK
        if n >= NATIVE_MIN_ROWS and lo_ok <= cov <= hi_ok:
            return {"method": "model_q10_q90", "label": "model's 10th-90th percentile",
                    "coverage": cov, "n": n}
        why = (f"the model's 10th-90th percentile held {cov:.0%} of {n} backtest actuals"
               if n else "the model's percentiles were not backtested")
    else:
        why = "the served model gives no interval at this grain"
    return {"method": "empirical_q80", "label": "80th percentile of the backtest error",
            "coverage": None, "n": int(len(rows)), "note": why}


SPARK_MONTHS = 12


def tile(payload: dict) -> dict:
    """What an estate tile shows, from that estate's own payload (either source)."""
    fc = payload.get("forecast") or []
    if not fc:
        return {}
    hist = {h["month"]: h["value"] for h in payload.get("history") or []}
    total = sum(f["ensemble"] for f in fc)
    last_year = [hist.get(f"{int(f['month'][:4]) - 1}{f['month'][4:]}") for f in fc]
    ly = sum(last_year) if all(v is not None for v in last_year) else None
    # the outlook page's mini chart: the last year of actuals and the forecast path
    spark = {"history": [{"month": h["month"], "value": h["value"],
                          "underrecorded": bool(h.get("underrecorded"))}
                         for h in (payload.get("history") or [])[-SPARK_MONTHS:]],
             "forecast": [{k: f.get(k) for k in ("month", "ensemble", "lower", "upper", "lower80", "upper80")}
                          for f in fc]}
    return {"total": total, "yoy_total": ly,
            "yoy_pct": round((total - ly) / ly * 100, 1) if ly else None,
            "band_pct": payload.get("avg_band_pct"),
            "first_month": fc[0]["month"], "last_month": fc[-1]["month"],
            "spark": spark}


# ── payloads ────────────────────────────────────────────────────────────────

def _selection(source: str = STAGING):
    reg, live_fn, sel_mod = _lab()
    sel = sel_mod.read(reg, source)
    if not sel or not sel.get("served"):
        raise Unavailable(f"No model has been selected for the {source} source yet. "
                          f"Build it: {_build_steps(source)}.")
    return reg, live_fn, sel


def rule_model(sel: dict, source: str, page_id: str) -> tuple:
    """(model id, manual-promotion entry or None) the governance picks for one estate,
    before any manual override. A live estate governed by a manual promotion (K3)
    takes it; every other estate takes its source's selection."""
    if source == LIVE and page_id in SOURCE_ESTATES[LIVE]:
        import experiment_router as er
        mid, entry = er._promotion_mod().served_model(SOURCE_ESTATES[LIVE][page_id])
        if entry:
            return mid, entry
    return sel["served"], None


def served_model(sel: dict, source: str, page_id: str) -> tuple:
    """(model id, manual-promotion entry or None) the page serves for one estate: a
    manual override from Model health when one is set (forecast/overrides.py), else
    rule_model."""
    ov = overrides.get(source, page_id)
    if ov and ov.get("model"):
        return ov["model"], None
    return rule_model(sel, source, page_id)


def _label(reg, model_id) -> str:
    try:
        return reg.model_spec(model_id)["label"]
    except KeyError:
        return str(model_id)


def _pick(reg, live_fn, sel: dict, source: str, page_id: str) -> tuple:
    """(bundle, model id, promotion, serving) for one estate. serving says how the model
    was chosen: "override" (picked on Model health), "promotion" or "rule". An override
    whose model cannot build this estate's forecast falls back to the rule's pick and
    the reason is kept in serving["override_error"]."""
    rule, promo = rule_model(sel, source, page_id)
    serving = {"estate": page_id, "mode": "promotion" if promo else "rule",
               "model": rule, "rule_model": rule}
    ov = overrides.get(source, page_id)
    if ov and ov.get("model") and ov["model"] != rule:
        try:
            bundle = _bundle(reg, live_fn, ov["model"], page_id, source)
            serving.update(mode="override", model=ov["model"], set_at=ov.get("set_at"),
                           set_by=ov.get("set_by"))
            return bundle, ov["model"], None, serving
        except Unavailable as exc:
            log.warning("[forecast] override %s on %s/%s not served: %s", ov["model"], source, page_id, exc)
            serving["override_error"] = (f"{_label(reg, ov['model'])} was picked on Model health but "
                                         f"cannot be served ({exc}), so the rule's pick is served")
    return _bundle(reg, live_fn, rule, page_id, source), rule, promo, serving


def _series(source: str, estate: str) -> tuple:
    """(bundle, registry, selection, model id, promotion, servings) for one estate or the
    staging group. In the group each estate is served its own pick; model id is None
    when those differ."""
    reg, live_fn, sel = _selection(source)
    table = SOURCE_ESTATES[source]
    if estate == GROUP and source == STAGING:
        picks = [_pick(reg, live_fn, sel, source, e) for e in table]
        models = {p[1] for p in picks}
        model = picks[0][1] if len(models) == 1 else None
        return _sum_bundles([p[0] for p in picks]), reg, sel, model, None, [p[3] for p in picks]
    if estate not in table:
        raise Unavailable(f"'{estate}' is not an estate in the {source} source.")
    bundle, model, promo, serving = _pick(reg, live_fn, sel, source, estate)
    return bundle, reg, sel, model, promo, [serving]


def _serving(reg, source: str, servings: list) -> dict:
    """How the page's model was chosen, for the forecast and Model health pages."""
    rows = []
    for s in servings:
        rid = SOURCE_ESTATES[source][s["estate"]]
        rows.append({**s, "estate_label": reg.ESTATES[rid]["label"],
                     "model_label": _label(reg, s["model"]),
                     "rule_model_label": _label(reg, s["rule_model"])})
    modes = {r["mode"] for r in rows}
    return {"mode": "override" if "override" in modes else rows[0]["mode"],
            "estates": rows,
            "errors": [r["override_error"] for r in rows if r.get("override_error")]}


TRACK_WINDOW = 12        # most recent next-month backtest forecasts the outlook reports on
TRACK_TOL_PCT = 10       # "landed within +/-10% of actual"
TRACK_MIN = 6
SHADOW_LOG = Path(__file__).resolve().parent / "forecast" / "lab" / "shadow_log.csv"


def _track_record(bt) -> dict | None:
    """Of the last TRACK_WINDOW next-month forecasts in the walk-forward backtest, how
    many landed within TRACK_TOL_PCT of the actual. The outlook says this instead of
    an error metric. None with fewer than TRACK_MIN scored months."""
    import pandas as pd
    rows = _at_step(bt, 1).sort_index(level="origin").tail(TRACK_WINDOW)
    rows = rows[rows["actual"] != 0]
    if len(rows) < TRACK_MIN:
        return None
    rel = (rows["actual"] - rows["pred"]).abs() / rows["actual"].abs()
    months = [str(pd.Period(o, "M") + 1) for o in rows.index.get_level_values("origin")]
    hits = [bool(x) for x in (rel <= TRACK_TOL_PCT / 100)]
    return {"step": 1, "n": len(hits), "within": sum(hits), "hits": hits, "months": months,
            "tol_pct": TRACK_TOL_PCT, "first_month": months[0], "last_month": months[-1]}


def _revision(source: str, servings: list, anchor: str, forecast: list, log_path=None) -> dict | None:
    """How the forecast moved since the previous issue of the same model for the same
    months, from the shadow log (forecast/lab/shadow.py). Compares the months both
    issues cover; for a group, every estate must have that earlier issue. None when
    there is no earlier issue yet."""
    import pandas as pd
    path = Path(log_path or SHADOW_LOG)
    if not path.exists() or not forecast:
        return None
    try:
        issued = pd.read_csv(path, dtype={"anchor": str, "period": str})
    except Exception as exc:
        log.warning("[forecast] shadow log unreadable: %s", exc)
        return None
    months = [f["month"] for f in forecast]
    prev = {m: 0.0 for m in months}
    anchors = set()
    for s in servings:
        rid = SOURCE_ESTATES[source][s["estate"]]
        rows = issued[(issued["estate"] == rid) & (issued["model"] == s["model"]) & (issued["anchor"] < anchor)]
        if "source" in rows.columns:
            rows = rows[rows["source"].isna() | (rows["source"] == source)]
        if rows.empty:
            return None
        last = rows["anchor"].max()
        anchors.add(last)
        got = rows[rows["anchor"] == last].drop_duplicates("period", keep="last").set_index("period")["pred"]
        for m in list(prev):
            if m in got.index:
                prev[m] += float(got[m])
            else:
                del prev[m]
    if not prev:
        return None
    cur = sum(f["ensemble"] for f in forecast if f["month"] in prev)
    old = sum(prev.values())
    return {"previous_anchor": max(anchors), "months": list(prev), "previous": round(old),
            "current": round(cur), "pct": round((cur - old) / old * 100, 1) if old else None}


def _base(source: str, estate: str, horizon: int) -> dict:
    """The page payload for one scope of one source, without the estate tiles."""
    bundle, reg, sel, model, promo, servings = _series(source, estate)
    serving = _serving(reg, source, servings)
    if model is not None:
        spec = reg.model_spec(model)
        model_id, model_name = spec["id"], spec["label"]
        model_label = model_name + (" (zero-shot)" if "Chronos" in model_name else "")
    else:                       # the staging group with a different pick per estate
        model_id = "mixed"
        model_name = "Mixed: " + "; ".join(f"{r['estate_label']} {r['model_label']}"
                                           for r in serving["estates"])
        model_label = model_name
    band_rows, band_info = _band_rows(bundle["bt"], bundle.get("ctx"), sel.get("min_context_months") or 0)
    bands = _step_bands(band_rows, horizon)
    outer = _outer_method(band_rows, bundle.get("bounds"), horizon)
    history = bundle["history"]
    anchor = {"month": history[-1]["month"], "value": history[-1]["value"]}

    forecast = []
    for i, (month, value) in enumerate(bundle["steps"][:horizon]):
        step = i + 1
        band = bands[step]["band_pct"] or 0.0
        if outer["method"] == "model_q10_q90":
            lo80, hi80 = bundle["bounds"][i]
        else:
            b80 = bands[step]["band80_pct"] or 0.0
            lo80, hi80 = value * (1 - b80 / 100), value * (1 + b80 / 100)
        forecast.append({"month": month, "step": step, "ensemble": round(value),
                         "lower": round(value * (1 - band / 100)),
                         "upper": round(value * (1 + band / 100)), "band_pct": band,
                         "lower80": round(max(0.0, lo80)), "upper80": round(hi80),
                         "band80_pct": round((hi80 - lo80) / 2 / value * 100, 1) if value else None})

    acc = _accuracy(band_rows, bundle["bn"], HEADLINE_STEPS)
    if acc:
        acc["context_rule"] = band_info["rule"]
    step1 = next((h for h in acc.get("per_horizon", []) if h["step"] == 1), {})
    avg_band = round(sum(f["band_pct"] for f in forecast) / len(forecast), 1) if forecast else None
    rids = (list(SOURCE_ESTATES[source].values()) if estate == GROUP
            else [SOURCE_ESTATES[source][estate]])
    flags = [f for f in sel.get("flags", []) if f.get("estate") in rids]
    return {
        "source": source,
        "history": history, "anchor": anchor, "forecast": forecast,
        "interval": {
            "method": "measured_smape",
            "conformal_nominal_pct": None, "conformal_coverage_h1_3": None,
            "per_step": {str(s): {"n_scored": b["n"], "band_pct": b["band_pct"],
                                  "coverage": b["coverage"], "coverage_n": b["n"],
                                  # the 80% half-width actually drawn at that step
                                  "band80_pct": (forecast[s - 1]["band80_pct"] if s <= len(forecast)
                                                 else b["band80_pct"])}
                         for s, b in bands.items()},
            "measured_on": band_info,
            "outer": {**outer, "nominal_pct": round(OUTER_NOMINAL * 100)},
        },
        "drivers": {"note": "", "months": []},
        "track_record": _track_record(bundle["bt"]),
        "revision": _revision(source, servings, anchor["month"], forecast),
        "components": [],
        "benchmark_label": BENCHMARK_LABEL,
        "horizon_choices": list(HORIZON_CHOICES),
        "capabilities": {"intelligence": False, "investigate": False},
        "smape": step1.get("smape"), "smape_1step": step1.get("smape"),
        "headline_smape": acc.get("pooled_smape"), "headline_mase": acc.get("pooled_mase"),
        "accuracy_multih": acc,
        "mase": step1.get("mase"), "n_folds": acc.get("n_origins"),
        "accuracy_note": "" if acc else ("Accuracy unavailable: the served model and the "
                                         "benchmark share no scored walk-forward rows."),
        "model_id": model_id, "model_name": model_name, "model_label": model_label,
        "serving": serving,
        "trained_through": anchor["month"], "data_through": bundle["last_day"],
        "stale_note": bundle["stale_note"],
        "stale": bundle.get("stale", False),
        "selection": {"rule": sel.get("rule"), "reason": sel.get("reason"),
                      "decided_at": sel.get("decided_at")},
        "promotion": promo,
        "flags": flags,
        "horizon": horizon,
        "total_forecast": sum(f["ensemble"] for f in forecast),
        "total_3m": sum(f["ensemble"] for f in forecast[:3]),
        "avg_band_pct": avg_band,
        "last_band_pct": forecast[-1]["band_pct"] if forecast else None,
        "band_pct": step1.get("smape"),
        "series_label": bundle["label"],
        "generated_at": bundle["generated_at"] or datetime.now().isoformat(timespec="seconds"),
    }


def _cached(estate: str, horizon: int, source: str = STAGING) -> dict:
    reg, _live, sel_mod = _lab()
    sig = _signature(reg, sel_mod, source)
    key = (source, estate, horizon)
    with _LOCK:
        if any(k[0] == source and v[0] != sig for k, v in _CACHE.items()):
            for k in [k for k in _CACHE if k[0] == source]:
                del _CACHE[k]
            reg.clear_cache()          # the series files changed under the registry's cache
            _clear_scored()
        hit = _CACHE.get(key)
        if hit is None:
            hit = (sig, _base(source, estate, horizon))
            _CACHE[key] = hit
        return hit[1]


def _scope(estate: str, horizon: int) -> dict:
    """Scope block for the staging source (the live page's scope is forecast_router's)."""
    reg = _lab()[0]
    estates = []
    for pid, rid in SOURCE_ESTATES[STAGING].items():
        row = {"id": pid, "label": reg.ESTATES[rid]["label"], "modeled": True, "has_blocks": False}
        try:
            row["tile"] = tile(_cached(pid, horizon, STAGING))
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


def payload(estate: str = GROUP, horizon: int = HORIZON_DEFAULT, refresh: bool = False,
            source: str = STAGING) -> dict:
    """Forecast page payload for one estate of a source, or the staging group ("all").

    Staging payloads carry their own scope; live ones are given the live page's
    scope by forecast_router.page_payload."""
    horizon = horizon if horizon in HORIZON_CHOICES else HORIZON_DEFAULT
    if refresh:
        with _LOCK:
            for k in [k for k in _CACHE if k[0] == source]:
                del _CACHE[k]
        _lab()[0].clear_cache()
        _clear_scored()
    out = dict(_cached(estate, horizon, source))
    if source == STAGING:
        out["scope"] = _scope(estate, horizon)
    return out


def modeled_estates(source: str = STAGING) -> list:
    """Estate ids of a source that can be served right now."""
    out = []
    for pid in SOURCE_ESTATES[source]:
        try:
            _cached(pid, HORIZON_DEFAULT, source)
            out.append(pid)
        except Unavailable:
            pass
        except Exception as exc:               # a broken artifact is "not available", not a 500
            log.warning("[forecast] %s estate %s unavailable: %s", source, pid, exc)
    return out


def sources() -> list:
    """The two sources, for the page's Database control (labels as on the chat page)."""
    labels = {LIVE: ("Live estates", "K3, EC and EB, from the estate databases"),
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

def scope_estates(source: str, estate: str) -> list:
    """Page estate ids a scope covers. The live group view shows K3 (forecast_router)."""
    table = SOURCE_ESTATES[source]
    if estate in (None, "", GROUP):
        return list(table) if source == STAGING else ["k3"]
    if estate not in table:
        raise Unavailable(f"'{estate}' is not an estate in the {source} source.")
    return [estate]


def set_override(source: str, estate: str, model: str | None) -> dict:
    """Serve `model` on a scope of a source until cleared; None returns it to the rule.

    Refused (Unavailable) unless the model builds a current forecast for every estate
    in the scope, so a pick can never leave the page without a forecast or serve one
    made from older data than the page shows."""
    reg, live_fn, sel = _selection(source)
    ids = scope_estates(source, estate)
    if model is None:
        out = overrides.clear(source, ids)
    else:
        try:
            reg.model_spec(model)
        except KeyError:
            raise Unavailable(f"Unknown model '{model}'.")
        for pid in ids:
            b = _bundle(reg, live_fn, model, pid, source)
            if b["stale"]:
                raise Unavailable(f"{_label(reg, model)} cannot be served on "
                                  f"{b['label']}: its {b['stale_note']}.")
        rules = {pid: rule_model(sel, source, pid)[0] for pid in ids}
        out = overrides.set_model(source, ids, model, rules)
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == source]:
            del _CACHE[k]
    log.info("[forecast] serving on %s/%s set to %s", source, estate, model or "the rule's pick")
    return out


def _board(reg, estate: str, source: str = STAGING) -> list:
    """Step-1 scoreboard of every lab model with a monthly backtest on this scope,
    all scored on the rows they share."""
    table = SOURCE_ESTATES[source]
    ids = scope_estates(source, estate)
    frames, meta = {}, {}
    for m in reg.MODELS:
        parts = [_scored(reg, m["id"], table[e]) for e in ids]
        if any(p is None for p in parts):
            continue
        idx = parts[0].index
        for p in parts[1:]:
            idx = idx.intersection(p.index)
        total = parts[0].loc[idx].copy()
        for p in parts[1:]:
            total = total + p.loc[idx]
        frames[m["label"]] = _at_step(total, 1)
        why = next((r for ok, r in (reg.static_availability(m["id"], table[e], GRAIN) for e in ids)
                    if not ok), "")
        meta[m["label"]] = {"id": m["id"], "servable": not why, "unservable_reason": why}
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
        rows.append({"model": label, **meta[label], "mae": round(float(err.abs().mean())),
                     "rmse": round(float((err ** 2).mean() ** 0.5)),
                     "bias": round(float(err.mean())), "smape": round(_smape(d), 1),
                     "mase": round(_mae(d) / den, 3) if den else None})
    return sorted(rows, key=lambda r: (r["mase"] is None, r["mase"]))


def monitoring(source: str, page_ids: list) -> list:
    """The served run's monitoring rows (forecast/monitor.py via refresh.py) for these
    estates of a source; [] before the first run with monitoring."""
    root = estate_config.run_root()
    if not root:
        return []
    try:
        report = json.loads((Path(root) / "health.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    return [m for m in report.get("monitoring", []) if m["source"] == source and m["estate"] in page_ids]


def health(estate: str = GROUP, source: str = STAGING) -> dict:
    """Model-health payload for one scope of a source, in /forecast/model-health's shape."""
    reg, _live, sel_mod = _lab()
    fc = payload(estate, max(HORIZON_CHOICES), source=source)
    bundle, _reg, sel, model, promo, _servings = _series(source, estate)
    serving = fc["serving"]
    acc_mh = fc.get("accuracy_multih") or {}
    history = fc["history"]
    mean_actual = sum(h["value"] for h in history) / len(history) if history else None
    board = _board(reg, estate, source)
    served = next((r for r in board if r["model"] == fc["model_name"]), None)

    per_step = [{"step": int(s), "smape_pct": b["band_pct"], "n_scored": b["n_scored"],
                 "split_coverage": None, "aci_coverage": None,
                 "served_width": None, "served_width_pct": None,
                 "chart_band_pct": b["band_pct"],
                 "chart_width_pct": round(2 * b["band_pct"], 1) if b["band_pct"] is not None else None,
                 "chart_coverage": b["coverage"], "chart_coverage_n": b["coverage_n"],
                 "outer_band_pct": b.get("band80_pct")}
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

    table = SOURCE_ESTATES[source]
    ids = scope_estates(source, estate)
    files = []
    for e in ids:
        spec = reg.ESTATES[table[e]]
        path = Path(spec.get("features") or reg.bw.source_path(spec["blocks"]))
        files.append(_src(f"{spec['label']} harvest series ({source})", path))
    files += [_src("Forecast Lab artifacts", Path(reg.ARTIFACT_DIR) / "manifest.json"),
              _src("Model selection", sel_mod.selection_path(reg, source))]
    gates = [(_meta(e, source).get("gate") or {}) for e in ids]
    picked = [r for r in serving["estates"] if r["mode"] == "override"]
    if picked:
        decided_by = "a manual pick on Model health (" + "; ".join(
            f"{r['estate_label']} since {r.get('set_at')}, the rule picks {r['rule_model_label']}"
            for r in picked) + ")"
    elif promo:
        decided_by = f"manual promotion {promo['id']} ({promo['date']}: {promo['reason']})"
    else:
        decided_by = f"rule {sel.get('rule')} ({sel.get('reason')})"
    return {
        "source": source,
        "scope": fc.get("scope"),
        "series_label": fc["series_label"],
        "benchmark_label": BENCHMARK_LABEL,
        "lifecycle": {
            "served_model": fc["model_name"], "served_ensemble": fc["model_name"],
            "served_label": fc["model_label"], "served_model_id": fc["model_id"],
            "serving": serving, "trained_through": last,
            "artifact_built": bundle["generated_at"],
            "version": ("manual pick" if picked else promo["id"] if promo else sel.get("rule")),
            "next_retrain": None,
            "registry_note": (f"Zero-shot model: nothing is trained. It is served by {decided_by}. "
                              f"After each {source} refresh rebuild and re-select: "
                              f"{_build_steps(source)}."),
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
            # the 3-month product's bias, on the rows smape_h1_3 / mase_h1_3 use (the overview's figure)
            "bias_pct_h1_3": acc_mh.get("pooled_bias_pct"),
            "n_folds": acc_mh.get("n_origins"),
            "benchmark": "Naive last-3 mean (MASE denominator)",
        },
        "scoreboard": board,
        "interval": {"served_method": None, "nominal_pct": None, "coverage_h1_3": None,
                     "coverage_ok": None, "chart_method": "measured_smape", "per_step": per_step,
                     "measured_on": fc["interval"].get("measured_on"),
                     "outer": fc["interval"].get("outer")},
        "data_quality": {
            "months_history": len(history),
            "first_month": history[0]["month"] if history else None,
            "last_complete_month": last, "underrecorded_months": len(under),
            "underrecorded_pct": round(len(under) / len(history) * 100, 1) if history else None,
            "freshness_months": fresh, "weather": None, "sources": files,
            "extract_gate_ok": (all(g.get("ok") for g in gates)
                                if gates and all(gates) else None),
        },
        "drift": {"available": False,
                  "reason": f"No reference-vs-live distribution monitor is wired up for the {source} series.",
                  "features": ["Production (target)", "Harvest days"]},
        "research": {"available": False,
                     "reason": "Every model is compared in the Forecast Lab, on the same "
                               "walk-forward rows as the served one."},
        "flags": fc.get("flags", []),
        "promotion": promo,
        "monitoring": monitoring(source, ids),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
