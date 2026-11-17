"""Load the hand-written seed examples (examples/seed_<source>.yaml) as verified few-shot examples.

Every seed is checked before it may be loaded:
  * it passes the safety validator, the source's lint and the schema check,
  * it runs, returns rows and passes the result checks (on at least one
    estate database for live, which fans out over BA and K3),
  * its question is not a near-copy of an eval question (cosine similarity
    >= 0.90 with any question in evals/golden_chat.yaml, sql_cases.yaml or
    sql_cases_multi.yaml): an eval must not be answerable by its own seed.

  python evals/seed_examples.py --check                 # report only
  python evals/seed_examples.py --apply                 # load the seeds that pass
  python evals/seed_examples.py --check --source staging

Safe to re-run: a seed already stored (same question and SQL) is only marked
verified again. Loaded seeds go into query_history.db with origin=seed,
verified=1, and into the Chroma example index, so the running server picks
them up without a restart (a restart rebuilds the index from the database too).
"""
import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import data_sources  # noqa: E402
import metadata_loader  # noqa: E402

LEAK_SIMILARITY = 0.90
EVAL_FILES = ["evals/golden_chat.yaml", "evals/sql_cases.yaml", "evals/sql_cases_multi.yaml"]


def eval_questions() -> list[str]:
    out: list[str] = []
    for f in EVAL_FILES:
        p = ROOT / f
        if not p.exists():
            continue
        for case in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
            out.append(case["query"])
            out.extend(case.get("turns", []))
    return out


def _cosine(a, b) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


def _run(engine, sql: str):
    from sqlalchemy import text
    with engine.connect() as conn:
        res = conn.execute(text(sql))
        cols = list(res.keys())
        rows = [dict(zip(cols, r)) for r in res.fetchmany(500)]
    return cols, rows


def check_seed(seed: dict, src) -> tuple[list[str], int]:
    """(problems, rows returned) for one seed."""
    import chat_router
    import sql_schema_check
    from sql_validator import validate

    sql = seed["sql"].strip().rstrip(";")
    q = seed["question"]
    problems: list[str] = []
    ok, why = validate(sql, src.dialect)
    if not ok:
        return [f"unsafe: {why}"], 0
    lint = src.lint(sql, q) if src.lint else None
    if lint:
        problems.append(f"lint: {lint[:120]}")
    schema = sql_schema_check.check(sql, src.dialect, src.id)
    if schema:
        problems.append(f"schema: {schema[:120]}")
    if problems:
        return problems, 0

    total, results = 0, []
    for sid in src.scope_order:
        try:
            cols, rows = _run(src.scopes[sid]["engine"], sql)
        except Exception as exc:
            problems.append(f"{src.scopes[sid]['label']}: error {str(exc).splitlines()[0][:140]}")
            continue
        rows = [{k: (float(v) if hasattr(v, "is_finite") else v) for k, v in r.items()} for r in rows]
        with data_sources.use(src.id):
            kind, finding = chat_router._result_finding(cols, rows, q, sql)
        results.append((src.scopes[sid]["label"], kind, finding))
        if not kind:
            total += len(rows)
    if not any(kind is None for _, kind, _ in results):
        problems.extend(f"{label}: {finding[:100]}" for label, kind, finding in results if finding)
    return problems, total


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--source", choices=["live", "staging", "all"], default="all")
    args = ap.parse_args()

    from retrieval import _embed, _example_meta
    from query_history import find_pair, record_query, set_verified

    evq = eval_questions()
    ev_emb = _embed(evq) if evq else []
    bad_total, loaded = 0, 0
    for src in data_sources.available():
        if args.source != "all" and src.id != args.source:
            continue
        path = ROOT / "examples" / f"seed_{src.id}.yaml"
        if not path.exists():
            print(f"{src.id}: no {path.name}")
            continue
        metadata_loader.load_all_cards(source=src)
        seeds = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        emb = _embed([s["question"] for s in seeds])
        print(f"\n{src.id}: {len(seeds)} seeds in {path.name}")
        good = []
        for seed, e in zip(seeds, emb):
            near = max(((_cosine(e, x), q) for x, q in zip(ev_emb, evq)), default=(0.0, ""))
            problems, n = check_seed(seed, src)
            if near[0] >= LEAK_SIMILARITY:
                problems.append(f"too close to eval question ({near[0]:.2f}): {near[1][:70]}")
            flag = "ok  " if not problems else "FAIL"
            print(f"  {flag} {n:5} rows  sim {near[0]:.2f}  {seed['question'][:70]}")
            for p in problems:
                print(f"         - {p}")
            if problems:
                bad_total += 1
            else:
                good.append((seed, e, n))

        if not args.apply:
            continue
        coll = None
        try:
            import chromadb
            coll = chromadb.PersistentClient(path="./chroma_store").get_or_create_collection(
                src.history_collection, metadata={"hnsw:space": "cosine"})
        except Exception as exc:
            print(f"  (example index not updated: {exc}; it is rebuilt at the next server start)")
        for seed, e, n in good:
            sql = seed["sql"].strip().rstrip(";")
            qid = find_pair(seed["question"], sql, src.id)
            if qid is None:
                qid = record_query(seed["question"], sql, "seed", [], success=True, source=src.id,
                                   row_count=n, origin="seed", verified=True)
            else:
                set_verified(qid)
            if coll is not None:
                coll.upsert(ids=[str(qid)], embeddings=[list(e)], documents=[seed["question"]],
                            metadatas=[_example_meta(sql, True, "seed")])
            loaded += 1
        print(f"  loaded {len(good)} seed(s) for {src.id}")

    print(f"\n{bad_total} seed(s) failed" + (f", {loaded} loaded" if args.apply else ""))
    sys.exit(1 if bad_total and not args.apply else 0)


if __name__ == "__main__":
    main()
