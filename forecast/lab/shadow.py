"""
Shadow log -- every monthly model's forecast recorded when it is issued, and
scored later against the actuals that arrive after it.

Backtests replay history, and k3's history has now been reused by many
experiments; only forecasts written down before their outcome can settle the
champion/challenger question for good (promotion.py, Gate B). This keeps that
record.

The log (forecast/lab/shadow_log.csv) is append-only. Like every forecast CSV
it holds company production figures, so .gitignore keeps it out of the repo
(forecast/*/*.csv): it lives only on this machine, so back it up with the rest
of forecast/ -- losing it loses the shadow evidence.
  * one issue = one (estate, model, anchor) -- the anchor is the last complete
    month the forecast was made from; re-running on the same data adds nothing
  * a row per step 1..MAX_H: target period, prediction, band, when issued, and
    the model's own version stamp
  * existing rows are never rewritten; a model that changes gets new issues
Scoring joins the log to the latest history: a row resolves once its target
month is complete and scoreable (registry.month_history).

Monthly routine, after each data refresh (dashboard env; --rebuild first
regenerates the Chronos forward forecasts in the chronos env):
    python forecast/lab/shadow.py record --rebuild
    python forecast/lab/shadow.py status
"""

import argparse
import importlib.util
import os
import subprocess
import sys
from datetime import datetime

import numpy as np
import pandas as pd

_LAB = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_LAB))
LOG = os.path.join(_LAB, "shadow_log.csv")
COLS = ["issued_at", "estate", "grain", "anchor", "model", "version", "step",
        "period", "pred", "lower", "upper"]
SHADOW_ESTATES = ("k3", "ec")
CHRONOS_PYTHON = os.environ.get(
    "CHRONOS_PYTHON", r"C:\Users\DP\anaconda3\envs\chronos-rd-py313\python.exe")


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load_by_path("epms_lab_registry", os.path.join(_LAB, "registry.py"))


def read_log():
    if not os.path.exists(LOG):
        return pd.DataFrame(columns=COLS)
    return pd.read_csv(LOG, dtype={"anchor": str, "period": str, "version": str})


def rebuild_forward():
    """Regenerate the Chronos monthly forward forecasts in the chronos env."""
    cmd = [CHRONOS_PYTHON, os.path.join(_LAB, "build_artifacts.py"),
           "--grains", "month", "--no-backtest"]
    print("rebuilding Chronos forecasts:", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=_REPO, check=True)


def record(estates=SHADOW_ESTATES):
    """Append one issue per (estate, model) whose current forecast is not logged yet.

    Only forecasts anchored on the estate's latest complete month are logged;
    a stale artifact is reported, not recorded, so the log never holds a
    forecast made from older data than it claims.
    """
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    import experiment_router as er   # lazy: pulls in the served model

    log = read_log()
    seen = set(zip(log["estate"], log["model"], log["anchor"]))
    issued_at = datetime.now().isoformat(timespec="seconds")
    new, report = [], []
    for estate in estates:
        ok, reason, _ = reg.estate_grain_status(estate, "month")
        if not ok:
            report.append(f"{estate}: skipped ({reason})")
            continue
        anchor = reg.period_str(reg.month_history(estate)["period"].iloc[-1], "month")
        for spec in reg.MODELS:
            if "month" not in spec["grains"]:
                continue
            res = er._model_result(spec, estate, "month", anchor)
            tag = f"{estate}/{spec['id']}"
            if not res["available"]:
                report.append(f"{tag}: not logged ({res['reason']})")
                continue
            if res["stale"]:
                report.append(f"{tag}: not logged (stale -- {res['stale_note']}; "
                              "rebuild first)")
                continue
            if (estate, spec["id"], anchor) in seen:
                report.append(f"{tag}: already logged for anchor {anchor}")
                continue
            version = res.get("generated_at") or ""
            if spec["id"] == "served_ensemble":
                version = "; ".join(res.get("notes", [])) or version
            for k, s in enumerate(res["steps"], start=1):
                new.append({"issued_at": issued_at, "estate": estate, "grain": "month",
                            "anchor": anchor, "model": spec["id"], "version": version,
                            "step": k, "period": s["period"], "pred": s["value"],
                            "lower": s.get("lower"), "upper": s.get("upper")})
            report.append(f"{tag}: logged {len(res['steps'])} steps from anchor {anchor}")
    if new:
        out = pd.concat([log, pd.DataFrame(new, columns=COLS)], ignore_index=True)
        out.to_csv(LOG, index=False)
    return report, len(new)


def resolved(estate="k3", log=None):
    """Logged rows whose target month is now known, with the actual attached."""
    log = read_log() if log is None else log
    log = log[(log["estate"] == estate) & (log["grain"] == "month")]
    if log.empty:
        return log.assign(actual=pd.Series(dtype=float))
    h = reg.month_history(estate)
    known = {reg.period_str(p, "month"): v
             for p, v, s in zip(h["period"], h["value"], h["scoreable"]) if s}
    out = log[log["period"].isin(known)].copy()
    out["actual"] = out["period"].map(known).astype(float)
    return out


def summary(estate="k3"):
    """Per model: issues logged, rows resolved, MAE/MAPE by step on resolved rows."""
    log = read_log()
    log = log[log["estate"] == estate]
    res = resolved(estate, log)
    out = []
    for model, g in log.groupby("model"):
        r = res[res["model"] == model]
        e = (r["pred"] - r["actual"]).abs()
        steps = {int(s): {"n": int(len(x)), "mae": float((x["pred"] - x["actual"]).abs().mean()),
                          "mape": float(((x["pred"] - x["actual"]).abs() / x["actual"]).mean() * 100)}
                 for s, x in r.groupby("step")}
        out.append({"model": model, "issues": int(g["anchor"].nunique()),
                    "anchors": sorted(g["anchor"].unique().tolist()),
                    "first_issued": str(g["issued_at"].min()),
                    "resolved_rows": int(len(r)),
                    "mae": float(e.mean()) if len(r) else None, "by_step": steps})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="log the current forecasts")
    r.add_argument("--rebuild", action="store_true",
                   help="regenerate the Chronos forward forecasts first (chronos env)")
    r.add_argument("--estates", nargs="+", default=list(SHADOW_ESTATES))
    s = sub.add_parser("status", help="show what is logged and resolved")
    s.add_argument("--estate", default="k3")
    args = ap.parse_args()

    if args.cmd == "record":
        if args.rebuild:
            rebuild_forward()
        report, n = record(args.estates)
        print("\n".join(report))
        print(f"\n{n} rows appended to {LOG}" if n else "\nnothing new to log")
    else:
        for m in summary(args.estate):
            steps = "  ".join(f"h{k}: MAE {v['mae']:,.0f} (n={v['n']})"
                              for k, v in sorted(m["by_step"].items())[:3])
            print(f"{m['model']:24s} {m['issues']} issue(s) {m['anchors']}  "
                  f"resolved {m['resolved_rows']}  {steps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
