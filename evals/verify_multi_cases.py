"""Verify evals/sql_cases_multi.yaml against the live estate databases.

No LLM calls. For every case, executes expected_sql on each in-scope estate
(config.ESTATES), flags results that would let a wrong query pass by luck
(empty, all NULL, all zero), and for kind=combined computes the expected
single answer from the per-estate components.

Usage (Anaconda python, repo root):
  python evals/verify_multi_cases.py
  python evals/verify_multi_cases.py --only mx_018
"""
import argparse
import os
import sys
from pathlib import Path

import yaml
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from config import ESTATES, ESTATE_ORDER  # noqa: E402

CASES = ROOT / "evals" / "sql_cases_multi.yaml"


def run(eid: str, sql: str):
    with ESTATES[eid]["engine"].connect() as conn:
        res = conn.execute(text(sql))
        cols = list(res.keys())
        rows = [dict(zip(cols, r)) for r in res.fetchall()]
    return cols, rows


def num(v):
    return float(v) if v is not None else 0.0


def trivial(rows: list[dict]) -> str | None:
    if not rows:
        return "EMPTY"
    vals = [v for r in rows for v in r.values() if isinstance(v, (int, float)) or hasattr(v, "is_finite")]
    if vals and all(num(v) == 0 for v in vals):
        return "ALL-ZERO"
    if all(v is None for r in rows for v in r.values()):
        return "ALL-NULL"
    return None


def combine(spec: dict, per: dict[str, list[dict]]) -> str:
    t = spec["type"]
    if t == "sum":
        parts = {e: num(rows[0][spec["col"]]) for e, rows in per.items()}
        return f"{sum(parts.values()):,.2f}  (parts {parts})"
    if t == "ratio":
        n = sum(num(rows[0][spec["num"]]) for rows in per.values())
        d = sum(num(rows[0][spec["den"]]) for rows in per.values())
        pooled = n * spec.get("scale", 1) / d if d else float("nan")
        naive = [num(rows[0][spec["num"]]) * spec.get("scale", 1) / num(rows[0][spec["den"]])
                 for rows in per.values() if num(rows[0][spec["den"]])]
        wrong = sum(naive) / len(naive) if naive else float("nan")
        return f"pooled {pooled:,.4f}  (naive mean of estate ratios would be {wrong:,.4f})"
    if t == "winner":
        parts = {e: num(rows[0][spec["col"]]) for e, rows in per.items()}
        ranked = sorted(parts.items(), key=lambda kv: -kv[1])
        (w, wv), (l, lv) = ranked[0], ranked[1]
        return f"{w} wins, {wv:,.0f} vs {l} {lv:,.0f}  (+{(wv - lv) / lv * 100:.1f}%)"
    if t == "peak_month":
        tot: dict = {}
        per_peak = {}
        for e, rows in per.items():
            best = max(rows, key=lambda r: num(r[spec["col"]]))
            per_peak[e] = int(best["month"])
            for r in rows:
                tot[int(r["month"])] = tot.get(int(r["month"]), 0) + num(r[spec["col"]])
        m = max(tot, key=tot.get)
        return f"combined peak month {m} ({tot[m]:,.0f}); per-estate peaks {per_peak}"
    if t == "topn_union":
        rows = [{**r, "_e": e} for e, rs in per.items() for r in rs]
        rows.sort(key=lambda r: -num(r[spec["order"]]))
        top = rows[: spec["n"]]
        return "; ".join(f"{r['_e']}:{'/'.join(str(r[k]) for k in spec['key'])}={num(r[spec['order']]):,.0f}"
                         for r in top)
    return f"unknown combine type {t}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    args = ap.parse_args()
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    bad = 0
    for c in cases:
        if args.only and c["id"] != args.only:
            continue
        scope = ESTATE_ORDER if c["scope"] == "all" else [c["scope"]]
        per, problems = {}, []
        for e in scope:
            try:
                cols, rows = run(e, c["expected_sql"])
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{e}: SQL ERROR {str(exc).splitlines()[0][:120]}")
                continue
            per[e] = rows
            t = trivial(rows)
            if t:
                problems.append(f"{e}: {t}")
        counts = {e: len(r) for e, r in per.items()}
        status = "FAIL" if problems else "ok  "
        bad += bool(problems)
        print(f"{status} {c['id']} [{c['category']}/{c['kind']}] rows={counts}")
        for p in problems:
            print(f"       ! {p}")
        if not problems and c["kind"] == "combined":
            print(f"       expected: {combine(c['combine'], per)}")
    print(f"\n{len(cases) - bad if not args.only else '-'} ok, {bad} with problems")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
