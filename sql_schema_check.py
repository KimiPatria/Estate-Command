"""Does every table and column a generated query names exist? Checked before it runs.

A misspelt or invented column (SUM(bunches_rip), JJGX) otherwise costs a round
trip to the database and comes back as a driver error the SQL writer has to
decode. Here the query is parsed in its own dialect (sqlglot), every table is
looked up in the source's metadata cards and every column is resolved against
those tables, so the writer is told exactly which name is wrong and which real
columns are closest.

Fails open: SQL that sqlglot cannot parse or resolve for any other reason is
passed on to the database as before. Only "this name does not exist" is a
finding.

    python sql_schema_check.py --replay      # findings on every saved example (false-positive check)
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Optional

from sqlglot import exp, parse_one
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

import data_sources
import metadata_loader

log = logging.getLogger(__name__)

_DIALECT = {"postgresql": "postgres", "mssql": "tsql"}
_UNRESOLVED = re.compile(r"Column '([^']+)' could not be resolved", re.IGNORECASE)


def _schema(source_id: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """{table: {column: type}} in lower case, plus lower -> name as written in the cards."""
    schema: dict[str, dict[str, str]] = {}
    shown: dict[str, str] = {}
    for name, card in metadata_loader.cards(source_id).items():
        cols = {}
        for c in card.columns:
            cols[c.name.lower()] = "TEXT"
            shown[c.name.lower()] = c.name
        schema[name.lower()] = cols
        shown[name.lower()] = name
    return schema, shown


def check(sql: str, dialect: str = "postgresql", source_id: Optional[str] = None) -> Optional[str]:
    """A message for the SQL writer naming what does not exist, or None."""
    src_id = source_id or data_sources.active().id
    schema, shown = _schema(src_id)
    if not schema or not sql:
        return None
    read = _DIALECT.get(dialect, dialect)
    try:
        tree = parse_one(sql, read=read)
    except (ParseError, Exception) as exc:            # noqa: BLE001 — fail open
        log.debug("[schema] could not parse, not checked: %s", exc)
        return None

    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    unknown: list[str] = []
    used: list[str] = []
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in ctes:
            continue
        if name in schema:
            used.append(name)
        elif table.name not in unknown:
            unknown.append(table.name)
        # dbo.OPH, public.t_oph: the cards are keyed by table name alone.
        table.set("db", None)
        table.set("catalog", None)
    if unknown:
        close = sorted({shown[t] for u in unknown
                        for t in difflib.get_close_matches(u.lower(), list(schema), n=3, cutoff=0.6)})
        return (f"Wrong query: table {', '.join(unknown)} does not exist. "
                + (f"Closest real tables: {', '.join(close)}. " if close else "")
                + "Use only tables that appear in the DDL.")

    try:
        qualify(tree, schema=schema, dialect=read, validate_qualify_columns=True,
                quote_identifiers=False, identify=False)
    except OptimizeError as exc:
        m = _UNRESOLVED.search(str(exc))
        if not m:
            log.debug("[schema] not checked: %s", exc)
            return None
        col = m.group(1)
        tables = list(dict.fromkeys(used))
        pool = sorted({c for t in tables for c in schema.get(t, {})})
        close = [shown[c] for c in difflib.get_close_matches(col.lower(), pool, n=4, cutoff=0.5)]
        where = ", ".join(shown[t] for t in tables) or "the tables queried"
        return (f"Wrong query: column {col} does not exist in {where}. "
                + (f"Closest real columns: {', '.join(close)}. " if close else "")
                + "Use only columns listed in the DDL, and qualify a column with its table alias "
                  "when several tables are joined.")
    except Exception as exc:                           # noqa: BLE001 — fail open
        log.debug("[schema] not checked: %s", exc)
        return None
    return None


def _replay() -> int:
    """Run the check over every saved, successful example: they all executed,
    so any finding here is a false positive (or a table the index hides)."""
    import sqlite3
    from collections import Counter

    db = sqlite3.connect("query_history.db")
    flagged = 0
    for src in data_sources.available():
        metadata_loader.load_all_cards(source=src)
        rows = db.execute("SELECT id, sql FROM queries WHERE success = 1 AND source = ?", (src.id,)).fetchall()
        why = Counter()
        for qid, sql in rows:
            finding = check(sql, src.dialect, src.id)
            if finding:
                flagged += 1
                why[finding[:110]] += 1
                print(f"  {src.id} #{qid}: {finding[:160]}")
        print(f"{src.id}: {sum(why.values())} of {len(rows)} executed examples flagged")
    return flagged


if __name__ == "__main__":
    import argparse
    import os
    import sys
    from pathlib import Path

    os.chdir(Path(__file__).parent)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()
    if args.replay:
        sys.exit(1 if _replay() else 0)
    ap.print_help()
