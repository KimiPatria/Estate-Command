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
        # Where an example came from, and whether a person (or a hand-written
        # seed) vouches for it. Retrieval prefers verified examples:
        #   auto   a chat query that passed every check, nobody has rated it
        #   rated  a chat query whose answer got a thumbs-up
        #   seed   a hand-written pair from examples/seed_<source>.yaml
        if "origin" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN origin TEXT NOT NULL DEFAULT 'auto'")
        if "verified" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        if "row_count" not in cols:
            conn.execute("ALTER TABLE queries ADD COLUMN row_count INTEGER")
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
    row_count: int | None = None,
    origin: str = "auto",
    verified: bool = False,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO queries (question, sql, provider, tables_used, success, error, source,
                                 row_count, origin, verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                sql,
                provider,
                json.dumps(tables_used) if tables_used else None,
                1 if success else 0,
                error,
                source,
                row_count,
                origin,
                1 if verified else 0,
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


def set_verified(query_id: int) -> None:
    """A person vouched for this query's answer (thumbs-up): rank it as verified."""
    with _connect() as conn:
        conn.execute(
            "UPDATE queries SET success = 1, verified = 1, "
            "origin = CASE WHEN origin = 'seed' THEN 'seed' ELSE 'rated' END WHERE id = ?",
            (query_id,))
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


def get_example(query_id: int) -> tuple[int, str, str, int, str] | None:
    """(id, question, sql, verified, origin) of one usable example, or None."""
    with _connect() as conn:
        r = conn.execute("SELECT id, question, sql, verified, origin FROM queries "
                         "WHERE id = ? AND success = 1", (query_id,)).fetchone()
    return (r["id"], r["question"], r["sql"], r["verified"], r["origin"]) if r else None


def get_example_rows(limit: int = 500, source: str = "live") -> list[tuple[int, str, str, int, str]]:
    """(id, question, sql, verified, origin) for usable examples of one source:
    verified ones first, then the most recent."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, question, sql, verified, origin FROM queries WHERE success = 1 AND source = ? "
            "ORDER BY verified DESC, id DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    return [(r["id"], r["question"], r["sql"], r["verified"], r["origin"]) for r in rows]


def find_pair(question: str, sql: str, source: str) -> int | None:
    """Id of a stored pair with exactly this question and SQL, for idempotent seeding."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM queries WHERE question = ? AND sql = ? AND source = ? LIMIT 1",
            (question, sql, source)).fetchone()
    return row["id"] if row else None
