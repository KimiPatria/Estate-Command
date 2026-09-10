"""
Golden-set eval harness for the text-to-SQL pipeline (Bedrock migration #1).

Runs the SAME building blocks as the live /chat and /report pipelines
(hybrid retrieval -> build_sql_messages -> configured LLM -> extract/validate
-> execute, with the one-shot self-correction retry) against the labeled cases
in evals/sql_cases.yaml, and scores:

  * EXECUTION ACCURACY — the generated SQL is executed against the live EPMS
    schema and its result ROWS are diffed against the reference SQL's rows
    (order-insensitive unless the case says ordered; numeric tolerance; the
    generated result may contain extra columns — the expected columns' values
    must all be present). Never string-matches SQL text.
  * RETRIEVAL QUALITY — precision / recall / MRR of the hybrid
    ChromaDB+BM25+RRF retriever against each case's expected_tables.
  * LATENCY & COST — per-call latency and token counts from the
    provider-agnostic client, costed via evals/model_prices.yaml, so a future
    Groq-vs-Nova comparison is apples-to-apples.

Provider-agnostic by construction: generation goes through llm.call_llm(),
which resolves to whatever LLM_PROVIDER/model config is active. Running this
after a Bedrock swap requires zero changes here.

Usage (anaconda python):
  python sql_eval.py                    # full run, results + MLflow
  python sql_eval.py --limit 5          # smoke run
  python sql_eval.py --only sql_015
  python sql_eval.py --check-cases      # execute every expected_sql only (no LLM)
  python sql_eval.py --with-examples    # include few-shot examples from query
                                        # history (production config — leaks
                                        # near-duplicates of these cases!)
"""

import argparse
import decimal
import itertools
import json
import os
import time
from collections import Counter
from datetime import date, datetime

import yaml

_DIR = os.path.dirname(os.path.abspath(__file__))
_CASES_PATH = os.path.join(_DIR, "evals", "sql_cases.yaml")
_PRICES_PATH = os.path.join(_DIR, "evals", "model_prices.yaml")
_RESULTS_DIR = os.path.join(_DIR, "evals", "results")

_MAX_ROWS = 1000          # identical cap for reference and generated SQL
_MAX_PERM_COLS = 8        # column-permutation matching guard


# ── row comparison ───────────────────────────────────────────────────────────

def _norm_val(v):
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, int):
        return float(v)
    if isinstance(v, (datetime, date)):
        return str(v)
    if isinstance(v, str):
        return v.strip()
    return v


def _bag_eq(exp_rows: list[tuple], got_rows: list[tuple], ordered: bool) -> bool:
    if ordered:
        return exp_rows == got_rows
    return Counter(exp_rows) == Counter(got_rows)


def rows_match(exp_cols, exp_rows, got_cols, got_rows, ordered=False) -> bool:
    """Execution-accuracy row diff.

    Direct positional comparison first; if that fails and the generated result
    has at least as many columns, try every column selection/permutation (the
    model may alias, reorder, or add columns — values are what matter).
    """
    E = [tuple(_norm_val(r[c]) for c in exp_cols) for r in exp_rows]
    G = [tuple(_norm_val(r[c]) for c in got_cols) for r in got_rows]
    if len(E) != len(G):
        return False
    if not E:
        return True
    ne, ng = len(exp_cols), len(got_cols)
    if ne == ng and _bag_eq(E, G, ordered):
        return True
    if ne <= ng <= _MAX_PERM_COLS:
        for perm in itertools.permutations(range(ng), ne):
            G2 = [tuple(row[i] for i in perm) for row in G]
            if _bag_eq(E, G2, ordered):
                return True
    return False


# ── retrieval metrics ────────────────────────────────────────────────────────

def retrieval_metrics(expected: list[str], retrieved: list[str]) -> dict:
    exp, got = set(expected), list(retrieved)
    hit = exp & set(got)
    mrr = 0.0
    for rank, name in enumerate(got, start=1):
        if name in exp:
            mrr = 1.0 / rank
            break
    return {
        "precision": round(len(hit) / len(got), 3) if got else 0.0,
        "recall": round(len(hit) / len(exp), 3) if exp else 1.0,
        "mrr": round(mrr, 3),
        "all_tables_found": exp <= set(got),
    }


# ── SQL execution (same guarded engine as the pipeline) ─────────────────────

def _execute(sql: str) -> tuple[list[str], list[dict], str | None]:
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    from config import engine
    from sql_validator import ensure_limit
    try:
        limited = ensure_limit(sql, _MAX_ROWS)
        with engine.connect() as conn:
            result = conn.execute(text(limited))
            columns = list(result.keys())
            rows = [dict(r._mapping) for r in result.fetchall()]
        return columns, rows, None
    except SQLAlchemyError as exc:
        err = str(exc.orig) if getattr(exc, "orig", None) else str(exc)
        return [], [], err[:400]
    except Exception as exc:
        return [], [], str(exc)[:400]


# ── cost model ───────────────────────────────────────────────────────────────

def _load_prices() -> dict:
    try:
        with open(_PRICES_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _cost_usd(prices: dict, model: str, tokens_in: int, tokens_out: int) -> float | None:
    p = prices.get(model)
    if not p:
        return None
    return round(tokens_in / 1e6 * p["input"] + tokens_out / 1e6 * p["output"], 6)


# ── per-case evaluation ──────────────────────────────────────────────────────

def _call_llm_retry(messages, *, task: str, max_tokens: int = 800, attempts: int = 3):
    """call_llm with backoff on provider rate limits (429) so a burst-limited
    free tier doesn't poison a baseline run."""
    from llm import call_llm
    for attempt in range(attempts):
        try:
            return call_llm(messages, max_tokens=max_tokens, temperature=0, task=task)
        except Exception as exc:
            msg = str(exc)
            transient = "429" in msg or "rate limit" in msg.lower() or "Rate limit" in msg
            if not transient or attempt == attempts - 1:
                raise
            wait = 25 * (attempt + 1)
            print(f"    rate-limited; sleeping {wait}s", flush=True)
            time.sleep(wait)


def eval_case(case: dict, *, k: int, with_examples: bool, prices: dict) -> dict:
    from prompt_builder import build_sql_messages, extract_sql
    from retrieval import retrieve_tables, retrieve_examples
    from sql_validator import validate

    row: dict = {"case_id": case["id"], "query": case["query"],
                 "expected_tables": case["expected_tables"]}

    # 0. reference result (executed live so schema drift is caught immediately)
    exp_cols, exp_rows, exp_err = _execute(case["expected_sql"])
    if exp_err:
        row.update(status="bad_case", error=f"expected_sql failed: {exp_err}")
        return row
    row["expected_row_count"] = len(exp_rows)

    # 1. retrieval
    t0 = time.monotonic()
    table_cards, used_fallback = retrieve_tables(case["query"], k=k)
    row["retrieval_ms"] = int((time.monotonic() - t0) * 1000)
    retrieved = [c.name for c in table_cards]
    row["retrieved_tables"] = retrieved
    row["used_llm_fallback"] = used_fallback
    row["retrieval"] = retrieval_metrics(case["expected_tables"], retrieved)

    if not table_cards:
        row.update(status="no_tables", exec_match=False)
        return row

    # 2. few-shot examples (off by default: query history contains these very
    #    questions, which would leak answers into the prompt)
    examples = []
    if with_examples:
        try:
            examples = retrieve_examples(case["query"], k=2)
        except Exception:
            examples = []

    # 3. generation
    messages = build_sql_messages(case["query"], table_cards, examples)
    t0 = time.monotonic()
    raw, provider_used, usage = _call_llm_retry(messages, task="eval_sql_generation")
    gen_ms = int((time.monotonic() - t0) * 1000)
    tokens_in, tokens_out = usage["input_tokens"], usage["output_tokens"]
    model_id = usage.get("model", "")

    sql = extract_sql(raw)
    row.update(generated_sql=sql, provider=provider_used, model=model_id,
               generation_ms=gen_ms)
    if not sql:
        row.update(status="no_sql", exec_match=False,
                   tokens_in=tokens_in, tokens_out=tokens_out,
                   cost_usd=_cost_usd(prices, model_id, tokens_in, tokens_out))
        return row

    ok, reason = validate(sql)
    row["sql_valid"] = ok
    used_retry = False
    err = None if ok else f"validation: {reason}"
    got_cols: list = []
    got_rows: list = []
    if ok:
        got_cols, got_rows, err = _execute(sql)

    # 4. one-shot self-correction (mirrors chat_router step 5)
    if err:
        retry_messages = build_sql_messages(
            case["query"], table_cards, examples, prior_error=err, prior_sql=sql
        )
        t0 = time.monotonic()
        raw2, provider_used, usage2 = _call_llm_retry(
            retry_messages, task="eval_sql_self_correction"
        )
        gen_ms += int((time.monotonic() - t0) * 1000)
        tokens_in += usage2["input_tokens"]
        tokens_out += usage2["output_tokens"]
        sql2 = extract_sql(raw2)
        if sql2:
            ok2, _ = validate(sql2)
            if ok2:
                c2, r2, err2 = _execute(sql2)
                if not err2:
                    sql, got_cols, got_rows, err = sql2, c2, r2, None
                    used_retry = True
                    row["generated_sql"] = sql

    row.update(
        used_retry=used_retry,
        generation_ms=gen_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=_cost_usd(prices, model_id, tokens_in, tokens_out),
    )

    if err:
        row.update(status="exec_error", exec_error=err, exec_match=False)
        return row

    match = rows_match(exp_cols, exp_rows, got_cols, got_rows,
                       ordered=bool(case.get("ordered")))
    row.update(
        status="ok",
        generated_row_count=len(got_rows),
        exec_match=match,
    )
    return row


# ── aggregation / reporting ──────────────────────────────────────────────────

def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 4) if xs else None


def aggregate(run_id: str, rows: list[dict], meta: dict) -> dict:
    scored = [r for r in rows if r.get("status") != "bad_case"]
    executed = [r for r in scored if r.get("status") in ("ok", "exec_error")]
    return {
        "run_id": run_id,
        **meta,
        "n_cases": len(rows),
        "n_bad_cases": sum(1 for r in rows if r.get("status") == "bad_case"),
        "execution_accuracy": _mean([1.0 if r.get("exec_match") else 0.0 for r in scored]),
        "executable_rate": _mean([1.0 if r.get("status") == "ok" else 0.0 for r in scored]),
        "sql_valid_rate": _mean([1.0 if r.get("sql_valid") else 0.0 for r in executed]),
        "self_correction_used": sum(1 for r in scored if r.get("used_retry")),
        "retrieval_precision": _mean([r["retrieval"]["precision"] for r in scored if "retrieval" in r]),
        "retrieval_recall": _mean([r["retrieval"]["recall"] for r in scored if "retrieval" in r]),
        "retrieval_mrr": _mean([r["retrieval"]["mrr"] for r in scored if "retrieval" in r]),
        "retrieval_all_found_rate": _mean(
            [1.0 if r["retrieval"]["all_tables_found"] else 0.0 for r in scored if "retrieval" in r]),
        "mean_retrieval_ms": _mean([r.get("retrieval_ms") for r in scored]),
        "mean_generation_ms": _mean([r.get("generation_ms") for r in scored]),
        "total_tokens_in": sum(r.get("tokens_in") or 0 for r in scored),
        "total_tokens_out": sum(r.get("tokens_out") or 0 for r in scored),
        "total_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in scored), 4),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _log_mlflow(run_id: str, summary: dict, results_path: str):
    try:
        import mlflow
        mlflow.set_tracking_uri(
            "sqlite:///" + os.path.join(_DIR, "mlflow.db").replace(os.sep, "/"))
        art = "file:///" + os.path.join(_DIR, "mlartifacts").replace(os.sep, "/")
        exp = mlflow.get_experiment_by_name("text2sql-evals")
        exp_id = (exp.experiment_id if exp else
                  mlflow.create_experiment("text2sql-evals", artifact_location=art))
        with mlflow.start_run(experiment_id=exp_id, run_name=run_id):
            mlflow.log_params({k: summary[k] for k in
                               ("llm_provider", "model", "with_examples", "n_cases")})
            for k in ("execution_accuracy", "executable_rate", "sql_valid_rate",
                      "retrieval_precision", "retrieval_recall", "retrieval_mrr",
                      "retrieval_all_found_rate", "mean_retrieval_ms",
                      "mean_generation_ms", "total_cost_usd"):
                if summary.get(k) is not None:
                    mlflow.log_metric(k, summary[k])
            mlflow.log_artifact(results_path)
            mlflow.log_dict(summary, "summary.json")
        print("MLflow: eval run logged (experiment text2sql-evals)")
    except Exception as exc:
        print(f"MLflow logging skipped: {exc}")


# ── entry points ─────────────────────────────────────────────────────────────

def load_cases(path=_CASES_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_cases(cases: list[dict]) -> int:
    """Execute every expected_sql (no LLM). Returns count of failures."""
    failures = 0
    for case in cases:
        cols, rows, err = _execute(case["expected_sql"])
        if err:
            print(f"FAIL {case['id']}: {err}")
            failures += 1
        else:
            print(f"ok   {case['id']}: {len(rows)} rows, cols={cols}")
    print(f"\n{len(cases) - failures}/{len(cases)} reference queries execute cleanly")
    return failures


def run(args) -> dict:
    from metadata_loader import load_all_cards
    import retrieval

    cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    if args.limit:
        cases = cases[: args.limit]

    if args.check_cases:
        raise SystemExit(check_cases(cases))

    print(f"Initializing retrieval indexes (server-equivalent startup)...")
    retrieval.initialize(load_all_cards())

    import llm_client
    from config import LLM_PROVIDER
    prices = _load_prices()
    model = llm_client.resolve_model(tier="main")
    meta = {"llm_provider": LLM_PROVIDER, "model": model,
            "with_examples": bool(args.with_examples), "k": args.k}
    print(f"Provider={LLM_PROVIDER} model={model} cases={len(cases)} "
          f"with_examples={bool(args.with_examples)}\n")

    os.makedirs(_RESULTS_DIR, exist_ok=True)
    run_id = datetime.now().strftime("sqlrun_%Y%m%d_%H%M%S")
    out_path = os.path.join(_RESULTS_DIR, f"{run_id}.jsonl")

    rows = []
    for i, case in enumerate(cases, 1):
        try:
            row = eval_case(case, k=args.k, with_examples=args.with_examples,
                            prices=prices)
        except Exception as exc:
            row = {"case_id": case["id"], "status": "harness_error",
                   "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                   "exec_match": False}
        rows.append(row)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        r = row.get("retrieval") or {}
        detail = (row.get("exec_error") or row.get("error") or "")[:60]
        print(f"[{i}/{len(cases)}] {case['id']:<8} "
              f"match={'Y' if row.get('exec_match') else 'n'} "
              f"status={row.get('status')} "
              f"recall={r.get('recall')} gen_ms={row.get('generation_ms')} {detail}",
              flush=True)
        time.sleep(args.sleep)  # per-minute rate-limit hygiene

    summary = aggregate(run_id, rows, meta)
    with open(os.path.join(_RESULTS_DIR, f"{run_id}_summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    if not args.no_mlflow:
        _log_mlflow(run_id, summary, out_path)

    print("\n" + json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--cases", default=_CASES_PATH)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--with-examples", action="store_true",
                    help="include few-shot examples from query history "
                         "(production config; leaks near-duplicate cases)")
    ap.add_argument("--check-cases", action="store_true",
                    help="only execute every expected_sql; no LLM calls")
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--no-mlflow", action="store_true")
    run(ap.parse_args())
