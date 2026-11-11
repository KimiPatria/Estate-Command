"""
Which model the forecast page serves on the staging database.

The staging estates (EA, EB, EC) have 17-22 months of history: too short for the
SARIMAX + LightGBM ensemble behind the live page, so the page serves one of the
lab's zero-shot arms instead. This script picks it from the walk-forward
backtests by a rule written down before any staging backtest was run.

RULE (fixed 2026-11-10 -- do not edit; add a new dated rule instead)
-------------------------------------------------------------------
Candidates  the lab's artifact models that have a monthly backtest on all three
            staging estates.
Yardstick   Naive last-3 mean. (Seasonal naive cannot be scored: no target has
            a full year before it until an estate passes 24 months.)
Rows        steps 1-3, scoreable months only, and for each estate only the
            (origin, step) pairs every candidate and the yardstick all have.
Score       per estate, the candidate's MAE divided by the yardstick's MAE on
            those rows; then the mean of the three ratios. A ratio is used,
            not pooled MAE, so EC and EB (three times EA's volume) do not
            decide it alone.
Decision    of the candidates whose ratio is below 1 on EVERY estate, the one
            with the lowest mean ratio. If none clears every estate, serve
            Naive last-3 mean and say so.

The page serves steps 1-6 only. With 9-16 origins per estate this is a noisy
choice, so the selection file carries every number behind it and the page shows
the origin count next to the accuracy it quotes.

Writes forecast/experiments/lab/staging_selection.json, which forecast_router
reads. Rerun after every rebuild of the staging artifacts:
    python forecast/build_staging_series.py
    <chronos env python> forecast/lab/build_artifacts.py --source staging [--remote]
    python forecast/lab/select_staging.py
"""

import importlib.util
import json
import os
import sys
from datetime import datetime

import pandas as pd

_LAB = os.path.dirname(os.path.abspath(__file__))

RULE_ID = "staging-default-2026-11-10"
STEPS = (1, 2, 3)
YARDSTICK = "naive_trailing3"
FALLBACK = "naive_trailing3"
GRAIN = "month"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"epms_lab_{name}", os.path.join(_LAB, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def selection_path(reg):
    return os.path.join(reg.ARTIFACT_DIR, "staging_selection.json")


def _rows(reg, model, estate):
    df, _why = reg.backtest(model, estate, GRAIN)
    if df is None or df.empty:
        return None
    df = df[df["scoreable"] & df["step"].isin(STEPS)]
    return df.set_index(["origin", "step"])


def decide(reg):
    """Apply the rule. Returns the selection dict (not yet written)."""
    estates = reg.estates_of("staging")
    artifact_models = [m["id"] for m in reg.MODELS if m["source"] == "artifact"]
    frames = {e: {m: _rows(reg, m, e) for m in artifact_models + [YARDSTICK]} for e in estates}

    missing = [e for e in estates if frames[e][YARDSTICK] is None]
    candidates = [m for m in artifact_models if all(frames[e][m] is not None for e in estates)]
    out = {
        "rule": RULE_ID, "decided_at": datetime.now().isoformat(timespec="seconds"),
        "grain": GRAIN, "steps": list(STEPS), "yardstick": YARDSTICK,
        "estates": estates, "candidates": candidates, "per_estate": {}, "scores": {},
    }
    if missing or not candidates:
        out.update({"selected": None,
                    "reason": ("no backtest for " + ", ".join(missing) if missing
                               else "no artifact model has a monthly backtest on every staging estate; "
                                    "run forecast/lab/build_artifacts.py --source staging")})
        return out

    ratios = {m: {} for m in candidates}
    for e in estates:
        common = frames[e][YARDSTICK].index
        for m in candidates:
            common = common.intersection(frames[e][m].index)
        base = frames[e][YARDSTICK].loc[common]
        base_mae = float((base["pred"] - base["actual"]).abs().mean())
        row = {"n_rows": int(len(common)), "n_origins": int(common.get_level_values(0).nunique()),
               "mean_actual": float(base["actual"].mean()),
               "mae": {YARDSTICK: round(base_mae)}, "ratio": {}}
        for m in candidates:
            d = frames[e][m].loc[common]
            mae = float((d["pred"] - d["actual"]).abs().mean())
            row["mae"][m] = round(mae)
            row["ratio"][m] = round(mae / base_mae, 4) if base_mae else None
            ratios[m][e] = mae / base_mae if base_mae else float("inf")
        out["per_estate"][e] = row

    for m in candidates:
        out["scores"][m] = {"mean_ratio": round(sum(ratios[m].values()) / len(estates), 4),
                            "beats_yardstick_everywhere": all(r < 1 for r in ratios[m].values())}
    passing = [m for m in candidates if out["scores"][m]["beats_yardstick_everywhere"]]
    if passing:
        best = min(passing, key=lambda m: out["scores"][m]["mean_ratio"])
        out.update({"selected": best,
                    "reason": f"lowest mean MAE ratio ({out['scores'][best]['mean_ratio']:.2f}) among the "
                              f"{len(passing)} of {len(candidates)} candidates that beat Naive last-3 on every estate"})
    else:
        out.update({"selected": FALLBACK,
                    "reason": "no candidate beat Naive last-3 on every staging estate, so the baseline is served"})
    return out


def read(reg):
    """The written selection, or None."""
    path = selection_path(reg)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    for stream in (sys.stdout, sys.stderr):      # model labels carry a middle dot; cp1252 consoles choke on it
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    reg = _load("registry")
    sel = decide(reg)
    label = {m["id"]: m["label"] for m in reg.MODELS}
    print(f"rule {sel['rule']} | steps {sel['steps']} | yardstick {label[YARDSTICK]}")
    for e, row in sel["per_estate"].items():
        print(f"  {reg.ESTATES[e]['label']}: {row['n_rows']} rows from {row['n_origins']} origins, "
              f"mean month {row['mean_actual']:,.0f}")
        for m, mae in row["mae"].items():
            r = row["ratio"].get(m)
            print(f"      {label[m]:<40} MAE {mae:>9,}" + (f"   ratio {r:.2f}" if r is not None else ""))
    for m, s in sel["scores"].items():
        print(f"  {label[m]:<40} mean ratio {s['mean_ratio']:.2f}   "
              f"{'beats' if s['beats_yardstick_everywhere'] else 'does NOT beat'} the yardstick everywhere")
    if sel["selected"] is None:
        print(f"NO SELECTION: {sel['reason']}")
        return 1
    with open(selection_path(reg), "w", encoding="utf-8") as fh:
        json.dump(sel, fh, indent=1)
    print(f"SELECTED: {label[sel['selected']]} -- {sel['reason']}")
    print(f"wrote {selection_path(reg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
