"""
Experiment pages — FastAPI router, mounted at /experiment on dashboard_server.py.

The Forecast Lab (/experiment/forecast) puts forecasting models side by side on
any estate, at a monthly or weekly grain, over a 1-12 step horizon. It is an
experiment surface for the forecasting team, kept apart from the client-facing
/forecast page: that page and its endpoints are untouched, and the lab only
reads the served model through forecast_router.get_forecast, the same call the
page makes.

Models come from forecast/lab/registry.py:
  live      computed here per request (the served ensemble, the two baselines)
  artifact  read from forecast/experiments/lab/, written by
            forecast/lab/build_artifacts.py in the chronos-rd-py313 env (this
            server's env has no Chronos)

Endpoints:
  GET /experiment/forecast/catalog                 estates, grains, models, build info
  GET /experiment/forecast/data?estate=&grain=     history + every model's 12-step
                                                   forecast; the page slices the horizon
  GET /experiment/forecast/backtest?estate=&grain= every model's walk-forward rows
                                                   (origin, step, period, actual, pred)
  GET /experiment/forecast/promotion               champion/challenger rule status
                                                   (forecast/lab/promotion.py)
"""

import importlib.util
import logging
from pathlib import Path
from threading import Lock

import numpy as np
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

log = logging.getLogger("epms-experiment")

router = APIRouter(prefix="/experiment", tags=["experiment"])

_LAB_DIR = Path(__file__).parent / "forecast" / "lab"
_LOCK = Lock()
BUILD_COMMAND = (r"C:\Users\DP\anaconda3\envs\chronos-rd-py313\python.exe "
                 "forecast/lab/build_artifacts.py")


def _load_lab(name):
    spec = importlib.util.spec_from_file_location(f"epms_lab_{name}", _LAB_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load_lab("registry")
_promotion = None


def _promotion_mod():
    """Loaded on first use: it pulls in scipy and the shadow log."""
    global _promotion
    if _promotion is None:
        _promotion = _load_lab("promotion")
    return _promotion


def _history_rows(estate, grain):
    h = reg.history(estate, grain)
    return [{"period": reg.period_str(p, grain), "value": round(float(v)),
             "underrecorded": bool(u)}
            for p, v, u in zip(h["period"], h["value"], h["underrecorded"])]


def _live(model_id, estate, grain):
    """Forecast payload for a live model, in the artifact schema."""
    if model_id == "served_ensemble":
        from forecast_router import get_forecast
        # Never refresh: that cache entry is the one /forecast's 1Y view serves,
        # and the lab only reads what the client page would show.
        d = get_forecast(estate=reg.ESTATES[estate]["served"], horizon=reg.MAX_H)
        return {
            "anchor": {"period": d["anchor"]["month"], "value": d["anchor"]["value"]},
            "data_through": d["anchor"]["month"],
            "generated_at": d.get("generated_at"),
            "band": {"label": "± measured backtest sMAPE per step, as on /forecast"},
            "notes": [f"{d.get('model_name', 'Ensemble')}, trained through "
                      f"{d.get('trained_through') or d['anchor']['month']}"],
            "steps": [{"period": f["month"], "value": f["ensemble"], "lower": f["lower"],
                       "upper": f["upper"]} for f in d["forecast"]],
        }
    if model_id == "naive_trailing3":
        ctx, anchor = reg.context(estate, grain)
        level = float(np.mean(ctx[-3:]))
        return {
            "anchor": {"period": reg.period_str(anchor, grain), "value": round(float(ctx[-1]))},
            "data_through": reg.period_str(anchor, grain), "generated_at": None,
            "band": None, "notes": [f"flat at {level:,.0f}"],
            "steps": [{"period": reg.period_str(p, grain), "value": round(level),
                       "lower": None, "upper": None}
                      for p in reg.future_periods(anchor, grain)],
        }
    if model_id == "seasonal_naive":
        h = reg.history(estate, grain)
        vals, k, last = h["value"].to_numpy(dtype=float), reg.SEASON[grain], len(h) - 1
        anchor = h["period"].iloc[-1]
        return {
            "anchor": {"period": reg.period_str(anchor, grain), "value": round(float(vals[-1]))},
            "data_through": reg.period_str(anchor, grain), "generated_at": None,
            "band": None, "notes": [f"repeats {reg.period_str(h['period'].iloc[last + 1 - k], grain)}"
                                    f" – {reg.period_str(anchor, grain)}"],
            "steps": [{"period": reg.period_str(p, grain), "value": round(float(vals[last + s - k])),
                       "lower": None, "upper": None}
                      for s, p in enumerate(reg.future_periods(anchor, grain), start=1)],
        }
    raise KeyError(model_id)


def _model_result(spec, estate, grain, lab_anchor):
    out = {k: spec[k] for k in ("id", "label", "color", "dash", "family", "about", "source")}
    ok, reason = reg.static_availability(spec["id"], estate, grain)
    if not ok:
        return {**out, "available": False, "reason": reason}
    try:
        res = (_live(spec["id"], estate, grain) if spec["source"] == "live"
               else reg.read_artifact(spec["id"], estate, grain))
    except Exception as exc:
        log.exception("[lab] %s failed on %s/%s", spec["id"], estate, grain)
        return {**out, "available": False, "reason": f"failed: {exc}"}
    if res is None:
        return {**out, "available": False,
                "reason": "not built yet -- run forecast/lab/build_artifacts.py"}
    stale = res["anchor"]["period"] != lab_anchor
    return {**out, "available": True, "reason": "",
            "anchor": res["anchor"], "steps": res["steps"][:reg.MAX_H],
            "band": res.get("band"), "notes": res.get("notes", []),
            "generated_at": res.get("generated_at"), "data_through": res.get("data_through"),
            "stale": stale,
            "stale_note": (f"built on data through {res['anchor']['period']}; the latest "
                           f"data runs to {lab_anchor}") if stale else ""}


@router.get("/forecast/catalog")
def lab_catalog(refresh: bool = False):
    try:
        with _LOCK:
            if refresh:
                reg.clear_cache()
            estates = []
            for eid, spec in reg.ESTATES.items():
                grains = {}
                for g in ("month", "week"):
                    ok, reason, n = reg.estate_grain_status(eid, g)
                    h = reg.history(eid, g) if n else None
                    grains[g] = {"available": ok, "reason": reason, "n": n,
                                 "first": reg.period_str(h["period"].iloc[0], g) if n else None,
                                 "last": reg.period_str(h["period"].iloc[-1], g) if n else None}
                estates.append({"id": eid, "label": spec["label"],
                                "served": bool(spec["served"]), "grains": grains})
        manifest = reg.read_manifest() or {}
        return {
            "estates": estates,
            "models": [{k: m[k] for k in ("id", "label", "color", "dash", "family",
                                          "about", "grains", "source")} for m in reg.MODELS],
            "max_h": reg.MAX_H,
            "artifacts": {"built_at": manifest.get("built_at"), "command": BUILD_COMMAND},
        }
    except Exception as exc:
        log.exception("[lab] catalog failed")
        return JSONResponse(status_code=500, content={"detail": f"Lab catalog failed: {exc}"})


@router.get("/forecast/data")
def lab_data(estate: str = Query(...), grain: str = Query("month"), refresh: bool = False):
    if estate not in reg.ESTATES:
        return JSONResponse(status_code=404, content={"detail": f"Unknown estate '{estate}'."})
    if grain not in ("month", "week"):
        return JSONResponse(status_code=400, content={"detail": "grain must be month or week"})
    try:
        with _LOCK:
            if refresh:
                reg.clear_cache()
            ok, reason, _ = reg.estate_grain_status(estate, grain)
            if not ok:
                return JSONResponse(status_code=409, content={"detail": reason})
            history = _history_rows(estate, grain)
        lab_anchor = history[-1]["period"]
        models = [_model_result(m, estate, grain, lab_anchor) for m in reg.MODELS]
        return {"estate": estate, "label": reg.ESTATES[estate]["label"], "grain": grain,
                "anchor": lab_anchor, "max_h": reg.MAX_H,
                "history": history, "models": models}
    except Exception as exc:
        log.exception("[lab] data failed for %s/%s", estate, grain)
        return JSONResponse(status_code=500, content={"detail": f"Lab data failed: {exc}"})


@router.get("/forecast/backtest")
def lab_backtest(estate: str = Query(...), grain: str = Query("month")):
    if estate not in reg.ESTATES:
        return JSONResponse(status_code=404, content={"detail": f"Unknown estate '{estate}'."})
    if grain not in ("month", "week"):
        return JSONResponse(status_code=400, content={"detail": "grain must be month or week"})
    try:
        with _LOCK:
            ok, reason, _ = reg.estate_grain_status(estate, grain)
            if not ok:
                return JSONResponse(status_code=409, content={"detail": reason})
            history = _history_rows(estate, grain)
            models = []
            for m in reg.MODELS:
                out = {k: m[k] for k in ("id", "label", "color", "dash", "family", "source")}
                df, why = reg.backtest(m["id"], estate, grain)
                if df is None or df.empty:
                    models.append({**out, "available": False,
                                   "reason": why or "no origin has enough history"})
                    continue
                models.append({**out, "available": True, "reason": "",
                               "rows": [{"origin": o, "step": int(s), "period": p,
                                         "actual": round(float(a)), "pred": round(float(v)),
                                         "scoreable": bool(sc)}
                                        for o, s, p, a, v, sc in df[reg.BACKTEST_COLS]
                                        .itertuples(index=False)]})
        return {"estate": estate, "label": reg.ESTATES[estate]["label"], "grain": grain,
                "max_h": reg.MAX_H, "history": history, "models": models}
    except Exception as exc:
        log.exception("[lab] backtest failed for %s/%s", estate, grain)
        return JSONResponse(status_code=500, content={"detail": f"Lab backtest failed: {exc}"})


@router.get("/forecast/promotion")
def lab_promotion():
    try:
        return _promotion_mod().status()
    except Exception as exc:
        log.exception("[lab] promotion status failed")
        return JSONResponse(status_code=500, content={"detail": f"Promotion status failed: {exc}"})
