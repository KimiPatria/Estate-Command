"""Run evals/golden_chat.yaml through the real chat pipeline and score the figures.

Each case is sent to /chat/message (fresh chat every time, as a first question),
the answer's rows are compared with figures computed by hand-written reference
SQL, and the generated SQL is checked against the case's must / must-not
patterns. Every case is run --runs times, because the model is not
deterministic: the number to watch is the pass rate and whether the SQL varies.

Usage (Anaconda python, repo root):
  python evals/run_golden_chat.py --check-cases        # reference SQL only, no LLM
  python evals/run_golden_chat.py --runs 3             # the test
  python evals/run_golden_chat.py --only sd_002,sd_007 --runs 5
  python evals/run_golden_chat.py --database live --min-pass 0.9   # exit 1 below 90%

Runs are not saved as few-shot examples (see --record).
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import data_sources  # noqa: E402
from config import ESTATES, ESTATE_ORDER  # noqa: E402

CASES = ROOT / "evals" / "golden_chat.yaml"
RESULTS = ROOT / "evals" / "results"


# ── expected figures ───────────────────────────────────────────────────────

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _run(engine, sql: str) -> list[dict]:
    with engine.connect() as conn:
        res = conn.execute(text(sql))
        cols = list(res.keys())
        return [dict(zip(cols, r)) for r in res.fetchall()]


def scope_ids(case: dict) -> list[str]:
    s = (case.get("scope") or "all").lower()
    return list(ESTATE_ORDER) if s == "all" else [s]


def figures(case: dict, sql: str) -> list[tuple[str, float]]:
    """[(key, value)] the reference SQL yields: a live estate label, or a staging estate name."""
    out: list[tuple[str, float]] = []
    if case["database"] == "live":
        for eid in scope_ids(case):
            rows = _run(ESTATES[eid]["engine"], sql)
            v = _fnum(rows[0]["value"]) if rows else None
            out.append((ESTATES[eid]["label"], v if v is not None else 0.0))
    else:
        for r in _run(data_sources.get("staging").engine, sql):
            out.append((str(r["estate"]), _fnum(r["value"]) or 0.0))
    return out


def same(a: float, b: float, rel_tol: float = 1e-6) -> bool:
    return abs(a - b) <= rel_tol * max(1.0, abs(b)) + 0.005


# ── reading the response ───────────────────────────────────────────────────

def candidates(resp: dict, key: str, single: bool) -> list[float]:
    """Every figure in the response that could be the answer for `key`: each
    number in its rows, and each column's total over its rows (so a monthly
    series that sums to the figure counts)."""
    rows = resp.get("rows") or []
    if rows and "_source_estate" in rows[0]:
        sel = [r for r in rows if r.get("_source_estate") == key]
    else:
        named = [r for r in rows if any(isinstance(v, str) and key.lower() in v.lower() for v in r.values())]
        sel = named or (rows if single else [])
    nums = [float(v) for r in sel for v in r.values() if _num(v)]
    cols = {c for r in sel for c in r}
    sums = [sum(float(r[c]) for r in sel if _num(r.get(c))) for c in cols]
    return nums + sums


_NUMBER = re.compile(r"\d[\d.,]*")


def answer_numbers(answer: str) -> list[float]:
    out = []
    for tok in _NUMBER.findall(answer or ""):
        tok = tok.rstrip(".,")
        for cleaned in {tok.replace(",", ""), tok.replace(".", "").replace(",", ".")}:
            v = _fnum(cleaned)
            if v is not None:
                out.append(v)
    return out


def norm_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip().lower())


# ── one run of one case ────────────────────────────────────────────────────

def judge(case: dict, resp: dict, expected, trap) -> dict:
    sql = resp.get("sql") or ""
    res = {"sql": sql, "answer": resp.get("answer"), "row_count": resp.get("row_count"),
           "estates": resp.get("estates_used"), "sql_outcome": resp.get("sql_outcome"), "problems": []}
    if resp.get("error_type") or not sql:
        res["problems"].append(f"no answer ({resp.get('error_type') or 'no SQL'})")
        res["pass"] = False
        return res
    for pat in case.get("sql_must", []):
        if not re.search(pat, sql, re.I | re.S):
            res["problems"].append(f"SQL lacks /{pat}/")
    for pat in case.get("sql_must_not", []):
        if re.search(pat, sql, re.I | re.S):
            res["problems"].append(f"SQL has /{pat}/")
    res["warnings"] = [f"SQL has /{pat}/" for pat in case.get("sql_warn_not", [])
                       if re.search(pat, sql, re.I | re.S)]
    single = len(expected) == 1
    tol = float(case.get("rel_tol", 1e-6))
    figs = []
    for key, value in expected:
        cands = candidates(resp, key, single)
        ok = any(same(c, value, tol) for c in cands)
        wrong = None
        if not ok and trap:
            tv = dict(trap).get(key)
            if tv is not None and any(same(c, tv, tol) for c in cands):
                wrong = tv
        figs.append({"key": key, "expected": value, "ok": ok, "matches_trap": wrong})
        if not ok:
            res["problems"].append(
                f"{key}: expected {value:,.2f}" + (f", got the trap figure {wrong:,.2f}" if wrong is not None else ", not in the rows"))
    res["figures"] = figs
    nums = answer_numbers(resp.get("answer") or "")
    res["answer_has_figures"] = all(any(abs(n - v) <= 5e-4 * max(1.0, abs(v)) for n in nums) for _, v in expected)
    res["pass"] = not res["problems"]
    return res


def judge_route(case: dict, resp: dict) -> dict:
    """expect_route cases: only where the message was sent (e.g. asked back) counts."""
    want = case["expect_route"]
    got = resp.get("route")
    res = {"sql": resp.get("sql") or "", "answer": resp.get("answer"), "route": got,
           "sql_outcome": resp.get("sql_outcome"), "problems": []}
    if got != want:
        res["problems"].append(f"route {got!r}, expected {want!r}")
    res["pass"] = not res["problems"]
    return res


def judge_overview(case: dict, resp: dict, expected: dict | None) -> dict:
    """kind: overview. Each measure the answer shows is checked in its own column
    of the merged table against the case's reference for that measure."""
    rows = resp.get("rows") or []
    cols = resp.get("columns") or []
    parts = resp.get("parts") or []
    res = {"sql": resp.get("sql") or "", "answer": resp.get("answer"), "route": resp.get("route"),
           "row_count": resp.get("row_count"), "sql_outcome": resp.get("sql_outcome"),
           "measures": [p.get("measure") for p in parts], "problems": [], "warnings": []}
    if resp.get("route") != "overview" or resp.get("error_type"):
        res["problems"].append(f"not answered as an overview (route {resp.get('route')}, "
                               f"error {resp.get('error_type')})")
        res["pass"] = False
        return res
    with_rows = [p for p in parts if p.get("row_count")]
    if case.get("check") == "structure":
        if len(with_rows) < 2 or len(rows) < 2:
            res["problems"].append(f"{len(with_rows)} measure(s) with rows, {len(rows)} table row(s)")
        res["pass"] = not res["problems"]
        return res

    tol = float(case.get("rel_tol", 1e-6))
    checked, figs = 0, []
    for p in with_rows:
        mid, label = p.get("measure"), p.get("label")
        if mid not in (expected or {}):
            res["warnings"].append(f"{mid}: no reference")
            continue
        if label not in cols:
            res["warnings"].append(f"{mid}: shown apart from the table")
            continue
        checked += 1
        for key, value in expected[mid]:
            sel = [r for r in rows if any(isinstance(v, str) and key.lower() in v.lower() for v in r.values())]
            got = [float(r[label]) for r in sel if _num(r.get(label))]
            ok = any(same(g, value, tol) for g in got)
            figs.append({"measure": mid, "key": key, "expected": value, "got": got[:1], "ok": ok})
            if not ok and value:     # an estate with no data for a measure shows an empty cell
                res["problems"].append(f"{mid} {key}: expected {value:,.2f}, got {got[:1] or 'nothing'}")
    if checked < 2:
        res["problems"].append(f"only {checked} measure(s) could be checked")
    res["figures"] = figs
    res["pass"] = not res["problems"]
    return res


# ── main ───────────────────────────────────────────────────────────────────

def load(args) -> list[dict]:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    if args.only:
        want = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in want]
    if args.database != "all":
        cases = [c for c in cases if c["database"] == args.database]
    return cases


def check_cases(cases: list[dict]) -> int:
    """Run the reference and trap SQL: are the expected figures real, non-trivial
    and different from the wrong one?"""
    bad = 0
    for c in cases:
        if c["database"] == "staging" and not data_sources.get("staging").available:
            print(f"{c['id']:7} SKIP  staging not configured"); continue
        if c.get("measures"):
            for mid, sql in c["measures"].items():
                try:
                    exp = figures(c, sql)
                except Exception as e:
                    print(f"{c['id']:7} ERROR {mid}: {str(e)[:200]}"); bad += 1; continue
                zero = not exp or all(v == 0 for _, v in exp)
                bad += zero
                print(f"{c['id']:7} {'ALL-ZERO' if zero else 'ok   '} {mid:17} "
                      + ", ".join(f"{k} {v:,.2f}" for k, v in exp))
            continue
        if not c.get("reference_sql"):
            print(f"{c['id']:7} ok    (no figures: {c.get('expect_route') or c.get('check')})"); continue
        try:
            exp = figures(c, c["reference_sql"])
            trap = figures(c, c["trap_sql"]) if c.get("trap_sql") else None
        except Exception as e:
            print(f"{c['id']:7} ERROR {str(e)[:200]}"); bad += 1; continue
        notes = []
        if not exp or all(v == 0 for _, v in exp):
            notes.append("ALL-ZERO: a wrong query could pass"); bad += 1
        if trap and all(same(a[1], b[1]) for a, b in zip(exp, trap)):
            notes.append("TRAP == REFERENCE: this case cannot tell right from wrong"); bad += 1
        shown = ", ".join(f"{k} {v:,.2f}" for k, v in exp)
        tr = ", ".join(f"{k} {v:,.2f}" for k, v in trap) if trap else "-"
        print(f"{c['id']:7} ok    expected [{shown}]  trap [{tr}]" + ("  !! " + "; ".join(notes) if notes else ""))
    print(f"\n{len(cases)} cases checked, {bad} problem(s)")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only")
    ap.add_argument("--database", default="all", choices=["all", "live", "staging"])
    ap.add_argument("--check-cases", action="store_true")
    ap.add_argument("--min-pass", type=float, default=None, help="exit 1 if the overall pass rate is below this")
    ap.add_argument("--model", default=None, help="a key of config.CHAT_MODELS; default is automatic")
    ap.add_argument("--record", action="store_true",
                    help="let the runs be saved as few-shot examples (off by default: a test must not teach what it tests)")
    args = ap.parse_args()

    cases = load(args)
    if not cases:
        sys.exit("no cases selected")
    if args.check_cases:
        sys.exit(check_cases(cases))

    from fastapi.testclient import TestClient
    import chat_router
    import dashboard_server

    if not args.record:
        # The pipeline saves every query that merely runs as a "successful"
        # example for later questions, right or wrong. Keep the test out of that.
        chat_router.record_query = lambda *a, **k: None

    report, total, passed = [], 0, 0
    with TestClient(dashboard_server.app) as client:
        for c in cases:
            if c["database"] == "staging" and not data_sources.get("staging").available:
                print(f"{c['id']:7} SKIP  staging not configured"); continue
            if c.get("measures"):
                expected = {mid: figures(c, sql) for mid, sql in c["measures"].items()}
            else:
                expected = figures(c, c["reference_sql"]) if c.get("reference_sql") else None
            trap = figures(c, c["trap_sql"]) if c.get("trap_sql") else None
            runs = []
            for _ in range(args.runs):
                t0 = time.time()
                sid = str(uuid.uuid4())
                # A conversation: the earlier messages go first in the same chat,
                # and only the reply to the last one (`query`) is scored.
                said = []
                for msg in [*c.get("turns", []), c["query"]]:
                    r = client.post("/chat/message", json={
                        "message": msg, "database": c["database"], "model": args.model,
                        "session_id": sid})
                    resp = r.json() if r.status_code == 200 else {"error_type": f"http {r.status_code}"}
                    said.append({"q": msg, "a": (resp.get("answer") or "")[:300], "route": resp.get("route")})
                if c.get("expect_route"):
                    out = judge_route(c, resp)
                elif c.get("kind") == "overview":
                    out = judge_overview(c, resp, expected)
                else:
                    out = judge(c, resp, expected, trap)
                if c.get("turns"):
                    out["conversation"] = said
                out["seconds"] = round(time.time() - t0, 1)
                runs.append(out)
            ok = sum(1 for x in runs if x["pass"])
            total += len(runs); passed += ok
            variants = len({norm_sql(x["sql"]) for x in runs})
            problems = Counter(p for x in runs for p in x["problems"])
            warned = Counter(w for x in runs for w in x.get("warnings", []))
            print(f"{c['id']:7} {ok}/{len(runs)} pass  {variants} distinct SQL  [{c['category']}] {c['query'][:62]}")
            for p, n in problems.most_common(3):
                print(f"          x{n}  {p}")
            for w, n in warned.most_common(2):
                print(f"          x{n}  warning: {w}")
            report.append({"id": c["id"], "category": c["category"], "query": c["query"], "database": c["database"],
                           "expected": expected, "trap": trap, "pass_rate": ok / len(runs),
                           "distinct_sql": variants, "runs": runs})

    rate = passed / total if total else 0.0
    by_cat: dict[str, list[float]] = {}
    for r in report:
        by_cat.setdefault(r["category"], []).append(r["pass_rate"])
    print(f"\nOVERALL {passed}/{total} = {rate:.0%}   (flaky cases: "
          f"{[r['id'] for r in report if 0 < r['pass_rate'] < 1] or 'none'})")
    for cat, rates in by_cat.items():
        print(f"  {cat:26} {sum(rates) / len(rates):.0%}")

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {"run": stamp, "runs_per_case": args.runs, "overall_pass_rate": rate, "cases": report}
    for name in (f"golden_chat_{stamp}.json", "golden_chat_latest.json"):
        (RESULTS / name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"written evals/results/golden_chat_{stamp}.json")
    if args.min_pass is not None and rate < args.min_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
