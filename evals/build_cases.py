"""
Build the labeled anomaly eval set (Stage-3 R&D) -> evals/anomaly_cases.jsonl.

Curated labels + reproducible forensics: the label table below encodes the
human-reviewable ground truth (cause taxonomy + rationale); all numbers
(actual, predicted, band, harvest days, weather) are pulled from the data at
build time so the cases stay consistent after retrains.

Cause taxonomy (the agent's final JSON must pick one of these):
  under_recording      — the month's low figure is a data-quality artifact
                         (few harvest days recorded; target seasonally imputed)
  weather_lagged       — moisture stress 5-7 months earlier (flowering window)
                         depressed production
  seasonal_model_bias  — the model systematically misses the seasonal peak /
                         post-peak transition
  level_shift          — sustained production level change the model is slow
                         to track (chronic one-sided errors)
  no_anomaly           — forecast and actual agree; nothing to explain
  unknown_external     — genuine anomaly, fully recorded, no weather/data
                         signal; honest answer is uncertainty

Labels were derived from walk-forward errors, recording flags and NASA POWER
water-balance forensics (see rationale per case) and are marked
human_verified=false until reviewed. Re-run after review edits at your peril —
regeneration overwrites the file (edit labels in _LABELS instead).
"""

import json
import os
import sys

import joblib
import pandas as pd

_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
_FC = os.path.join(_ROOT, "forecast")

# month -> (category, secondary-acceptable, one-line labeled cause, rationale)
_LABELS = {
    "2023-12": ("weather_lagged", [],
                "Lagged moisture stress: the May-Sep 2023 (El Nino) dry spell hit the "
                "flowering window 5-7 months before this month.",
                "Aug 2023 water balance -213mm, Sep -127mm; production fell 22% below "
                "forecast with 27 harvest days recorded (recording OK)."),
    "2024-01": ("under_recording", ["weather_lagged"],
                "Under-recording: only 16 harvest days were recorded, so the raw month "
                "is incomplete (target imputed).",
                "is_underrecorded=1, n_harvest_days=16; the +39% miss is dominated by "
                "the recording gap, with lagged 2023 drought as a secondary drag."),
    "2024-09": ("seasonal_model_bias", ["no_anomaly"],
                "Seasonal ramp underestimation: production climbs into the Sep-Oct "
                "peak faster than the AR-lag ensemble can track.",
                "-14.7% error while ramping to the Oct peak; inside the 90% band, "
                "same signed pattern as 2024-10."),
    "2024-10": ("seasonal_model_bias", [],
                "Seasonal peak underestimation: the ensemble undershot the Sep-Oct "
                "production peak (actual surged to ~153k).",
                "-15.5% breach on the high side; weather benign; recording complete "
                "(29 days). Trailing-3 and AR lags anchor the model too low at peaks."),
    "2024-11": ("seasonal_model_bias", [],
                "Post-peak transition overshoot: after chasing the October peak the "
                "model over-forecast the seasonal decline.",
                "+13.4% error immediately after the peak month; mirror image of the "
                "peak underestimation, recording complete."),
    "2025-02": ("weather_lagged", [],
                "Lagged moisture stress: the Jun-Jul 2024 dry spell (water balance "
                "-158mm in Jul) hit flowering 5-7 months earlier.",
                "+23.2% breach; 27 harvest days recorded (recording OK); dry-window "
                "arithmetic puts Jul 2024 exactly 7 months prior."),
    "2025-03": ("level_shift", ["weather_lagged"],
                "Early-2025 production slump: actuals settled ~10-15% below 2024 "
                "levels and the model was slow to re-anchor.",
                "+16.3% error follows the Feb breach; recording complete; part of a "
                "lower 2025 production regime."),
    "2025-06": ("level_shift", [],
                "Sustained level shift: 2025 production runs below 2024 and the "
                "ensemble over-forecasts for several consecutive months.",
                "+9.9% error, first of four consecutive over-forecasts Jun-Sep 2025."),
    "2025-09": ("level_shift", [],
                "Chronic over-forecasting: fourth consecutive one-sided miss as the "
                "model tracks a production level the estate no longer produces.",
                "Jun-Sep 2025 errors +9.9/+12.3/+10.6/+15.9%; recording complete "
                "throughout — signed-error run, not noise."),
    "2025-12": ("under_recording", [],
                "Under-recording: only 11 harvest days recorded; the served value is "
                "seasonally imputed.",
                "is_underrecorded=1, n_harvest_days=11; any apparent anomaly is a "
                "data-quality artifact."),
    "2026-01": ("weather_lagged", [],
                "Lagged moisture stress: the severe Jul 2025 dry month (67mm rain, "
                "water balance -175mm) hit flowering ~6 months earlier.",
                "+26% breach with 20 harvest days (not flagged); the sharpest dry "
                "signal in the record sits exactly in the 5-7 month lag window."),
    "2026-02": ("under_recording", ["weather_lagged"],
                "Under-recording: 13 harvest days recorded (imputed target), with the "
                "lagged 2025 drought as a secondary drag.",
                "is_underrecorded=1, n_harvest_days=13; +21.6% miss just inside the "
                "band; both mechanisms present, recording gap is primary."),
    "2026-04": ("under_recording", ["no_anomaly"],
                "Under-recording: only 12 harvest days recorded; the imputed target "
                "landed on the forecast, so no production anomaly is evident.",
                "is_underrecorded=1, n_harvest_days=12 yet error is -0.8% — the "
                "imputation absorbed the gap; flagging the data-quality caveat is "
                "the correct finding."),
    "2023-02": ("unknown_external", [],
                "Genuine collapse with complete recording and no weather signal; the "
                "cause is not recoverable from the available data.",
                "45,471 bunches (~60% below trend) with 25 harvest days; water "
                "balance mid-2022 (flowering window) was healthy. The honest verdict "
                "is an unknown external disruption flagged with low confidence."),
    # negatives — the correct verdict is "no anomaly, forecast on track"
    "2023-11": ("no_anomaly", [], "Forecast and actual agree (-2.2%).",
                "Error well inside the band; recording complete."),
    "2024-04": ("no_anomaly", [], "Forecast and actual agree (-2.4%).",
                "Error well inside the band; recording complete."),
    "2024-05": ("no_anomaly", [], "Forecast and actual agree (+3.0%).",
                "Error well inside the band; recording complete."),
    "2024-06": ("no_anomaly", [], "Forecast and actual agree (-3.0%).",
                "Error well inside the band; recording complete."),
    "2025-05": ("no_anomaly", [], "Forecast and actual agree (+3.0%).",
                "Error well inside the band; recording complete."),
    "2025-10": ("no_anomaly", [], "Forecast and actual agree (-5.3%).",
                "Error inside the band; recording complete."),
}

CATEGORIES = ["under_recording", "weather_lagged", "seasonal_model_bias",
              "level_shift", "no_anomaly", "unknown_external"]


def build():
    est = pd.read_csv(os.path.join(_FC, "features_estate_monthly.csv")).set_index("month")
    s1 = (pd.read_csv(os.path.join(_FC, "predictions_multih.csv"))
          .query("step == 1").set_index("month"))
    w = pd.read_csv(os.path.join(_FC, "weather_nasa_power_history.csv"), parse_dates=["date"])
    w["month"] = w["date"].dt.to_period("M").astype(str)
    wm = w.groupby("month").agg(rain_mm=("rainfall_mm", "sum"),
                                wb_mm=("water_balance_mm", "sum")).round(0)
    width1 = float(joblib.load(os.path.join(_FC, "monthly_model.joblib"))
                   ["conformal"]["served_widths"][1])

    cases = []
    for month, (cat, secondary, cause, rationale) in sorted(_LABELS.items()):
        e = est.loc[month] if month in est.index else None
        case = {
            "id": f"anom_{month.replace('-', '')}",
            "month": month,
            "label": {"category": cat, "acceptable_secondary": secondary,
                      "cause": cause, "rationale": rationale},
            "actual": round(float(e["bunches_total"])) if e is not None else None,
            "n_harvest_days": int(e["n_harvest_days"]) if e is not None else None,
            "is_underrecorded": bool(e["is_underrecorded"]) if e is not None else None,
            "rain_mm": float(wm.loc[month, "rain_mm"]) if month in wm.index else None,
            "water_balance_mm": float(wm.loc[month, "wb_mm"]) if month in wm.index else None,
            "human_verified": False,
            "source": "auto-forensics (build_cases.py) — review before trusting",
        }
        if month in s1.index:
            r = s1.loc[month]
            err = float(r["pred"]) - float(r["actual"])
            case.update(predicted=round(float(r["pred"])),
                        error=round(err),
                        error_pct=round(err / float(r["actual"]) * 100, 1),
                        band_lower=round(float(r["pred"]) - width1),
                        band_upper=round(float(r["pred"]) + width1),
                        band_breach=bool(abs(err) > width1))
        else:
            case.update(predicted=None, error=None, error_pct=None,
                        band_lower=None, band_upper=None, band_breach=None,
                        note="pre-backtest month; judge from history alone")
        cases.append(case)

    out = os.path.join(_DIR, "anomaly_cases.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    kinds = pd.Series([c["label"]["category"] for c in cases]).value_counts()
    print(f"Wrote {len(cases)} cases -> {out}")
    print(kinds.to_string())
    return cases


if __name__ == "__main__":
    build()
