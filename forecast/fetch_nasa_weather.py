"""
NASA POWER daily-weather fetcher for a single estate location.

Produces the same 9-column schema as the existing forecast/weather_nasa_power_history.csv
(date, rainfall_mm, temp_mean_c, temp_max_c, temp_min_c, humidity_pct, solar_radiation,
et0_mm, water_balance_mm) so it drops straight into monthly_model.build_weather_monthly().

Location can be given explicitly (--lat/--lon) or derived from an EPMS block-overlay CSV
(--overlay forecast/<ESTATE>/<ESTATE>_overlay.csv, the same file forecast/blocks.py reads)
by averaging every polygon vertex across every block — NASA POWER's grid cell is coarse
(~0.5 deg) so any point inside the estate resolves to the same weather.

et0_mm is FAO-56 Penman-Monteith reference evapotranspiration, computed from net
radiation, vapour-pressure deficit and wind (all reported by POWER). Hargreaves-Samani
is kept only as a fallback for days missing WS2M/PS: POWER's ~50 km cell smooths the
diurnal range to ~2.9 degC at K3, which collapses the Hargreaves sqrt(Tmax-Tmin) term
and understates ET0 by roughly 40%. water_balance_mm = rainfall_mm - et0_mm.

NOTE: this does NOT reproduce the legacy forecast/weather_nasa_power_history.csv. That
file was produced by an earlier, uncommitted pipeline and does not describe K3: its
rainfall averages ~4,250 mm/yr against a ~2,167 mm/yr published climatology for Lahad
Datu, and correlates with POWER at these coordinates at r = -0.004 daily (no date shift
within +/-24 months explains it). Its stored et0_mm is not derivable from its own
temperature columns by any formula here. Treat this fetcher, not that file, as the
source of truth.

Usage:
  python fetch_nasa_weather.py --overlay forecast/EC/EC_overlay.csv \
      --start 2024-06-01 --end 2025-05-31 --out forecast/EC/weather_nasa_power_history.csv
  python fetch_nasa_weather.py --lat -7.05 --lon 140.84 --start 2024-06-01 --end 2025-05-31 \
      --out forecast/EC/weather_nasa_power_history.csv
"""

import argparse
import csv
import datetime
import json
import math
import urllib.request

NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
PARAMS = "T2M,T2M_MAX,T2M_MIN,RH2M,ALLSKY_SFC_SW_DWN,PRECTOTCORR,WS2M,PS"
FILL_VALUE = -999.0


def centroid_from_overlay(path: str) -> tuple[float, float]:
    """Average every [lon, lat] vertex across every block polygon in an EPMS
    overlay CSV (see forecast/blocks.py for the same file format)."""
    lons, lats = [], []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ring = json.loads(row["overlay_coordinates"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            for lon, lat, *_ in ring:
                lons.append(lon)
                lats.append(lat)
    if not lons:
        raise ValueError(f"No polygon coordinates found in {path}")
    return sum(lats) / len(lats), sum(lons) / len(lons)


def fetch_power(lat: float, lon: float, start: str, end: str) -> dict:
    """Raw daily NASA POWER records for the AG community, keyed by parameter."""
    url = (
        f"{NASA_POWER_URL}?parameters={PARAMS}&community=AG"
        f"&longitude={lon}&latitude={lat}"
        f"&start={start.replace('-', '')}&end={end.replace('-', '')}&format=JSON"
    )
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["properties"]["parameter"]


def extraterrestrial_radiation(lat_deg: float, day_of_year: int) -> float:
    """FAO-56 Ra (MJ/m^2/day) from latitude and day-of-year."""
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi / 365 * day_of_year)
    decl = 0.409 * math.sin(2 * math.pi / 365 * day_of_year - 1.39)
    x = -math.tan(phi) * math.tan(decl)
    omega_s = math.acos(min(1.0, max(-1.0, x)))
    gsc = 0.0820  # MJ m^-2 min^-1
    return (24 * 60 / math.pi) * gsc * dr * (
        omega_s * math.sin(phi) * math.sin(decl)
        + math.cos(phi) * math.cos(decl) * math.sin(omega_s)
    )


def hargreaves_et0(tmean: float, tmax: float, tmin: float, lat_deg: float, day_of_year: int) -> float:
    ra = extraterrestrial_radiation(lat_deg, day_of_year)
    ra_mm = ra * 0.408
    return 0.0023 * ra_mm * (tmean + 17.8) * math.sqrt(max(tmax - tmin, 0.0))


def _svp(t: float) -> float:
    """FAO-56 eq.11 saturation vapour pressure (kPa) at temperature t (degC)."""
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def elevation_from_pressure(p_kpa: float) -> float:
    """Invert FAO-56 eq.7 to get site elevation (m) from mean surface pressure."""
    return (293.0 / 0.0065) * (1.0 - (p_kpa / 101.3) ** (1.0 / 5.26))


def penman_monteith_et0(tmean, tmax, tmin, rh, rs, wind, pres, lat_deg, day_of_year):
    """FAO-56 eq.6 reference evapotranspiration (mm/day).

    Preferred over Hargreaves here because NASA POWER's ~50 km cell reports a
    heavily smoothed diurnal range (~2.9 degC at K3 against 8-10 degC at a real
    station), which collapses the Hargreaves sqrt(Tmax-Tmin) term and understates
    ET0 by roughly 40% in the humid tropics. Penman-Monteith instead drives ET0
    from net radiation, vapour-pressure deficit and wind, all of which POWER
    reports directly, so ALLSKY_SFC_SW_DWN finally carries real weight.
    """
    ra = extraterrestrial_radiation(lat_deg, day_of_year)
    z = elevation_from_pressure(pres)
    # Radiation balance
    rso = (0.75 + 2e-5 * z) * ra                       # clear-sky radiation
    rns = (1 - 0.23) * rs                              # net shortwave, albedo 0.23
    es = (_svp(tmax) + _svp(tmin)) / 2.0
    ea = es * max(min(rh, 100.0), 0.0) / 100.0         # FAO-56 eq.19 (RHmean only)
    # Cloudiness factor: FAO-56 bounds Rs/Rso to [0.25, 1.0]
    ratio = min(max(rs / rso, 0.25), 1.0) if rso > 0 else 1.0
    sigma = 4.903e-9                                   # MJ K^-4 m^-2 day^-1
    tmax_k, tmin_k = tmax + 273.16, tmin + 273.16
    rnl = (sigma * ((tmax_k ** 4 + tmin_k ** 4) / 2.0)
           * (0.34 - 0.14 * math.sqrt(max(ea, 0.0)))
           * (1.35 * ratio - 0.35))
    rn = rns - rnl
    # Aerodynamic + radiation terms
    delta = 4098 * _svp(tmean) / ((tmean + 237.3) ** 2)
    gamma = 0.000665 * pres
    u2 = max(wind, 0.5)                                # FAO-56 floor on wind speed
    num = 0.408 * delta * rn + gamma * (900.0 / (tmean + 273.0)) * u2 * max(es - ea, 0.0)
    den = delta + gamma * (1 + 0.34 * u2)
    return max(num / den, 0.0)


def build_rows(params: dict, lat: float) -> list[dict]:
    dates = sorted(params["T2M"])
    rows = []
    for d in dates:
        tmean = params["T2M"][d]
        tmax = params["T2M_MAX"][d]
        tmin = params["T2M_MIN"][d]
        rh = params["RH2M"][d]
        solar = params["ALLSKY_SFC_SW_DWN"][d]
        rain = params["PRECTOTCORR"][d]
        wind = params.get("WS2M", {}).get(d, FILL_VALUE)
        pres = params.get("PS", {}).get(d, FILL_VALUE)
        if FILL_VALUE in (tmean, tmax, tmin, rh, solar, rain):
            continue  # NASA POWER fill value for a missing observation
        day_of_year = datetime.date(int(d[:4]), int(d[4:6]), int(d[6:8])).timetuple().tm_yday
        if FILL_VALUE not in (wind, pres):
            et0 = penman_monteith_et0(tmean, tmax, tmin, rh, solar, wind, pres,
                                      lat, day_of_year)
        else:
            # Degraded fallback: understates ET0 on POWER's smoothed diurnal range.
            et0 = hargreaves_et0(tmean, tmax, tmin, lat, day_of_year)
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
            "rainfall_mm": round(rain, 2),
            "temp_mean_c": round(tmean, 2),
            "temp_max_c": round(tmax, 2),
            "temp_min_c": round(tmin, 2),
            "humidity_pct": round(rh, 2),
            "solar_radiation": round(solar, 2),
            "wind_2m_ms": round(wind, 2) if wind != FILL_VALUE else "",
            "pressure_kpa": round(pres, 2) if pres != FILL_VALUE else "",
            "et0_mm": round(et0, 2),
            "water_balance_mm": round(rain - et0, 2),
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", help="EPMS block-overlay CSV to derive lat/lon from")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.overlay:
        lat, lon = centroid_from_overlay(args.overlay)
    elif args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
    else:
        ap.error("pass either --overlay or both --lat and --lon")

    print(f"Fetching NASA POWER daily weather for ({lat:.4f}, {lon:.4f}), "
          f"{args.start}..{args.end}")
    params = fetch_power(lat, lon, args.start, args.end)
    rows = build_rows(params, lat)

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "date", "rainfall_mm", "temp_mean_c", "temp_max_c", "temp_min_c",
            "humidity_pct", "solar_radiation", "wind_2m_ms", "pressure_kpa",
            "et0_mm", "water_balance_mm",
        ])
        writer.writeheader()
        writer.writerows(rows)
    method = ("FAO-56 Penman-Monteith" if rows and rows[0].get("wind_2m_ms") != ""
              else "Hargreaves-Samani")
    import weather_quality as wq
    meta = {
        "source": "NASA POWER daily point (community=AG)",
        "url": NASA_POWER_URL, "parameters": PARAMS,
        "latitude": round(lat, 6), "longitude": round(lon, 6),
        "start": args.start, "end": args.end, "rows": len(rows),
        "et0_method": method,
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(wq.meta_path(args.out), "w", encoding="utf-8") as mh:
        json.dump(meta, mh, indent=2)
    print(f"Wrote {len(rows)} days -> {args.out}")
    print(f"Wrote provenance -> {wq.meta_path(args.out)}")
    report = wq.validate_weather_csv(args.out)
    print("Validation: " + report["status"].upper())
    for c in report["checks"]:
        print("  [%4s] %s: %s" % (c["status"], c["name"], c["detail"]))


if __name__ == "__main__":
    main()
