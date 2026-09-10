"""
Forecast Intelligence — LLM risk-synthesis layer for the production forecast.

This module is a *read-only narrative layer* over the ML forecast. It pulls
real-world signals (14-day weather outlook, ENSO status, fire hotspots) and asks
an LLM (openai/gpt-oss-120b via Groq) to synthesise them with the existing
ML numbers into a risk level + narrative.

HARD CONSTRAINT — the LLM never alters the forecast numbers:
  * It receives the ML forecast as read-only context.
  * Its JSON output contains only risk_level / risk_summary / narrative.
  * The signal values shown to the user are the *server-fetched ground truth*
    (the ``signals`` block below), never the LLM's echo — so nothing the model
    says can change a displayed figure.

Served via GET /forecast/intelligence (see forecast_router.py), TTL-cached 6h.
Every external call degrades gracefully: a dead network or missing API key
yields "N/A" signals and the analysis still runs.
"""

import asyncio
import csv
import html
import io
import json
import logging
import math
import os
import re
import time
from datetime import datetime

import httpx

import llm_client
from config import FIRMS_MAP_KEY
from prompts import load_prompt

log = logging.getLogger("epms-forecast-intel")

_FORECAST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forecast")

# Per-estate directories holding the weather CSV and its provenance sidecar.
# Mirrors forecast_router.ESTATE_DIRS, kept local so this module has no import
# cycle back into the router.
_ESTATE_DIRS = {
    "k3": _FORECAST_DIR,
    "ec": os.path.join(_FORECAST_DIR, "EC"),
}
DEFAULT_ESTATE = "k3"

# Fallback coordinates for an estate whose weather provenance sidecar is missing.
# K3: Sabah, Malaysia (K3 OPH block 5°02'27.2"N 118°38'07.5"E, near Lahad Datu).
# EC: Papua, Indonesia (centroid of EC_overlay.csv).
_FALLBACK_GEO = {
    "k3": (5.0409, 118.6354, "Sabah (K3 estate)"),
    "ec": (-7.0236, 140.8695, "Papua (EC estate)"),
}

# Half-width of the FIRMS query box around the estate centroid, in degrees.
# 2.0° reproduces the previous hand-tuned Sabah box (116.6,3.0,120.6,7.0) and is
# roughly the radius over which a fire front's haze is worth reporting.
_FIRMS_BOX_DEG = 2.0

_HTTP_TIMEOUT = 12.0  # seconds per external request

# 6-hour in-process TTL cache, keyed by estate. Previously a single global dict,
# which meant selecting EC served whichever estate happened to warm the cache.
_INTEL_CACHE: dict = {}
_TTL_SECONDS = 6 * 3600

# Last-known-good ENSO reading, so a CPC outage degrades to a stale value with an
# explicit age rather than a bare "N/A".
_ENSO_LAST_GOOD: dict = {}


def estate_geo(estate: str = DEFAULT_ESTATE) -> dict:
    """Coordinates + FIRMS bounding box for one estate.

    Reads the latitude/longitude straight out of the weather provenance sidecar
    written by forecast/fetch_nasa_weather.py, so the point the intelligence tier
    queries is by construction the same point the model's weather came from.
    Falls back to a hardcoded coordinate if the sidecar is absent.
    """
    estate = (estate or DEFAULT_ESTATE).lower()
    lat, lon, label = _FALLBACK_GEO.get(estate, _FALLBACK_GEO[DEFAULT_ESTATE])
    source = "fallback constant"
    meta_file = os.path.join(_ESTATE_DIRS.get(estate, _FORECAST_DIR),
                             "weather_nasa_power_history.csv.meta.json")
    try:
        with open(meta_file, encoding="utf-8") as fh:
            meta = json.load(fh)
        if meta.get("latitude") is not None and meta.get("longitude") is not None:
            lat, lon = float(meta["latitude"]), float(meta["longitude"])
            source = "weather provenance sidecar"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    d = _FIRMS_BOX_DEG
    return {
        "estate": estate,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "label": label,
        "coord_source": source,
        # FIRMS wants west,south,east,north
        "bbox": f"{lon - d:.4f},{lat - d:.4f},{lon + d:.4f},{lat + d:.4f}",
    }


# ── signal fetchers (each returns a dict; never raises) ──────────────────────

async def fetch_weather_signal(client: httpx.AsyncClient, geo: dict | None = None) -> dict:
    """14-day rainfall + max-temp outlook for the estate point (Open-Meteo, free)."""
    geo = geo or estate_geo()
    try:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": geo["lat"],
                "longitude": geo["lon"],
                "daily": "precipitation_sum,temperature_2m_max",
                "forecast_days": 14,
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        daily = r.json().get("daily", {})
        rain = [x for x in (daily.get("precipitation_sum") or []) if x is not None]
        tmax = [x for x in (daily.get("temperature_2m_max") or []) if x is not None]
        if not rain:
            return {"available": False}
        return {
            "available": True,
            "total_rain_mm": round(sum(rain), 1),
            "dry_days": sum(1 for x in rain if x < 1.0),  # <1mm ≈ effectively dry
            "max_temp_c": round(max(tmax), 1) if tmax else None,
            "period_days": len(rain),
            "area": geo["label"],
            "lat": geo["lat"],
            "lon": geo["lon"],
        }
    except Exception as exc:
        log.warning("[intel] weather signal failed: %s", exc)
        return {"available": False}


_FIRMS_WINDOW_DAYS = 5


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r_lat, r_lon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(r_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(r_lon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


async def fetch_firms_signal(client: httpx.AsyncClient, geo: dict | None = None) -> dict:
    """Fire activity around the estate over the last 5 days (NASA FIRMS VIIRS NRT).

    Reports Fire Radiative Power, not just a detection count. FRP is a physical
    measure of burn intensity in megawatts and is already a column in the CSV we
    fetch — the previous implementation line-counted the response and discarded
    every field, so ten smouldering detections and one large fire front scored
    identically. Low-confidence detections are dropped (VIIRS grades each pixel
    l/n/h) because they are the bulk of the false positives.

    Requires a free FIRMS_MAP_KEY. Without one, returns {available: False} so the
    LLM simply omits any haze/labour-disruption reasoning. Day range is capped
    at 5 by this key/source (NASA returns 400 "Invalid day range" above that).
    """
    if not FIRMS_MAP_KEY:
        return {"available": False, "reason": "no FIRMS_MAP_KEY"}
    geo = geo or estate_geo()
    try:
        url = (
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
            f"{FIRMS_MAP_KEY}/VIIRS_SNPP_NRT/{geo['bbox']}/{_FIRMS_WINDOW_DAYS}"
        )
        r = await client.get(url)
        r.raise_for_status()
        text = r.text.strip()
        # CSV: a header line then one row per detection. Guard against HTML/error bodies.
        if not text or "," not in text.splitlines()[0]:
            return {"available": False}

        rows = list(csv.DictReader(io.StringIO(text)))
        kept, frps, dists = 0, [], []
        for row in rows:
            if str(row.get("confidence", "")).strip().lower() in ("l", "low"):
                continue
            kept += 1
            try:
                frps.append(float(row["frp"]))
            except (KeyError, TypeError, ValueError):
                pass
            try:
                dists.append(_haversine_km(geo["lat"], geo["lon"],
                                           float(row["latitude"]), float(row["longitude"])))
            except (KeyError, TypeError, ValueError):
                pass

        return {
            "available": True,
            "hotspot_count": kept,
            "raw_detections": len(rows),
            "low_confidence_dropped": len(rows) - kept,
            "total_frp_mw": round(sum(frps), 1) if frps else 0.0,
            "max_frp_mw": round(max(frps), 1) if frps else 0.0,
            "nearest_km": round(min(dists), 1) if dists else None,
            "area": geo["label"],
            "radius_deg": _FIRMS_BOX_DEG,
            "window_days": _FIRMS_WINDOW_DAYS,
        }
    except Exception as exc:
        log.warning("[intel] FIRMS signal failed: %s", exc)
        return {"available": False}


_RONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt"
_ENSO_DISC_URL = ("https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/"
                  "enso_advisory/ensodisc.shtml")


async def _fetch_roni(client: httpx.AsyncClient) -> dict:
    """Latest Relative Oceanic Niño Index from CPC's structured monthly series.

    CPC made RONI the official ENSO index on 2026-02-01, replacing ONI. RONI
    measures the Niño-3.4 anomaly relative to the wider tropics, so it does not
    inherit the long-term tropical warming trend the way ONI does; in practice it
    reads cooler than ONI during recent El Niños (e.g. MJJ 2026: RONI +0.98 vs
    ONI +1.39). Thresholds are the familiar ±0.5 °C.

    Parsed from a fixed 3-column ASCII table (SEAS YR ANOM) rather than scraped
    out of HTML, so it survives a page restyle.
    """
    r = await client.get(_RONI_URL)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0].upper() == "SEAS":
            continue
        try:
            rows.append((parts[0].upper(), int(parts[1]), float(parts[2])))
        except ValueError:
            continue
    if not rows:
        raise ValueError("RONI series empty or unparseable")
    season, year, value = rows[-1]
    prev = rows[-2][2] if len(rows) > 1 else None
    if value >= 0.5:
        phase = "El Niño"
    elif value <= -0.5:
        phase = "La Niña"
    else:
        phase = "Neutral"
    trend = None
    if prev is not None:
        delta = value - prev
        trend = "strengthening" if abs(value) > abs(prev) else "weakening"
        if abs(delta) < 0.05:
            trend = "steady"
    return {
        "roni": round(value, 2),
        "roni_season": f"{season} {year}",
        "roni_phase": phase,
        "roni_trend": trend,
        "roni_prev": round(prev, 2) if prev is not None else None,
    }


async def fetch_enso_signal(client: httpx.AsyncClient) -> dict:
    """Current ENSO state from NOAA CPC: the official alert label plus the
    numeric RONI value behind it.

    The alert label is still scraped from the ENSO discussion (there is no
    structured feed for it), but it is no longer the only thing reported — a
    categorical "El Niño Advisory" says nothing about whether the event is
    strengthening or nearly over. Both halves degrade independently, and a
    last-known-good value with an explicit age is served if CPC is unreachable.
    """
    out: dict = {"available": False, "source": "NOAA CPC"}

    # ── numeric index (structured, robust) ───────────────────────────────────
    try:
        out.update(await _fetch_roni(client))
        out["available"] = True
        out["index"] = "RONI"
    except Exception as exc:
        log.warning("[intel] RONI fetch failed: %s", exc)

    # ── official alert label (HTML scrape, best-effort) ──────────────────────
    try:
        r = await client.get(_ENSO_DISC_URL)
        r.raise_for_status()
        m = re.search(
            r"ENSO Alert System Status:.*?<span[^>]*>(.*?)</span>",
            r.text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            # Strip residual tags, unescape entities (&ntilde; → ñ), collapse spaces.
            status = re.sub(r"<[^>]+>", "", m.group(1))
            status = re.sub(r"\s+", " ", html.unescape(status)).strip().rstrip(".")
            if status:
                out["status"] = status
                out["available"] = True
    except Exception as exc:
        log.warning("[intel] ENSO alert-label scrape failed: %s", exc)

    if not out.get("status") and out.get("roni_phase"):
        # Label unavailable — describe the phase from the index instead of "N/A".
        out["status"] = f"{out['roni_phase']} (from RONI, alert label unavailable)"

    if out["available"]:
        out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        _ENSO_LAST_GOOD.clear()
        _ENSO_LAST_GOOD.update(out)
        return out

    if _ENSO_LAST_GOOD:
        stale = dict(_ENSO_LAST_GOOD)
        stale["stale"] = True
        log.warning("[intel] ENSO unavailable; serving last-known-good from %s",
                    stale.get("fetched_at"))
        return stale
    return {"available": False, "source": "NOAA CPC"}


# ── prompt construction ──────────────────────────────────────────────────────

_RISK_SYSTEM_PROMPT = load_prompt("forecast_risk_system")


def _fmt_signal(label: str, sig: dict, render) -> str:
    if not sig.get("available"):
        return f"- {label}: N/A (unavailable)"
    return f"- {label}: {render(sig)}"


def _build_user_prompt(forecast: dict, weather: dict, firms: dict, enso: dict) -> str:
    fc = forecast.get("forecast") or []
    nxt = fc[0] if fc else {}
    lines = ["ML PRODUCTION FORECAST (authoritative, do not alter):"]
    # Quote the 3-month figure, not the 1-step one: the narrative describes a
    # 3-month forecast, and `smape` alone is the 1-step-ahead number (~10.1%),
    # which understates the error on the months being discussed.
    _mh = forecast.get("accuracy_multih") or {}
    _per_h = ", ".join(f"h{h['step']} {h['smape']}%" for h in (_mh.get("per_horizon") or []))
    lines.append(
        f"- Model: {forecast.get('model_label', 'ensemble')} "
        f"(backtested sMAPE {forecast.get('headline_smape', forecast.get('smape', '?'))}% "
        f"pooled over the 3-month horizon"
        + (f" [{_per_h}]" if _per_h else "")
        + f"; 1-month-ahead only {forecast.get('smape_1step', forecast.get('smape', '?'))}%"
        f", MASE {forecast.get('mase', '?')})"
    )
    if nxt:
        lines.append(
            f"- Next month ({nxt.get('month')}): ensemble {nxt.get('ensemble'):,} bunches "
            f"(range {nxt.get('lower'):,} – {nxt.get('upper'):,})"
        )
    for f in fc[1:]:
        lines.append(
            f"- {f.get('month')}: ensemble {f.get('ensemble'):,} bunches "
            f"(range {f.get('lower'):,} – {f.get('upper'):,})"
        )
    lines.append(f"- 3-month total (ensemble): {forecast.get('total_3m', 0):,} bunches")

    lines.append("\nCURRENT REAL-WORLD SIGNALS:")
    lines.append(_fmt_signal(
        f"14-day weather outlook ({weather.get('area', 'estate point')})", weather,
        lambda s: f"{s['total_rain_mm']} mm total rain over {s['period_days']} days, "
                  f"{s['dry_days']} effectively-dry days, max temp {s.get('max_temp_c')}°C",
    ))

    def _enso_txt(s):
        bits = [s.get("status") or s.get("roni_phase") or "unknown"]
        if s.get("roni") is not None:
            bits.append(f"RONI {s['roni']:+.2f} °C for {s.get('roni_season')} "
                        f"({s.get('roni_phase')}, {s.get('roni_trend') or 'trend n/a'}; "
                        f"El Niño ≥ +0.5, La Niña ≤ −0.5)")
        if s.get("stale"):
            bits.append(f"STALE — CPC unreachable, last good {s.get('fetched_at')}")
        return " · ".join(bits) + f" ({s.get('source', 'NOAA CPC')})"

    lines.append(_fmt_signal("ENSO status", enso, _enso_txt))

    def _firms_txt(s):
        txt = (f"{s['hotspot_count']} VIIRS detections within ~{s.get('radius_deg')}° of "
               f"{s.get('area')} in the last {s.get('window_days')}d")
        if s.get("total_frp_mw"):
            txt += (f", total fire radiative power {s['total_frp_mw']} MW "
                    f"(largest single {s.get('max_frp_mw')} MW)")
        if s.get("nearest_km") is not None:
            txt += f", nearest {s['nearest_km']} km from the estate"
        return txt

    lines.append(_fmt_signal("Fire activity", firms, _firms_txt))
    lines.append(
        "\nAssess near-term production risk and respond with the JSON object only."
    )
    return "\n".join(lines)


# ── LLM call (blocking; run via asyncio.to_thread) ───────────────────────────

def _call_llm_json(messages: list[dict]) -> tuple[dict, str]:
    result = llm_client.chat(
        messages,
        tier="main",
        temperature=0.3,
        max_tokens=700,
        json_mode=True,
        task="forecast_intel",
    )
    return json.loads(result.text), result.model_id


def _normalise(parsed: dict) -> dict:
    level = str(parsed.get("risk_level", "")).strip().lower()
    if level not in ("low", "moderate", "high"):
        level = "moderate"
    return {
        "risk_level": level,
        "risk_summary": str(parsed.get("risk_summary", "")).strip(),
        "narrative": str(parsed.get("narrative", "")).strip(),
    }


# ── public entrypoint ────────────────────────────────────────────────────────

async def analyze_risk(forecast: dict, estate: str = DEFAULT_ESTATE) -> dict:
    """Fetch live signals and synthesise a risk assessment over the ML forecast.

    ``forecast`` is the read-only dict returned by forecast_router.get_forecast().
    Returns a dict with risk_level / risk_summary / narrative plus the
    server-fetched ground-truth ``signals`` (never the LLM's echo).
    """
    geo = estate_geo(estate)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        weather, firms, enso = await asyncio.gather(
            fetch_weather_signal(client, geo),
            fetch_firms_signal(client, geo),
            fetch_enso_signal(client),
        )

    messages = [
        {"role": "system", "content": _RISK_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(forecast, weather, firms, enso)},
    ]
    parsed, model_id = await asyncio.to_thread(_call_llm_json, messages)
    result = _normalise(parsed)

    # Ground-truth signals for display — independent of anything the LLM said.
    result["signals"] = {"weather": weather, "enso": enso, "firms": firms}
    result["estate"] = geo["estate"]
    result["geo"] = geo
    result["model"] = model_id
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return result


async def get_intelligence(forecast_loader, refresh: bool = False,
                           estate: str = DEFAULT_ESTATE) -> dict:
    """TTL-cached wrapper. ``forecast_loader`` is a zero-arg callable returning the
    ML forecast dict (kept as a callable so the fit only runs on a cache miss).

    The cache is keyed by estate — it used to be a single global slot, so asking
    for EC could return whichever estate had most recently warmed it.
    """
    estate = (estate or DEFAULT_ESTATE).lower()
    now = time.time()
    slot = _INTEL_CACHE.get(estate)
    if not refresh and slot and (now - (slot.get("ts") or 0)) < _TTL_SECONDS:
        return slot["data"]
    forecast = await asyncio.to_thread(forecast_loader)
    result = await analyze_risk(forecast, estate=estate)
    _INTEL_CACHE[estate] = {"data": result, "ts": now}
    return result
