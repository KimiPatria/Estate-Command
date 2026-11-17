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

Both sources are logged (live and staging, see SHADOW_ESTATES): every row
carries its estate's source, so scoring can be split by source.

Monthly routine, after each data refresh (dashboard env; --rebuild first
regenerates the Chronos forward forecasts in the chronos env, whose python is
CHRONOS_PYTHON in .env):
    python forecast/lab/shadow.py record --rebuild
    python forecast/lab/shadow.py status
    python forecast/lab/shadow.py backup        # off-machine copy (S3)
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
        "period", "pred", "lower", "upper", "source"]
# Live EA is left out: its snapshot holds 2 months, below the 3 a forecast needs.
SHADOW_ESTATES = ("k3", "ec", "eb", "stg_ec", "stg_eb", "stg_ea")
# Off-machine copies go to the account's SageMaker bucket (build plan D3).
BACKUP_PREFIX = "forecast-runs/shadow"


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_REPO, ".env"))
    except ImportError:
        pass


def chronos_python():
    """Interpreter of the Chronos env, from CHRONOS_PYTHON (.env or the environment)."""
    _load_env()
    path = os.environ.get("CHRONOS_PYTHON")
    if not path:
        raise SystemExit("CHRONOS_PYTHON is not set: add CHRONOS_PYTHON=<path to the "
                         "chronos-rd-py313 python.exe> to .env")
    return path


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = _load_by_path("epms_lab_registry", os.path.join(_LAB, "registry.py"))


def read_log():
    if not os.path.exists(LOG):
        return pd.DataFrame(columns=COLS)
    log = pd.read_csv(LOG, dtype={"anchor": str, "period": str, "version": str})
    if "source" not in log.columns:          # rows written before both sources were logged
        log["source"] = None
    missing = log["source"].isna()
    log.loc[missing, "source"] = log.loc[missing, "estate"].map(
        lambda e: reg.ESTATES.get(e, {}).get("source"))
    return log[COLS]


def rebuild_forward():
    """Regenerate the Chronos monthly forward forecasts in the chronos env."""
    cmd = [chronos_python(), os.path.join(_LAB, "build_artifacts.py"),
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
            version = _version(spec, res)
            for k, s in enumerate(res["steps"], start=1):
                new.append({"issued_at": issued_at, "estate": estate, "grain": "month",
                            "anchor": anchor, "model": spec["id"], "version": version,
                            "step": k, "period": s["period"], "pred": s["value"],
                            "lower": s.get("lower"), "upper": s.get("upper"),
                            "source": reg.ESTATES[estate]["source"]})
            report.append(f"{tag}: logged {len(res['steps'])} steps from anchor {anchor}")
    if new:
        out = pd.concat([log, pd.DataFrame(new, columns=COLS)], ignore_index=True)
        out.to_csv(LOG, index=False)
    return report, len(new)


def _version(spec, res):
    """What identifies the forecast that was logged: the ensemble's own fit note,
    otherwise the artifact's build time plus the checkpoint revision it ran on."""
    if spec["id"] == "served_ensemble":
        return "; ".join(res.get("notes", [])) or (res.get("generated_at") or "")
    parts = [res.get("generated_at") or ""]
    if spec["source"] == "artifact":
        man = reg.read_manifest() or {}
        rev = man.get("checkpoint_revision")
        parts.append(f"{man.get('checkpoint', 'amazon/chronos-2')}@{rev[:12] if rev else 'unpinned'}")
    return "; ".join(p for p in parts if p)


def backup(dest=None):
    """Copy the log off this machine: S3, server-side encrypted, one dated key per copy."""
    if not os.path.exists(LOG):
        raise SystemExit(f"no shadow log at {LOG}")
    import boto3
    sm = _load_by_path("epms_chronos_sagemaker", os.path.join(_REPO, "forecast",
                                                               "chronos_finetune_sagemaker.py"))
    root = (dest or f"{sm.DEFAULT_S3_ROOT}/{BACKUP_PREFIX}").rstrip("/")
    if not root.startswith("s3://"):
        raise SystemExit(f"backup destination must be an s3:// URI, got {root}")
    bucket, _, prefix = root[5:].partition("/")
    key = f"{prefix}/shadow_log_{datetime.now():%Y%m%dT%H%M%S}.csv".lstrip("/")
    try:
        boto3.client("s3", region_name=sm.DEFAULT_REGION).upload_file(
            LOG, bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    except Exception as exc:
        raise SystemExit(f"backup failed ({type(exc).__name__}: {exc}). "
                         "If the AWS session expired, run `aws login` and retry.")
    return f"s3://{bucket}/{key}"


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
    s.add_argument("--estate", default=None, help="one estate (default: every logged estate)")
    b = sub.add_parser("backup", help="copy the log to S3 (server-side encrypted)")
    b.add_argument("--dest", default=None, help=f"s3:// prefix (default: <SageMaker bucket>/{BACKUP_PREFIX})")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):      # model labels carry a middle dot
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.cmd == "record":
        if args.rebuild:
            rebuild_forward()
        report, n = record(args.estates)
        print("\n".join(report))
        print(f"\n{n} rows appended to {LOG}" if n else "\nnothing new to log")
    elif args.cmd == "backup":
        print(f"copied {LOG} -> {backup(args.dest)}")
    else:
        log = read_log()
        estates = [args.estate] if args.estate else list(dict.fromkeys(log["estate"]))
        for estate in estates:
            src = reg.ESTATES.get(estate, {}).get("source", "?")
            print(f"== {estate} ({src})")
            for m in summary(estate):
                steps = "  ".join(f"h{k}: MAE {v['mae']:,.0f} (n={v['n']})"
                                  for k, v in sorted(m["by_step"].items())[:3])
                print(f"   {m['model']:24s} {m['issues']} issue(s) {m['anchors']}  "
                      f"resolved {m['resolved_rows']}  {steps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
