"""Retire saved few-shot examples that break the data rules.

Every query that merely ran was once saved as a "successful" example for later
similar questions, right or wrong, so wrong SQL (the approval date as the
harvest date, deleted rows counted, tonnage summed per row) was copied and
reinforced. New queries are now checked before they are saved; this cleans up
what was saved before.

  python evals/purge_bad_examples.py            # show what would go
  python evals/purge_bad_examples.py --apply    # retire them (rows stay, success=0)

Safe to re-run. Also removes them from the Chroma example index, if it exists.
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import data_sources  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect("query_history.db")
    bad: dict[str, list[tuple[int, str]]] = {}
    seen = Counter()
    for qid, source, question, sql in db.execute(
            "SELECT id, source, question, sql FROM queries WHERE success = 1"):
        src = data_sources.registry().get(source)
        seen[source] += 1
        reason = src.lint(sql, question) if src and src.lint else None
        if reason:
            bad.setdefault(source, []).append((qid, reason.split(":")[1].split(".")[0].strip()[:60]))

    for source, rows in bad.items():
        why = Counter(r for _, r in rows)
        print(f"{source}: {len(rows)} of {seen[source]} saved examples break a rule")
        for r, n in why.most_common():
            print(f"   {n:4}  {r}")
    if not bad:
        print("nothing to retire")
        return
    if not args.apply:
        print("\n(dry run: add --apply to retire them)")
        return

    for source, rows in bad.items():
        ids = [i for i, _ in rows]
        db.executemany("UPDATE queries SET success = 0 WHERE id = ?", [(i,) for i in ids])
        try:
            import chromadb
            coll = chromadb.PersistentClient(path="./chroma_store").get_or_create_collection(
                data_sources.get(source).history_collection)
            coll.delete(ids=[str(i) for i in ids])
        except Exception as exc:
            print(f"  (could not update the example index for {source}: {exc}; it is rebuilt at next start)")
    db.commit()
    print("retired.")


if __name__ == "__main__":
    main()
