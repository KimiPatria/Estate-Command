"""
Eval harness for the Forecast Investigator (Stage-3 R&D).

Runs the agent on the labeled historical anomaly set
(evals/anomaly_cases.jsonl), grades each verdict with an LLM judge, and writes
per-case results + aggregates to evals/results/ and MLflow (experiment
`investigator-evals`).

Judge design (per the LLM-as-judge playbook):
  * Direct scoring — ground truth exists (labeled causes), so no pairwise.
  * DIFFERENT model as judge (GROQ_MODEL_JUDGE, default qwen/qwen3-32b)
    than the agent (GROQ_MODEL, default openai/gpt-oss-120b) — mitigates
    self-enhancement bias.
  * Evidence-before-score: the judge must quote evidence per criterion before
    emitting numbers; explicit length/authority-neutrality instructions.
  * One criterion = one aspect: cause_correctness (categorical full/partial/
    miss vs the label), groundedness 1-5 (claims traceable to the tool trail),
    actionability 1-5 (follow-ups concrete + targeted).
  * Calibration is COMPUTED, not judged: Brier = mean((confidence - c)^2)
    with c in {1, .5, 0} for {full, partial, miss}.
  * disagreement_flag routes suspect labels to human review (the cases are
    auto-forensics; human_verified=false until reviewed).

Deterministic pre-checks run before the judge (schema-valid verdict, category
in taxonomy, confidence in [0,1], >=2 successful tool calls); failures are
recorded per-case and surface in the aggregate.

Usage (anaconda python):
  python eval_harness.py                 # all 20 cases
  python eval_harness.py --limit 3       # smoke run
  python eval_harness.py --only anom_202410
"""

import argparse
import asyncio
import json
import os
import re
import time
from datetime import datetime

CATEGORIES = ["under_recording", "weather_lagged", "seasonal_model_bias",
              "level_shift", "no_anomaly", "unknown_external"]
_CORRECTNESS_SCORE = {"full": 1.0, "partial": 0.5, "miss": 0.0}

_DIR = os.path.dirname(os.path.abspath(__file__))
_CASES = os.path.join(_DIR, "evals", "anomaly_cases.jsonl")
_RESULTS_DIR = os.path.join(_DIR, "evals", "results")


# ── judge ────────────────────────────────────────────────────────────────────

def _judge_case(case: dict, verdict: dict, evidence_obs: list) -> dict:
    import llm_client
    from config import GROQ_MODEL_JUDGE
    from prompts import load_prompt
    prompt = load_prompt(
        "investigator_judge",
        label=json.dumps(case["label"]),
        facts=json.dumps({k: case.get(k) for k in
                          ("month", "actual", "predicted", "error_pct",
                           "band_breach", "n_harvest_days", "is_underrecorded")}),
        verdict=json.dumps(verdict),
        # 5000 chars (not 6000): qwen's 6000 TPM cap counts input+max_tokens,
        # and 6000-char evidence pushed marginal requests to 413.
        evidence=json.dumps(evidence_obs)[:5000],
    )
    # gpt-oss is a reasoning model: give the reasoning channel headroom or
    # the JSON gets truncated and Groq's json_object validator 400s the call.
    extra = {"reasoning_effort": "low"} if "gpt-oss" in GROQ_MODEL_JUDGE else None
    result = llm_client.chat(
        [{"role": "user", "content": prompt}],
        model=GROQ_MODEL_JUDGE,
        temperature=0.0,
        # input + max_tokens must stay under qwen's 6000 tokens-per-minute
        # request ceiling on the free tier; 3000 overflowed on evidence-heavy
        # cases (413 Request too large).
        max_tokens=2400,
        json_mode=True,
        task="investigator_judge",
        extra_kwargs=extra,
    )
    msg = result.message
    # gpt-oss is a reasoning model: JSON may land in content while chain-of-
    # thought sits in the reasoning channel — fall back if content came back empty.
    raw = result.text or str(
        (getattr(msg, "additional_kwargs", None) or {}).get("reasoning_content") or ""
    ).strip()
    parsed = {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    if not parsed:
        parsed = {"judge_raw": raw[:800]}  # keep for debugging
    for k in ("groundedness", "actionability"):  # coerce "4" -> 4.0
        try:
            parsed[k] = float(parsed[k])
        except (KeyError, TypeError, ValueError):
            parsed[k] = None
    parsed["judge_model"] = GROQ_MODEL_JUDGE
    return parsed


# ── deterministic pre-checks ─────────────────────────────────────────────────

def _prechecks(verdict: dict, evidence_log: list) -> list:
    fails = []
    if verdict.get("cause_category") not in CATEGORIES:
        fails.append("cause_category_invalid")
    c = verdict.get("confidence")
    if not isinstance(c, (int, float)) or not (0.0 <= float(c) <= 1.0):
        fails.append("confidence_out_of_range")
    if not verdict.get("root_cause"):
        fails.append("root_cause_missing")
    if not verdict.get("key_evidence"):
        fails.append("key_evidence_empty")
    if sum(1 for e in evidence_log if e.get("ok")) < 2:
        fails.append("fewer_than_2_successful_tools")
    return fails


# ── run ──────────────────────────────────────────────────────────────────────

async def run(cases_path=_CASES, limit=None, only=None):
    from forecast_router import get_forecast
    from forecast_investigator import investigate
    from tracing import setup_tracing

    setup_tracing(project_name="investigator-evals")

    cases = [json.loads(l) for l in open(cases_path, encoding="utf-8")
             if l.strip()]
    if only:
        cases = [c for c in cases if c["id"] == only]
    if limit:
        cases = cases[:limit]

    os.makedirs(_RESULTS_DIR, exist_ok=True)
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    out_path = os.path.join(_RESULTS_DIR, f"{run_id}.jsonl")

    rows = []
    for i, case in enumerate(cases, 1):
        t0 = time.time()
        print(f"[{i}/{len(cases)}] {case['id']} ({case['label']['category']}) …",
              flush=True)
        # Agent and judge failures are different animals: an agent crash is a
        # real miss (no verdict produced); a judge crash is infrastructure —
        # the case is recorded as "unjudged" and excluded from accuracy.
        try:
            inv = await investigate(get_forecast, None, case=case)
            verdict = inv.get("report") or {}
            elog = inv.get("evidence_log") or []
            eobs = inv.get("evidence_observations") or []
            fails = _prechecks(verdict, elog)
        except Exception as exc:
            verdict, elog, eobs = {}, [], []
            fails = [f"agent_error:{type(exc).__name__}:{str(exc)[:180]}"]

        if verdict:
            judge = None
            for attempt in (1, 2):
                try:
                    judge = await asyncio.to_thread(_judge_case, case, verdict, eobs)
                    break
                except Exception as exc:
                    judge_err = str(exc)[:200]
                    print(f"    judge attempt {attempt} failed: {judge_err[:100]}",
                          flush=True)
            if judge is None:
                judge = {"cause_correctness": "unjudged",
                         "judge_comment": f"judge failed: {judge_err}"}
        else:
            judge = {"cause_correctness": "miss", "groundedness": 1.0,
                     "actionability": 1.0, "judge_comment": "agent run failed"}

        corr = str(judge.get("cause_correctness") or "unjudged").lower()
        if corr not in (*_CORRECTNESS_SCORE, "unjudged"):
            corr = "unjudged"
        cscore = _CORRECTNESS_SCORE.get(corr)  # None when unjudged
        conf = verdict.get("confidence") if isinstance(
            verdict.get("confidence"), (int, float)) else 0.0
        row = {
            "run_id": run_id, "case_id": case["id"], "month": case["month"],
            "label_category": case["label"]["category"],
            "label_cause": case["label"]["cause"],
            "predicted_category": verdict.get("cause_category"),
            "root_cause": verdict.get("root_cause"),
            "confidence": conf,
            "cause_correctness": corr, "correctness_score": cscore,
            "groundedness": judge.get("groundedness"),
            "actionability": judge.get("actionability"),
            "disagreement_flag": bool(judge.get("disagreement_flag")),
            "judge_comment": judge.get("judge_comment"),
            "judge_evidence": judge.get("evidence_notes"),
            "judge_full": judge,
            "precheck_failures": fails,
            "tools_used": [e.get("tool") for e in elog],
            "tools_failed": [e.get("tool") for e in elog if not e.get("ok")],
            "brier": (round((float(conf) - cscore) ** 2, 4)
                      if cscore is not None else None),
            "elapsed_s": round(time.time() - t0, 1),
            "verdict": verdict,
        }
        rows.append(row)
        with open(out_path, "a", encoding="utf-8") as f:  # salvageable partials
            f.write(json.dumps(row) + "\n")
        print(f"    -> {corr} (pred={row['predicted_category']}, "
              f"grounded={row['groundedness']}, {row['elapsed_s']}s)", flush=True)
        await asyncio.sleep(2)  # be gentle with per-minute rate limits

    summary = _aggregate(run_id, rows)
    with open(os.path.join(_RESULTS_DIR, f"{run_id}_summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    _log_mlflow(run_id, summary, out_path)
    print("\n" + json.dumps({k: v for k, v in summary.items()
                             if k != "per_category"}, indent=1))
    print("per-category:", json.dumps(summary["per_category"], indent=1))
    return summary


def _aggregate(run_id: str, rows: list) -> dict:
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    judged = [r for r in rows if r["correctness_score"] is not None]
    per_cat = {}
    for cat in CATEGORIES:
        sub = [r for r in judged if r["label_category"] == cat]
        if sub:
            per_cat[cat] = {"n": len(sub),
                            "partial_credit": mean([r["correctness_score"] for r in sub])}
    return {
        "run_id": run_id,
        "n_cases": len(rows),
        "n_judged": len(judged),
        "accuracy_full": mean([1.0 if r["cause_correctness"] == "full" else 0.0
                               for r in judged]),
        "partial_credit": mean([r["correctness_score"] for r in judged]),
        "groundedness": mean([r["groundedness"] for r in rows
                              if isinstance(r["groundedness"], (int, float))]),
        "actionability": mean([r["actionability"] for r in rows
                               if isinstance(r["actionability"], (int, float))]),
        "brier_calibration": mean([r["brier"] for r in rows
                                   if r["brier"] is not None]),
        "precheck_failure_cases": sum(1 for r in rows if r["precheck_failures"]),
        "judge_disagreements": sum(1 for r in rows if r["disagreement_flag"]),
        "unjudged_cases": len(rows) - len(judged),
        "mean_elapsed_s": mean([r["elapsed_s"] for r in rows]),
        "per_category": per_cat,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _log_mlflow(run_id: str, summary: dict, results_path: str):
    try:
        import mlflow
        import llm_client
        from config import INVESTIGATOR_MODEL, GROQ_MODEL_JUDGE
        agent_model = llm_client.resolve_model(INVESTIGATOR_MODEL, tier="main")
        mlflow.set_tracking_uri(
            "sqlite:///" + os.path.join(_DIR, "mlflow.db").replace(os.sep, "/"))
        art = "file:///" + os.path.join(_DIR, "mlartifacts").replace(os.sep, "/")
        exp = mlflow.get_experiment_by_name("investigator-evals")
        exp_id = (exp.experiment_id if exp else
                  mlflow.create_experiment("investigator-evals", artifact_location=art))
        with mlflow.start_run(experiment_id=exp_id, run_name=run_id):
            mlflow.log_params({"agent_model": agent_model,
                               "judge_model": GROQ_MODEL_JUDGE,
                               "n_cases": summary["n_cases"]})
            for k in ("accuracy_full", "partial_credit", "groundedness",
                      "actionability", "brier_calibration",
                      "precheck_failure_cases", "judge_disagreements"):
                if summary.get(k) is not None:
                    mlflow.log_metric(k, summary[k])
            for cat, v in summary["per_category"].items():
                mlflow.log_metric(f"pc_{cat}", v["partial_credit"])
            mlflow.log_artifact(results_path)
            mlflow.log_dict(summary, "summary.json")
        print(f"MLflow: eval run logged (experiment investigator-evals)")
    except Exception as exc:
        print(f"MLflow logging skipped: {exc}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=_CASES)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only")
    a = ap.parse_args()
    asyncio.run(run(a.cases, a.limit, a.only))
