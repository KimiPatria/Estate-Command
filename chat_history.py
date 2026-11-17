"""Session-scoped chat history store for the EPMS chatbot.

Persists full conversation turns (question, answer, optional SQL + data rows)
per session in the same SQLite file as query_history.py. Rows are capped at
write time so the store — and any prompt built from it — stays bounded.
"""

import json
import logging
import uuid

from query_history import _connect

log = logging.getLogger(__name__)

# Max data rows persisted per turn. Anything beyond this was never shown to
# the LLM anyway (answer prompt previews 50), so storing more is waste.
# 200 = everything the chat pipeline fetches, so a reopened chat (or a "plot
# that" follow-up) has the same rows the answer was written from. Prompts
# replay far fewer (CHAT_HISTORY_MAX_ROWS).
MAX_STORED_ROWS = 200

_TITLE_LEN = 60


def init_chat_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                sql TEXT,
                columns TEXT,
                rows TEXT,
                row_count INTEGER DEFAULT 0,
                stats TEXT,
                route TEXT DEFAULT 'sql',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Added after the table first shipped, so existing databases need it too.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(chat_turns)")}
        if "meta" not in columns:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN meta TEXT")
        if "feedback" not in columns:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN feedback TEXT")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
        if "pinned" not in columns:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_session ON chat_turns(session_id, id)"
        )
        conn.commit()
    log.info("chat history tables ready")


def ensure_session(session_id: str | None, first_question: str) -> str:
    """Return a valid session id, creating the session row on first use."""
    sid = (session_id or "").strip() or str(uuid.uuid4())
    title = first_question.strip()[:_TITLE_LEN]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP",
            (sid, title or "New chat"),
        )
        conn.commit()
    return sid


def add_turn(
    session_id: str,
    question: str,
    answer: str,
    sql: str | None = None,
    columns: list[str] | None = None,
    rows: list[dict] | None = None,
    row_count: int = 0,
    stats: dict | None = None,
    route: str = "sql",
    meta: dict | None = None,
) -> int:
    """Store one turn and return its id. `meta` holds the conversation state
    around the answer: estates in play, the question a reply resolved to, the
    offered buttons, the steps taken and the chart shown."""
    capped = rows[:MAX_STORED_ROWS] if rows else None
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO chat_turns
                (session_id, question, answer, sql, columns, rows, row_count, stats, route, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                question,
                answer,
                sql,
                json.dumps(columns, default=str) if columns else None,
                json.dumps(capped, default=str) if capped else None,
                row_count,
                json.dumps(stats, default=str) if stats else None,
                route,
                json.dumps(meta, default=str) if meta else None,
            ),
        )
        conn.commit()
        return cur.lastrowid


def _parse_turn(r) -> dict:
    return {
        "id": r["id"],
        "feedback": r["feedback"],
        "question": r["question"],
        "answer": r["answer"],
        "sql": r["sql"],
        "columns": json.loads(r["columns"]) if r["columns"] else [],
        "rows": json.loads(r["rows"]) if r["rows"] else [],
        "row_count": r["row_count"] or 0,
        "stats": json.loads(r["stats"]) if r["stats"] else {},
        "route": r["route"] or "sql",
        "meta": json.loads(r["meta"]) if r["meta"] else {},
        "created_at": r["created_at"],
    }


def get_turns(session_id: str, limit: int = 20) -> list[dict]:
    """Most recent turns of a session, oldest first. Each carries `pos`, its
    place in the whole session (0 = first), so a window of turns can be cut
    at the same places whatever `limit` was asked for (chat_intent.window)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY id) - 1 AS pos "
            "FROM chat_turns WHERE session_id = ?) ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    turns = []
    for r in reversed(rows):
        t = _parse_turn(r)
        t["pos"] = r["pos"]
        turns.append(t)
    return turns


def list_sessions(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.created_at, s.updated_at, s.pinned,
                   COUNT(t.id) AS turn_count
            FROM chat_sessions s
            LEFT JOIN chat_turns t ON t.session_id = s.id
            GROUP BY s.id
            HAVING turn_count > 0
            ORDER BY s.pinned DESC, s.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_session(session_id: str, title: str | None = None,
                   pinned: bool | None = None) -> bool:
    """Rename and/or pin a chat. Leaves updated_at alone: tidying the sidebar
    should not move a chat to the top of it."""
    sets, args = [], []
    if title is not None:
        title = " ".join(title.split())[:_TITLE_LEN]
        if title:
            sets.append("title = ?")
            args.append(title)
    if pinned is not None:
        sets.append("pinned = ?")
        args.append(1 if pinned else 0)
    if not sets:
        return False
    with _connect() as conn:
        cur = conn.execute(f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ?",
                           (*args, session_id))
        conn.commit()
    return cur.rowcount > 0


def delete_last_turn(session_id: str) -> bool:
    """Drop the newest turn of a chat: the user is regenerating the answer or
    has edited their last message, and the old exchange must not stay in the
    history the next answer is written from."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM chat_turns WHERE id = (SELECT MAX(id) FROM chat_turns WHERE session_id = ?)",
            (session_id,),
        )
        conn.commit()
    return cur.rowcount > 0


def set_feedback(session_id: str, turn_id: int, rating: str | None) -> dict | None:
    """Store a thumbs up/down on an answer. Returns the turn's meta, or None
    if that turn is not in that chat."""
    with _connect() as conn:
        row = conn.execute("SELECT meta FROM chat_turns WHERE id = ? AND session_id = ?",
                           (turn_id, session_id)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE chat_turns SET feedback = ? WHERE id = ?", (rating, turn_id))
        conn.commit()
    return json.loads(row["meta"]) if row["meta"] else {}


def delete_session(session_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        conn.execute("DELETE FROM chat_turns WHERE session_id = ?", (session_id,))
        conn.commit()
    return cur.rowcount > 0


# Tables must exist before the first request hits the router.
init_chat_db()
