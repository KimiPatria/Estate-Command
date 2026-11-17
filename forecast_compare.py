"""
Compare sources: the client exhibit (buildplan_forecast_mlops.md Phase 4).

Puts the two ingestion paths side by side -- the per-estate live EPMS databases
and the central staging database -- so the choice between them rests on what
each one gives:

  availability  which data items each source holds, with the source's own column
                header and a status measured from the database (meta.json
                `columns`, written by forecast/build_series.py). Nothing here is
                typed in by hand.
  history       how far back each source goes, per estate, and whether it can be
                forecast at all
  agreement     where both have the same month, do the bunch totals match
  accuracy      the served Chronos-2 arm of each source, on its full window and on
                the window both sources share (same origins, same target months),
                with the combination as a Lab reference
  forward       each source's forecast for the same estate
  cost          connections a refresh needs, estates covered, data dates, extract time

Read-only: files and artifacts only, cached on their modification times.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock

log = logging.getLogger("epms-forecast")

_LOCK = Lock()
_CACHE: dict = {}
SOURCES = ("live", "staging")
STEPS = (1, 2, 3)
REFERENCE = "combo_weekly_profile"
SAME_MODELS = ("chronos2_monthly", "chronos2_estate_weekly", "combo_weekly_profile")


def _fs():
    import forecast_sources as fs
    return fs


def _cfg():
    from forecast import estate_config
    return estate_config


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _extract(sid: str, code: str) -> dict:
    """meta.json + columns.json + monthly.csv of one extract (empty when not built)."""
    import pandas as pd
    d = Path(_cfg().series_dir(sid, code))
    meta = _read_json(d / "meta.json") or {}
    cols = _read_json(d / "columns.json") or {}
    try:
        monthly = pd.read_csv(d / "monthly.csv", dtype={"month": str})
    except Exception:
        monthly = None
    return {"meta": meta, "stats": cols.get("stats") or {}, "registry": cols.get("registry") or [],
            "monthly": monthly, "dir": d}


def _signature() -> tuple:
    ec = _cfg()
    paths = [Path(ec.CONFIG_PATH)]
    for sid in SOURCES:
        for code in ec.estates(sid):
            d = Path(ec.series_dir(sid, code))
            paths += [d / "meta.json", d / "columns.json"]
        paths.append(Path(_fs()._lab()[0].ARTIFACT_DIR) / f"{sid}_selection.json")
    paths.append(Path(_fs()._lab()[0].ARTIFACT_DIR) / "manifest.json")
    return tuple((str(p), p.stat().st_mtime if p.exists() else None) for p in paths)


# ── availability ────────────────────────────────────────────────────────────

def _availability(ex: dict) -> dict:
    from forecast import contract as contract_items
    th = _cfg().thresholds()
    items = {}
    for sid in SOURCES:
        for code, e in ex[sid].items():
            for c in e["registry"]:
                st = e["stats"].get(c["source_name"])
                slot = items.setdefault(c["item"], {}).setdefault(sid, {"columns": [], "rows": 0, "filled": 0,
                                                                        "per_estate": {}})
                if c["source_name"] not in slot["columns"]:
                    slot["columns"].append(c["source_name"])
                if st is None:
                    slot["per_estate"][code] = {"column": c["source_name"], "status": "not available"}
                    continue
                fill = st["filled"] / st["rows"] if st.get("rows") else 0.0
                slot["rows"] += int(st.get("rows") or 0)
                slot["filled"] += int(st.get("filled") or 0)
                slot["per_estate"][code] = {
                    "column": c["source_name"], "fill_rate": round(fill, 4),
                    "status": contract_items.status(fill, th),
                    "first_month": st.get("first_month"), "last_month": st.get("last_month"),
                    **({"note": st["note"]} if st.get("note") else {})}
    # data joined from outside the source (estates.yaml `joined`)
    for sid in SOURCES:
        joined = [(code, j) for code, e in _cfg().estates(sid).items() for j in e.get("joined", [])]
        if joined:
            slot = items.setdefault("weather", {}).setdefault(sid, {"columns": [], "rows": 0, "filled": 0,
                                                                    "per_estate": {}, "joined": True})
            for code, j in joined:
                if j not in slot["columns"]:
                    slot["columns"].append(j)
                slot["per_estate"][code] = {"column": j, "status": "populated", "joined": True}
    out = []
    for item, label in contract_items.ITEMS.items():
        if item not in items:
            continue
        row = {"item": item, "label": label}
        for sid in SOURCES:
            slot = items[item].get(sid)
            if not slot:
                row[sid] = {"columns": [], "status": "not available", "per_estate": {}}
                continue
            if slot.get("joined"):
                status, fill = "populated", None
            else:
                fill = round(slot["filled"] / slot["rows"], 4) if slot["rows"] else None
                status = contract_items.status(fill, th) if fill is not None else "not available"
            row[sid] = {"columns": slot["columns"], "status": status, "fill_rate": fill,
                        "per_estate": slot["per_estate"], **({"joined": True} if slot.get("joined") else {})}
        out.append(row)
    return {"items": out, "thresholds": th}


# ── history and agreement ──────────────────────────────────────────────────

def _forecastable(reg, sid: str, code: str, e: dict) -> tuple:
    if not e["forecastable"]:
        return False, "listed for comparison only (estates.yaml forecastable: false)"
    ok, reason, n = reg.estate_grain_status(e["lab_id"], "month")
    return ok, ("" if ok else reason)


def _history(ex: dict, reg) -> list:
    ec = _cfg()
    codes = list(dict.fromkeys(code for sid in SOURCES for code in ec.estates(sid)))
    rows = []
    for code in codes:
        row = {"estate": code}
        for sid in SOURCES:
            e = ec.estates(sid).get(code)
            x = ex[sid].get(code)
            if not e or not x or x["monthly"] is None:
                row[sid] = {"available": False,
                            "reason": "not in this source" if not e else "not extracted yet"}
                continue
            m = x["monthly"]
            full = m[m["is_partial"] == 0]["month"].tolist()
            ok, why = _forecastable(reg, sid, code, e)
            row[sid] = {"available": True, "first_month": m["month"].iloc[0], "last_month": m["month"].iloc[-1],
                        "months": [{"month": r.month, "partial": bool(r.is_partial)} for r in m.itertuples()],
                        "full_months": len(full), "first_day": x["meta"].get("first_day"),
                        "last_day": x["meta"].get("last_day"), "forecastable": ok, "reason": why}
        rows.append(row)
    return rows


def _agreement(ex: dict) -> list:
    out = []
    for code in ex["live"]:
        if code not in ex["staging"]:
            continue
        a, b = ex["live"][code]["monthly"], ex["staging"][code]["monthly"]
        if a is None or b is None:
            continue
        j = a.merge(b, on="month", suffixes=("_live", "_staging"))
        j = j[(j["is_partial_live"] == 0) & (j["is_partial_staging"] == 0)]
        months = []
        for r in j.itertuples():
            lv, sv = float(r.bunches_total_live), float(r.bunches_total_staging)
            months.append({"month": r.month, "live": round(lv), "staging": round(sv),
                           "diff_pct": round((sv - lv) / lv * 100, 2) if lv else None})
        mism = [m for m in months if m["diff_pct"] is not None and abs(m["diff_pct"]) > 0.5]
        out.append({"estate": code, "shared_months": len(months),
                    "matching_months": len(months) - len(mism), "months": months, "mismatches": mism})
    return out


# ── accuracy and forward ───────────────────────────────────────────────────

def _smape(rows) -> float | None:
    if rows is None or rows.empty:
        return None
    d = rows["actual"].abs() + rows["pred"].abs()
    return round(float((2 * (rows["pred"] - rows["actual"]).abs() / d.where(d != 0, 1)).mean() * 100), 1)


def _steps(rows):
    return rows[rows.index.get_level_values("step").isin(STEPS)] if rows is not None else None


def _accuracy_and_forward(reg) -> tuple:
    fs = _fs()
    ec = _cfg()
    sels = {}
    for sid in SOURCES:
        try:
            sels[sid] = fs._selection(sid)[2]
        except fs.Unavailable:
            sels[sid] = None
    shared_codes = [c for c in ec.estates("staging") if c in ec.estates("live")]
    acc, fwd = [], []
    for code in list(dict.fromkeys(list(ec.estates("live")) + list(ec.estates("staging")))):
        pid = code.lower()
        a_row = {"estate": code}
        f_row = {"estate": code}
        full_rows = {}
        for sid in SOURCES:
            e = ec.estates(sid).get(code)
            sel = sels.get(sid)
            if not e or sel is None or pid not in fs.SOURCE_ESTATES[sid]:
                a_row[sid] = f_row[sid] = {"available": False, "reason": "not in this source"}
                continue
            try:
                d = fs.payload(pid, 3, source=sid)
            except fs.Unavailable as exc:
                a_row[sid] = f_row[sid] = {"available": False, "reason": str(exc)}
                continue
            model, _promo = fs.served_model(sel, sid, pid)
            bt = fs._scored(reg, model, e["lab_id"])
            band_rows, info = fs._band_rows(bt, fs._context_months(reg, e["lab_id"]),
                                            sel.get("min_context_months") or 0)
            full_rows[sid] = (model, bt)
            ref_bt = fs._scored(reg, REFERENCE, e["lab_id"]) if reg.static_availability(
                REFERENCE, e["lab_id"], "month")[0] else None
            ref_rows = ref_bt.loc[ref_bt.index.intersection(band_rows.index)] if ref_bt is not None else None
            a_row[sid] = {"available": True, "model": model, "model_label": reg.model_spec(model)["label"],
                          "smape": _smape(_steps(band_rows)), "origins": info["n_origins"],
                          "measured_on": info["rule"], "low_evidence": info["low_evidence"],
                          "band": [f["band_pct"] for f in d["forecast"]],
                          "reference_smape": _smape(_steps(ref_rows)) if ref_rows is not None else None,
                          "flags": d.get("flags", [])}
            f_row[sid] = {"available": True, "model_label": d["model_label"], "anchor": d["anchor"],
                          "history": d["history"][-12:],
                          "steps": [{"month": f["month"], "value": f["ensemble"], "lower": f["lower"],
                                     "upper": f["upper"], "lower80": f.get("lower80"),
                                     "upper80": f.get("upper80")} for f in d["forecast"]]}
        if code in shared_codes and len(full_rows) == 2:
            (lm, lbt), (sm, sbt) = full_rows["live"], full_rows["staging"]
            keys = _steps(lbt).index.intersection(_steps(sbt).index)
            if len(keys):
                a_row["shared"] = {"origins": int(keys.get_level_values("origin").nunique()),
                                   "rows": int(len(keys)),
                                   "first_origin": min(keys.get_level_values("origin")),
                                   "last_origin": max(keys.get_level_values("origin")),
                                   "live": _smape(lbt.loc[keys]), "staging": _smape(sbt.loc[keys])}
            else:
                a_row["shared"] = {"origins": 0, "rows": 0, "live": None, "staging": None,
                                   "reason": "the two sources have no backtest origin in common"}
            # The same model on both sources, on the rows both have: what is left of the
            # difference is the data itself, not the arm or the length of history.
            same = []
            for m in SAME_MODELS:
                lids = [ec.estates(s)[code]["lab_id"] for s in SOURCES]
                if not all(reg.static_availability(m, rid, "month")[0] for rid in lids):
                    continue
                a, b = (fs._scored(reg, m, rid) for rid in lids)
                if a is None or b is None:
                    continue
                k = _steps(a).index.intersection(_steps(b).index)
                if len(k):
                    same.append({"model": m, "model_label": reg.model_spec(m)["label"],
                                 "origins": int(k.get_level_values("origin").nunique()), "rows": int(len(k)),
                                 "live": _smape(a.loc[k]), "staging": _smape(b.loc[k])})
            a_row["same_model"] = same
        acc.append(a_row)
        fwd.append(f_row)
    return acc, fwd


# ── connection cost ────────────────────────────────────────────────────────

def _cost(ex: dict) -> list:
    ec = _cfg()
    out = []
    for sid in SOURCES:
        ests = ec.estates(sid)
        dbs = sorted({e["database_env"] for e in ests.values() if e.get("database_env")})
        files = sorted({e["path"] for e in ests.values() if e.get("path") and e["adapter"] != "file_drop"})
        metas = [x["meta"] for x in ex[sid].values() if x["meta"]]
        out.append({
            "source": sid, "label": ec.source(sid)["label"], "description": ec.source(sid)["description"],
            "databases": len(dbs), "files": len(files),
            "estates": [e["label"] for e in ests.values()],
            "forecastable": [e["label"] for c, e in ests.items()
                             if e["forecastable"] and x_ok(ex[sid].get(c))],
            "last_day": max((m.get("last_day") for m in metas if m.get("last_day")), default=None),
            "oldest_last_day": min((m.get("last_day") for m in metas if m.get("last_day")), default=None),
            "built_at": max((m.get("built_at") for m in metas if m.get("built_at")), default=None),
            "extract_seconds": (round(sum(m.get("extract_seconds") or 0 for m in metas), 1)
                                if any(m.get("extract_seconds") for m in metas) else None),
            "gate_ok": all((m.get("gate") or {}).get("ok") for m in metas) if metas else None,
            "alerts": _alerts(sid),
        })
    return out


def _alerts(sid: str) -> list:
    """Monitoring alerts of the served run for one source (health.json, refresh.py)."""
    root = _cfg().run_root()
    if not root:
        return []
    try:
        report = json.loads((Path(root) / "health.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    return [m for m in report.get("monitoring", []) if m["source"] == sid and m["level"] == "alert"]


def x_ok(x) -> bool:
    return bool(x and x["meta"] and (x["meta"].get("full_months") or 0) >= 3)


# ── payload ────────────────────────────────────────────────────────────────

def _build() -> dict:
    fs = _fs()
    reg = fs._lab()[0]
    ec = _cfg()
    ex = {sid: {code: _extract(sid, code) for code in ec.estates(sid)} for sid in SOURCES}
    acc, fwd = _accuracy_and_forward(reg)
    return {
        "sources": [{"id": sid, "label": ec.source(sid)["label"],
                     "description": ec.source(sid)["description"]} for sid in SOURCES],
        "availability": _availability(ex),
        "history": _history(ex, reg),
        "agreement": _agreement(ex),
        "accuracy": acc,
        "forward": fwd,
        "cost": _cost(ex),
        "reference_label": reg.model_spec(REFERENCE)["label"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def payload(refresh: bool = False) -> dict:
    sig = _signature()
    with _LOCK:
        hit = _CACHE.get("compare")
        if refresh or hit is None or hit[0] != sig:
            hit = (sig, _build())
            _CACHE["compare"] = hit
        return hit[1]
