"""
Provenance + physics audit for an estate weather CSV.

Why this exists
---------------
The training scoreboard cannot see the weather file. build_weather_monthly()'s
output reaches the served forward forecast and the conformal intervals, but the
walk-forward metric that validates the model was, until recently, computed
entirely from columns baked into features_estate_monthly.csv. Swapping
weather_nasa_power_history.csv left every scoreboard figure bit-identical, so a
corrupt weather file was completely silent.

That is not hypothetical. K3's original weather file averaged ~4,250 mm/yr of
rainfall against a ~2,167 mm/yr published climatology for Lahad Datu, correlated
with NASA POWER at the estate's own coordinates at r = -0.004 daily (no date
shift within +/-24 months explained it), and stored an et0_mm that could not be
reproduced from its own temperature columns by any formula in this package. It
had trained the served model for months without anything flagging it.

The decisive check here is ET0 reproducibility: recompute ET0 from the file's
OWN stored columns and compare against the stored value. A file whose stored
ET0 cannot be reproduced from its stored inputs did not come from this pipeline,
whatever its header says.

Used by forecast_router._model_health() and printed by fetch_nasa_weather.py
after every fetch.
"""

import csv
import datetime
import json
import os

# Plausibility bands for a lowland humid-tropical estate. Deliberately wide —
# these catch a wrong location or a broken pipeline, not fine bias.
RAIN_BAND_MM_YR = (1200.0, 4500.0)
ET0_BAND_MM_DAY = (2.5, 6.0)

_STALE_WARN_DAYS = 45
_ET0_TOLERANCE_MM = 0.05
_ET0_REPRO_MIN_PCT = 99.0


def meta_path(csv_path: str) -> str:
    """Sidecar path recording what was actually fetched, next to the CSV."""
    return csv_path + ".meta.json"


def _f(row: dict, key: str):
    v = (row.get(key) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _load_et0_functions():
    """Import the ET0 formulas from the fetcher that owns their definition.

    Loaded lazily and by path so this module stays importable from the router
    without putting forecast/ on sys.path.
    """
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "epms_fetch_nasa_weather", os.path.join(here, "fetch_nasa_weather.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def validate_weather_csv(path: str) -> dict:
    """Audit a weather CSV. Returns a dict with per-check status and an overall
    status of "ok" | "warn" | "fail" | "unknown"."""
    out: dict = {"available": False, "path": path, "checks": [], "status": "unknown"}

    def add(name, status, detail):
        out["checks"].append({"name": name, "status": status, "detail": detail})

    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        add("File readable", "fail", str(exc))
        out["status"] = "fail"
        return out
    if not rows:
        add("File readable", "fail", "file has no data rows")
        out["status"] = "fail"
        return out

    out["available"] = True
    out["rows"] = len(rows)

    # ── provenance sidecar ───────────────────────────────────────────────────
    meta = None
    try:
        with open(meta_path(path), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = None
    out["provenance"] = meta
    if meta:
        add("Provenance recorded", "ok",
            f"{meta.get('source')} at ({meta.get('latitude')}, "
            f"{meta.get('longitude')}), ET0 via {meta.get('et0_method')}")
    else:
        add("Provenance recorded", "warn",
            "no .meta.json sidecar — this file was not written by "
            "fetch_nasa_weather.py, so its source and coordinates cannot be verified")

    # ── coverage / freshness ─────────────────────────────────────────────────
    dates = sorted(r["date"] for r in rows if r.get("date"))
    if dates:
        out["first_date"], out["last_date"] = dates[0], dates[-1]
        try:
            last = datetime.date.fromisoformat(dates[-1])
            stale = (datetime.date.today() - last).days
            out["stale_days"] = stale
            add("Freshness", "ok" if stale <= _STALE_WARN_DAYS else "warn",
                f"last record {dates[-1]} ({stale} days old)")
        except ValueError:
            out["stale_days"] = None

    blank = sum(1 for r in rows
                if _f(r, "rainfall_mm") is None or _f(r, "temp_mean_c") is None)
    out["blank_rows"] = blank
    add("No blank rows", "ok" if blank == 0 else "fail",
        f"{blank} row(s) missing core fields" if blank else "all rows complete")

    # ── plausibility ─────────────────────────────────────────────────────────
    rain = [x for x in (_f(r, "rainfall_mm") for r in rows) if x is not None]
    if rain:
        annual = sum(rain) / len(rain) * 365.0
        out["annual_rainfall_mm"] = round(annual, 0)
        lo, hi = RAIN_BAND_MM_YR
        add("Rainfall plausible", "ok" if lo <= annual <= hi else "fail",
            f"{annual:,.0f} mm/yr (expected {lo:,.0f}–{hi:,.0f} for a "
            f"humid-tropical estate)")

    et0 = [x for x in (_f(r, "et0_mm") for r in rows) if x is not None]
    if et0:
        mean_et0 = sum(et0) / len(et0)
        out["et0_mean_mm_day"] = round(mean_et0, 3)
        lo, hi = ET0_BAND_MM_DAY
        add("ET0 plausible", "ok" if lo <= mean_et0 <= hi else "fail",
            f"{mean_et0:.2f} mm/day (expected {lo}–{hi})")

    # ── ET0 reproducibility from the file's own columns ──────────────────────
    # The decisive check. When the sidecar gives a latitude we use it; otherwise
    # we sweep plausible tropical latitudes and take the best fit, because Ra
    # varies only ~10% across that range. A file that matches at NO latitude did
    # not come from this pipeline — which is exactly how K3's legacy file, whose
    # stored ET0 was ~2.2x anything its own temperatures could produce, is caught
    # despite having no provenance record at all.
    lat_known = (meta or {}).get("latitude")
    try:
        fw = _load_et0_functions()
    except Exception as exc:  # pragma: no cover - defensive
        add("ET0 reproducible", "warn", f"could not load ET0 formulas: {exc}")
        out["et0_reproducible_pct"] = None
        fw = None

    if fw is not None:
        candidates = ([float(lat_known)] if lat_known is not None
                      else [x * 2.5 for x in range(-8, 9)])  # -20..+20 degrees

        def _score(cand_lat):
            ok = tot = 0
            method = "Hargreaves-Samani"
            for r in rows:
                tm, tx, tn = (_f(r, "temp_mean_c"), _f(r, "temp_max_c"),
                              _f(r, "temp_min_c"))
                rh, rs = _f(r, "humidity_pct"), _f(r, "solar_radiation")
                ws, ps = _f(r, "wind_2m_ms"), _f(r, "pressure_kpa")
                stored = _f(r, "et0_mm")
                if None in (tm, tx, tn, stored):
                    continue
                try:
                    doy = datetime.date.fromisoformat(r["date"]).timetuple().tm_yday
                except (ValueError, KeyError):
                    continue
                if None not in (rh, rs, ws, ps):
                    calc = fw.penman_monteith_et0(tm, tx, tn, rh, rs, ws, ps,
                                                  cand_lat, doy)
                    method = "FAO-56 Penman-Monteith"
                else:
                    calc = fw.hargreaves_et0(tm, tx, tn, cand_lat, doy)
                tot += 1
                if abs(calc - stored) <= _ET0_TOLERANCE_MM:
                    ok += 1
            return ((100.0 * ok / tot) if tot else 0.0), method

        best_pct, best_lat, best_method = -1.0, None, None
        for cand in candidates:
            pct, method = _score(cand)
            if pct > best_pct:
                best_pct, best_lat, best_method = pct, cand, method

        out["et0_reproducible_pct"] = round(best_pct, 1)
        out["et0_method_implied"] = best_method
        where = (f"latitude {best_lat:g} from sidecar" if lat_known is not None
                 else f"best fit at inferred latitude {best_lat:g}")
        add("ET0 reproducible",
            "ok" if best_pct >= _ET0_REPRO_MIN_PCT else "fail",
            f"{best_pct:.1f}% of rows reproduce their stored et0_mm from their own "
            f"inputs via {best_method} ({where}, tolerance {_ET0_TOLERANCE_MM} mm). "
            f"Below {_ET0_REPRO_MIN_PCT:.0f}% means the file did not come from this pipeline.")

    order = {"fail": 2, "warn": 1, "ok": 0}
    worst = max((order.get(c["status"], 0) for c in out["checks"]), default=0)
    out["status"] = {2: "fail", 1: "warn", 0: "ok"}[worst]
    return out


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "weather_nasa_power_history.csv")
    rep = validate_weather_csv(target)
    print(f"{target}\nStatus: {rep['status'].upper()}")
    for c in rep["checks"]:
        print(f"  [{c['status']:>4}] {c['name']}: {c['detail']}")
