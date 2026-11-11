import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path("./query_history.db")


_schema_ready = False


def _raw_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _connect() -> sqlite3.Connection:
    # Nothing calls init_db() at startup, so the first connection of the process
    # makes sure the table and its `source` column exist.
    global _schema_ready
    if not _schema_ready:
        _schema_ready = True
        init_db()
    return _raw_connect()


def init_db() -> None:
    with _raw_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                provider TEXT NOT NULL,
                tables_used TEXT,
                success INTEGER NOT NULL,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Which database the SQL was written for. Postgres and T-SQL examples
        # must never be mixed, so retrieval filters on it. Rows from before
        # this column existed were all written against the live databases.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(queries)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN source TEXT NOT NULL DEFAULT 'live'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_success ON queries(success)")
        conn.commit()
    log.info("query_history.db initialized at %s", _DB_PATH.resolve())


def record_query(
    question: str,
    sql: str,
    provider: str,
    tables_used: list[str],
    success: bool,
    error: str | None = None,
    source: str = "live",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO queries (question, sql, provider, tables_used, success, error, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                sql,
                provider,
                json.dumps(tables_used) if tables_used else None,
                1 if success else 0,
                error,
                source,
            ),
        )
        conn.commit()
        row_id = cur.lastrowid
    log.debug("Recorded query id=%d success=%s", row_id, success)
    return row_id


def set_success(query_id: int, success: bool) -> None:
    """Mark a stored query as (not) fit to be shown to the SQL writer as an example."""
    with _connect() as conn:
        conn.execute("UPDATE queries SET success = ? WHERE id = ?", (1 if success else 0, query_id))
        conn.commit()


def get_successful_pairs(limit: int = 500, source: str = "live") -> list[tuple[int, str, str]]:
    """Return (id, question, sql) for successful queries of one data source, most recent first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, question, sql FROM queries WHERE success = 1 AND source = ? "
            "ORDER BY id DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    return [(r["id"], r["question"], r["sql"]) for r in rows]
