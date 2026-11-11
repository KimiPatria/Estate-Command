"""Score the leak diagnosis against the planted causes (buildplan_finance.md §5).

    python evals/run_leak_diagnosis.py [--runs 3]

Two settings, each run --runs times (Nova varies run to run; see
evals/BASELINE.md, so medians are compared, not single runs):

    with_checks     what the window shows: the server's checks in the grounding
    without_checks  the ablation: the causes and actions only. If this scores
                    as well, the model is guessing from the driver's name and
                    the checks are decoration.

Measures, per setting: likely cause = planted cause (planted cases), true
cause ruled out, decoys read as within_norm or only "possible", unverified
figures and invalid ids after the retry. Results go to
evals/results/leak_diagnosis_<stamp>.json and leak_diagnosis_latest.json,
which the cost window's Trust section reads.

This is the only reader of the answer key besides finance.accuracy().
"""

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import config  # noqa: E402,F401  (loads .env)
from gis import diagnosis  # noqa: E402

KEY = ROOT / "gis" / "data" / "synthetic" / "ec_fi_leak_key.csv"
OUT = ROOT / "evals" / "results"


def answer_key() -> dict:
    with KEY.open(encoding="utf-8") as fh:
        next(fh)
        return {r["leak_id"]: r for r in csv.DictReader(fh)}


def run_once(case: dict, with_checks: bool, force: bool = False) -> dict:
    d = diagnosis.diagnose(case["unit"], case["month"], refresh=True, with_checks=with_checks, lang="en",
                           force=force)
    if d.get("within_norm"):
        return {"available": True, "cause": "within_norm", "confidence": "gate", "ruled_out": [],
                "headline": d.get("reason"), "unverified": 0, "retried": False, "tokens": 0}
    if not d.get("available"):
        return {"available": False, "cause": None, "confidence": None, "ruled_out": [],
                "reason": d.get("reason"), "attempts": d.get("attempts")}
    g = d["diagnosis"]
    return {"available": True, "cause": g["cause"]["id"], "confidence": g["cause"]["confidence"],
            "ruled_out": [r["id"] for r in g["ruled_out"]], "headline": g["headline"],
            "unverified": len(d["audit"]["unverified"]), "retried": len(d["attempts"]) > 1,
            "tokens": (d.get("model") or {}).get("usage", {}).get("total_tokens")}


def score(case, truth, r) -> dict:
    decoy = truth == "within_norm"
    if not r["available"]:
        return {"correct": False, "true_ruled_out": False, "invalid": True}
    if decoy:
        ok = r["cause"] == "within_norm" or r["confidence"] == "possible"
    else:
        ok = r["cause"] == truth
    return {"correct": ok, "true_ruled_out": (not decoy) and truth in r["ruled_out"], "invalid": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    cases = yaml.safe_load((ROOT / "evals" / "leak_diagnosis_cases.yaml").read_text(encoding="utf-8"))["cases"]
    key = answer_key()
    # The product setting, the ablation, and the decoys with the materiality gate lifted, so the
    # model's own reading of noise stays measured.
    settings = [("with_checks", "With the checks", True, False, None),
                ("without_checks", "Checks withheld", False, False, None),
                ("decoys_ungated", "Decoys, gate lifted", True, True, "within_norm")]
    results = {c["id"]: {"id": c["id"], "unit": c["unit"], "month": c["month"],
                         "true_cause": key[c["key"]]["true_cause"], "results": {}, "runs": {}} for c in cases}
    summary = []
    t0 = time.time()
    for skey, label, with_checks, force, only in settings:
        per_run = []
        scases = [c for c in cases if only is None or results[c["id"]]["true_cause"] == only]
        for i in range(args.runs):
            correct = ruled = invalid = unverified = planted = decoys_ok = tokens = cf = 0
            for c in scases:
                truth = results[c["id"]]["true_cause"]
                r = run_once(c, with_checks, force)
                s = score(c, truth, r)
                results[c["id"]]["results"].setdefault(skey, []).append(r["cause"] or "unavailable")
                results[c["id"]]["runs"].setdefault(skey, []).append({**r, **s})
                correct += s["correct"]
                ruled += s["true_ruled_out"]
                invalid += s["invalid"]
                unverified += r.get("unverified", 0) or 0
                tokens += r.get("tokens") or 0
                if truth == "within_norm":
                    decoys_ok += s["correct"]
                else:
                    planted += s["correct"]
                if c.get("counterfactual"):
                    cf += s["correct"]
                print(f"{label} run {i + 1}: {c['id']:<22} truth {truth:<22} got {r['cause']} "
                      f"({r['confidence']}) {'OK' if s['correct'] else '--'}", flush=True)
            per_run.append({"correct": correct, "planted_correct": planted, "decoys_ok": decoys_ok, "cf_correct": cf,
                            "true_ruled_out": ruled, "invalid": invalid, "unverified": unverified,
                            "tokens": tokens})
        summary.append({
            "key": skey, "label": label, "runs": args.runs, "cases": len(scases),
            "planted_cases": sum(1 for c in scases if results[c["id"]]["true_cause"] != "within_norm"),
            "decoy_cases": sum(1 for c in scases if results[c["id"]]["true_cause"] == "within_norm"),
            "counterfactual_cases": sum(1 for c in scases if c.get("counterfactual")),
            "median_counterfactual_correct": statistics.median(r["cf_correct"] for r in per_run),
            "median_correct": statistics.median(r["correct"] for r in per_run),
            "median_planted_correct": statistics.median(r["planted_correct"] for r in per_run),
            "median_decoys_ok": statistics.median(r["decoys_ok"] for r in per_run),
            "median_true_ruled_out": statistics.median(r["true_ruled_out"] for r in per_run),
            "unverified": sum(r["unverified"] for r in per_run),
            "invalid": sum(r["invalid"] for r in per_run),
            "tokens_per_run": statistics.median(r["tokens"] for r in per_run),
            "per_run": per_run,
        })
    out = {"run_at": datetime.now().isoformat(timespec="seconds"),
           "model": "amazon.nova-pro-v1:0", "seconds": round(time.time() - t0),
           "settings": summary, "cases": list(results.values()),
           "note": ("Planted world, in-sample: the cause descriptions were revised once after the first run "
                    "(the overtime pair), and the counterfactuals and the materiality gate were added after "
                    "it. Real leaks mix causes; expect lower on the client's data.")}
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (OUT / f"leak_diagnosis_{stamp}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    (OUT / "leak_diagnosis_latest.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print()
    for s in summary:
        print(f"{s['label']}: {s['median_planted_correct']}/{s['planted_cases']} planted right "
              f"(counterfactuals {s['median_counterfactual_correct']}/{s['counterfactual_cases']}), "
              f"{s['median_decoys_ok']}/{s['decoy_cases']} decoys read as normal, true cause ruled out "
              f"{s['median_true_ruled_out']}, unverified figures {s['unverified']}, invalid {s['invalid']} "
              f"(medians of {s['runs']} runs)")


if __name__ == "__main__":
    main()
