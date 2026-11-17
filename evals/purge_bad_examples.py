"""Retire saved few-shot examples that break the data rules.

Every query that merely ran was once saved as a "successful" example for later
similar questions, right or wrong, so wrong SQL (the approval date as the
harvest date, deleted rows counted, tonnage summed per row) was copied and
reinforced. New queries are now checked before they are saved; this cleans up
what was saved before.

  python evals/purge_bad_examples.py                  # show what would go
  python evals/purge_bad_examples.py --rerun-empty    # also re-run unrated examples, flag empty ones
  python evals/purge_bad_examples.py --apply          # retire them (rows stay, success=0)

An example goes when it breaks a data rule (lint), names a table or column that
does not exist (or that the index now hides, such as t_abw), or, with
--rerun-empty, when an unrated example (origin=auto) now returns no rows on any
estate. Seeds and thumbs-up examples are never retired for coming back empty.

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


def _empty_everywhere(src, sql: str) -> bool:
    from sqlalchemy import text
    from sql_validator import ensure_limit
    for sid in src.scope_order:
        try:
            with src.scopes[sid]["engine"].connect() as conn:
                if conn.execute(text(ensure_limit(sql, 5, src.dialect))).fetchmany(1):
                    return False
        except Exception:
            continue                  # an error is the schema check's business, not this one's
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rerun-empty", action="store_true")
    args = ap.parse_args()

    import chat_router
    import metadata_loader
    import sql_schema_check

    for src in data_sources.available():
        metadata_loader.load_all_cards(source=src)

    db = sqlite3.connect("query_history.db")
    cols = {r[1] for r in db.execute("PRAGMA table_info(queries)")}
    origin_col = "origin" if "origin" in cols else "'auto'"
    bad: dict[str, list[tuple[int, str]]] = {}
    seen = Counter()
    for qid, source, question, sql, origin in db.execute(
            f"SELECT id, source, question, sql, {origin_col} FROM queries WHERE success = 1"):
        src = data_sources.registry().get(source)
        seen[source] += 1
        if not src:
            continue
        reason = src.lint(sql, question) if src.lint else None
        if reason:
            reason = reason.split(":")[1].split(".")[0].strip()[:60]
        elif src.available and sql_schema_check.check(sql, src.dialect, src.id):
            reason = "names a table or column that does not exist"
        elif (args.rerun_empty and origin == "auto" and src.available
              # "yesterday", "last 3 months": empty because the data stops earlier, not because the SQL is wrong
              and not chat_router._RECENT.search(question) and _empty_everywhere(src, sql)):
            reason = "returns no rows on any estate"
        if reason:
            bad.setdefault(source, []).append((qid, reason))

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
    db.commit()
    # The Chroma example index is not edited here: deleting ids in place has
    # corrupted it. The server rebuilds it from the database at every start.
    print("retired. Restart the server to rebuild the example index.")


if __name__ == "__main__":
    main()
