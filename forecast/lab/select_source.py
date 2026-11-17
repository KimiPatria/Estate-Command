"""
Which Chronos-2 arm the forecast page serves, per source (live or staging).

The page serves Chronos-2 zero-shot on both sources (user decision 2026-11-12,
buildplan_forecast_mlops.md D1). This picks the arm, by a rule written down
before it was run. K3 is not decided here: it is a recorded manual promotion
(promotion.py, MANUAL_PROMOTIONS).

RULE chronos-ctx12-2026-11-12 (fixed -- do not edit; add a new dated rule instead)
---------------------------------------------------------------------------------
Estates     the source's estates that can be forecast (>= 3 complete months),
            except K3.
Served      Chronos-2 arms only: estate weekly, monthly, monthly + weather and
candidates  block sum -- each needs a monthly backtest on every one of those
            estates (monthly + weather drops out where an estate has no weather).
Rows        steps 1-3, scoreable months, from origins with >= 12 complete months
            of context. An estate with fewer than 3 such origins uses all its
            backtest origins (>= 6 months) and is marked low-evidence. Per estate,
            only the (origin, step) rows every candidate and the yardstick share.
Score       per estate, MAE / MAE of Naive last-3 on those rows; then the mean of
            the per-estate ratios (so a large estate does not decide alone).
Decision    the candidate with the lowest mean ratio is served. Where it does not
            beat the yardstick on an estate it is STILL served and that estate is
            flagged (D10) -- the old rule served the naive baseline instead.
Challengers seasonal_profile and combo_weekly_profile are scored on the same rows
            and reported under "challengers". They are never served.

RULE chronos-ctx12-evidence-2026-11-12 (in force; supersedes the rule above)
---------------------------------------------------------------------------
Identical, except that low-evidence estates (fewer than 3 origins with >= 12
months of context) are left out of the SCORE. They are still served the chosen
arm, and still flagged.
2026-11-13: chronos2_weekly_seasadj is added to the challengers it REPORTS. What it
scores and serves is unchanged (a challenger never enters the decision).

Why, and when: the first rule was run once, on 2026-11-12, before anything was
served. On live it chose the block sum -- the worst arm on EC, the only live
estate with evidence (6 origins) -- because live EB's 3 rows from 2 origins
weighed as much as EC's 15 in the mean ratio. Two origins cannot tell four arms
apart. Staging's choice is the same under both rules (every staging estate has
>= 5 mature origins). This rule was written after seeing that result, which is
why it is a new dated rule and not an edit. The first stays runnable with
--rule chronos-ctx12-2026-11-12.

RULE chronos-seasadj-h6-2026-11-13 (written, NOT in force -- switching is the user's call)
-----------------------------------------------------------------------------------
As chronos-ctx12-evidence-2026-11-12, with two changes:
  * Served candidates add chronos2_weekly_seasadj (estate weekly with the season
    divided out before Chronos-2 and multiplied back after; plain estate weekly
    until the estate has seen all 12 months of the year).
  * Rows are steps 1-6, not 1-3. The page draws 6 and more steps, and the served
    arm's error grows from 12% at step 1 to 49% at step 6 on staging (origins with
    a year of context) -- a rule that scores steps 1-3 cannot see that.
Why, and when: forecast/experiments/seasonal_chronos_2026-11-13/ measured the
adjusted arm at 13.8% sMAPE steps 1-3 vs 18.6%, flat 11-17% through step 6, on 20
staging estate-origins (5 each for EA and EB). It is worse at step 1 (15% vs 12%)
and wins mainly at seasonal turns. Written after that experiment, so it is a new
dated rule. Under the rule in force the arm is reported as a challenger.
Turn it on: set RULE_ID below, or run with --rule chronos-seasadj-h6-2026-11-13.

Writes forecast/experiments/lab/<source>_selection.json. The previous staging
rule (select_staging.py, staging-default-2026-11-10) is left as it was and still
runs; whichever ran last is the file in force, and the file names its rule.

    python forecast/lab/select_source.py --source staging
    python forecast/lab/select_source.py --source live
    python forecast/lab/select_source.py --source live --rule chronos-ctx12-2026-11-12
"""

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime

import pandas as pd

_LAB = os.path.dirname(os.path.abspath(__file__))

SERVED_CANDIDATES = ("chronos2_estate_weekly", "chronos2_monthly", "chronos2_monthly_cov",
                     "chronos2_blocks")
SEASADJ = "chronos2_weekly_seasadj"
CHALLENGERS = ("seasonal_profile", "combo_weekly_profile")
# rule id -> what it scores. Fixed once written: a change is a new dated rule.
RULES = {
    "chronos-ctx12-2026-11-12": {
        "score_low_evidence": True, "steps": (1, 2, 3),
        "candidates": SERVED_CANDIDATES, "challengers": CHALLENGERS},
    "chronos-ctx12-evidence-2026-11-12": {
        "score_low_evidence": False, "steps": (1, 2, 3),
        "candidates": SERVED_CANDIDATES, "challengers": CHALLENGERS + (SEASADJ,)},
    "chronos-seasadj-h6-2026-11-13": {
        "score_low_evidence": False, "steps": (1, 2, 3, 4, 5, 6),
        "candidates": SERVED_CANDIDATES + (SEASADJ,), "challengers": CHALLENGERS},
}
RULE_ID = "chronos-ctx12-evidence-2026-11-12"       # in force
STEPS = RULES[RULE_ID]["steps"]
GRAIN = "month"
YARDSTICK = "naive_trailing3"
MIN_CONTEXT = 12          # months of context an origin needs to count as the current regime
MIN_ORIGINS = 3           # below this, an estate falls back to every backtest origin
EXCLUDED = ("k3",)        # decided by promotion.MANUAL_PROMOTIONS
SOURCES = ("live", "staging")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"epms_lab_{name}", os.path.join(_LAB, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def selection_path(reg, source):
    return os.path.join(reg.ARTIFACT_DIR, f"{source}_selection.json")


def read(reg, source):
    """The selection file in force for a source, or None. Works for files from either
    rule: `served` (this rule) or `selected` (staging-default-2026-11-10)."""
    path = selection_path(reg, source)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        sel = json.load(fh)
    sel.setdefault("served", sel.get("selected"))
    return sel


def rule_estates(reg, source):
    out = []
    for e in reg.estates_of(source):
        if e in EXCLUDED:
            continue
        ok, _reason, _n = reg.estate_grain_status(e, GRAIN)
        if ok:
            out.append(e)
    return out


def _context_months(reg, estate):
    """origin period string -> complete months of context at that origin."""
    h = reg.month_history(estate)
    n = h["context_ok"].cumsum().to_numpy()
    return {reg.period_str(p, GRAIN): int(k) for p, k in zip(h["period"], n)}


def _rows(reg, model, estate, steps=STEPS):
    df, _why = reg.backtest(model, estate, GRAIN)
    if df is None or df.empty:
        return None
    df = df[df["scoreable"] & df["step"].isin(steps)]
    return df.set_index(["origin", "step"])[["actual", "pred"]].astype(float)


def _mae(df):
    return float((df["pred"] - df["actual"]).abs().mean())


def decide(reg, source, rule=RULE_ID):
    spec = RULES[rule]
    score_low_evidence, steps = spec["score_low_evidence"], spec["steps"]
    served_candidates, challengers = spec["candidates"], spec["challengers"]
    estates = rule_estates(reg, source)
    out = {"rule": rule, "decided_at": datetime.now().isoformat(timespec="seconds"),
           "source": source, "grain": GRAIN, "steps": list(steps), "yardstick": YARDSTICK,
           "min_context_months": MIN_CONTEXT, "estates": estates, "excluded": list(EXCLUDED),
           "per_estate": {}, "scores": {}, "challengers": {}, "flags": [], "served": None}
    if not estates:
        out["reason"] = f"no {source} estate has the 3 complete months a forecast needs"
        out["selected"] = None
        return out
    models = list(served_candidates) + list(challengers) + [YARDSTICK]
    frames = {e: {m: _rows(reg, m, e, steps) for m in models} for e in estates}
    candidates = [m for m in served_candidates if all(frames[e][m] is not None for e in estates)]
    out["candidates"] = candidates
    missing = [e for e in estates if frames[e][YARDSTICK] is None]
    if missing or not candidates:
        out["reason"] = ("no yardstick backtest for " + ", ".join(missing) if missing else
                         "no Chronos-2 arm has a monthly backtest on every estate; run "
                         f"forecast/lab/build_artifacts.py --source {source}")
        out["selected"] = None
        return out

    ratios = {m: {} for m in candidates}
    ch_ratios = {m: {} for m in challengers}
    for e in estates:
        ctx = _context_months(reg, e)
        common = frames[e][YARDSTICK].index
        for m in candidates:
            common = common.intersection(frames[e][m].index)
        mature = [o for o in common.get_level_values("origin").unique() if ctx.get(o, 0) >= MIN_CONTEXT]
        low_evidence = len(mature) < MIN_ORIGINS
        if not low_evidence:
            common = common[common.get_level_values("origin").isin(mature)]
        base = frames[e][YARDSTICK].loc[common]
        base_mae = _mae(base)
        row = {"n_rows": int(len(common)), "n_origins": int(common.get_level_values(0).nunique()),
               "origins": sorted(common.get_level_values(0).unique().tolist()),
               "context_rule": (f">= {MIN_CONTEXT} months" if not low_evidence else
                                f"all backtest origins (only {len(mature)} with >= {MIN_CONTEXT} months)"),
               "low_evidence": low_evidence, "mean_actual": round(float(base["actual"].mean())),
               "mae": {YARDSTICK: round(base_mae)}, "ratio": {}}
        for m in candidates:
            mae = _mae(frames[e][m].loc[common])
            row["mae"][m] = round(mae)
            row["ratio"][m] = round(mae / base_mae, 4) if base_mae else None
            ratios[m][e] = mae / base_mae if base_mae else float("inf")
        for m in challengers:
            f = frames[e][m]
            if f is None:
                continue
            idx = common.intersection(f.index)
            if len(idx) != len(common):        # scored only where it has every row
                continue
            mae = _mae(f.loc[idx])
            row["mae"][m] = round(mae)
            row["ratio"][m] = round(mae / base_mae, 4) if base_mae else None
            ch_ratios[m][e] = mae / base_mae if base_mae else float("inf")
        out["per_estate"][e] = row
        if low_evidence:
            out["flags"].append({"estate": e, "kind": "low_evidence",
                                 "detail": row["context_rule"]})

    scored = [e for e in estates if score_low_evidence or not out["per_estate"][e]["low_evidence"]]
    if not scored:                       # every estate is low-evidence: score them all anyway
        scored = list(estates)
    out["scored_estates"] = scored
    for m in candidates:
        out["scores"][m] = {"mean_ratio": round(sum(ratios[m][e] for e in scored) / len(scored), 4),
                            "beats_yardstick_everywhere": all(ratios[m][e] < 1 for e in estates)}
    for m in challengers:
        if len(ch_ratios[m]) == len(estates):
            out["challengers"][m] = {
                "mean_ratio": round(sum(ch_ratios[m][e] for e in scored) / len(scored), 4),
                "beats_yardstick_everywhere": all(r < 1 for r in ch_ratios[m].values())}
    best = min(candidates, key=lambda m: out["scores"][m]["mean_ratio"])
    for e in estates:
        if ratios[best][e] >= 1:
            out["flags"].append({"estate": e, "kind": "loses_to_yardstick",
                                 "detail": f"MAE ratio {ratios[best][e]:.2f} against Naive last-3"})
    out["served"] = out["selected"] = best
    out["reason"] = (f"lowest mean MAE ratio ({out['scores'][best]['mean_ratio']:.2f}, scored on "
                     f"{', '.join(reg.ESTATES[e]['label'] for e in scored)}) among the "
                     f"{len(candidates)} Chronos-2 arms with a backtest on every {source} estate")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=SOURCES, required=True)
    ap.add_argument("--rule", choices=list(RULES), default=RULE_ID)
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):      # model labels carry a middle dot
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    reg = _load("registry")
    sel = decide(reg, args.source, args.rule)
    label = {m["id"]: m["label"] for m in reg.MODELS}
    print(f"rule {sel['rule']} | source {args.source} | steps {sel['steps']} | yardstick {label[YARDSTICK]}")
    for e, row in sel["per_estate"].items():
        print(f"  {reg.ESTATES[e]['label']}: {row['n_rows']} rows from {row['n_origins']} origins "
              f"({row['context_rule']})")
        for m, r in row["ratio"].items():
            tag = "  [challenger]" if m in RULES[sel['rule']]["challengers"] else ""
            print(f"      {label[m]:<40} MAE {row['mae'][m]:>9,}   ratio {r:.2f}{tag}")
    if not sel["served"]:
        print(f"NO SELECTION: {sel['reason']}")
        return 1
    for f in sel["flags"]:
        print(f"  FLAG {f['estate']}: {f['kind']} -- {f['detail']}")
    with open(selection_path(reg, args.source), "w", encoding="utf-8") as fh:
        json.dump(sel, fh, indent=1)
    print(f"SERVED: {label[sel['served']]} -- {sel['reason']}")
    for m, s in sel["challengers"].items():
        print(f"  challenger {label[m]}: mean ratio {s['mean_ratio']:.2f}")
    print(f"wrote {selection_path(reg, args.source)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
