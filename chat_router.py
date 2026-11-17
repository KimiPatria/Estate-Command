"""
EPMS Data Chatbot — FastAPI router
Mounted at /chat on dashboard_server.py (port 8001).

POST /chat/message — conversational Q&A backed by the EPMS database.
Session-scoped context: a lightweight router decides per message whether to
answer from conversation history (no SQL) or run the full pipeline:
retrieve tables → generate SQL → execute → reason → respond.
"""

import asyncio
import contextvars
import decimal
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import chat_charts
import chat_events
import chat_forecast
import chat_intent
import chat_overview
from chat_conversation import (
    ROUTE_CHAT,
    ROUTE_CLARIFY,
    carried_estates,
    chart_followup,
    clarify_gate,
    is_confirmation,
    is_overview,
    is_smalltalk,
    names_period,
    pipeline_turns,
    resolve_confirmation,
    resolve_reply,
    retrieval_context,
    scope_after,
    suggest_followups,
    widens_scope,
)
from chat_history import (
    add_turn,
    delete_last_turn,
    delete_session,
    ensure_session,
    get_turns,
    list_sessions,
    set_feedback,
    update_session,
)
from config import (
    engine,
    LLM_PROVIDER,
    CHAT_HISTORY_DATA_TURNS,
    CHAT_HISTORY_MAX_ROWS,
    CHAT_HISTORY_TEXT_TURNS,
    CHAT_DEFAULT_MODEL,
    CHAT_ESCALATE_TO_REASONING,
    CHAT_MODELS,
    CHAT_SCHEMA_CHECK,
    CHAT_SQL_FIX_ATTEMPTS,
    ESTATE_ORDER,
    ESTATES,
    RAG_SOURCE_DEFAULT,
)
import data_sources
from error_handler import is_off_domain, known_on_domain
from estates import detect_estate_mentions, strip_estate_mentions
from guardrails import apply_input_guardrails, apply_output_guardrails
import llm_client
from llm import call_llm, get_provider, PROVIDER_LABELS
from model_routing import answer_tier
from prompt_builder import build_sql_messages, extract_sql
from prompts import load_prompt
from query_history import record_query, set_success, set_verified
from rag_sources import SessionContext, SqlRagSource, VALID_SOURCES, dispatch as dispatch_rag
from retrieval import retrieve_tables, retrieve_examples
import sql_schema_check
from sql_validator import validate, ensure_limit

log = logging.getLogger("epms-chat")

_CHAT_ANSWER_SYSTEM_PROMPT = load_prompt("chat_answer_system")

_CONTEXT_ANSWER_SYSTEM_PROMPT = load_prompt("chat_context_system")

_ROUTER_SYSTEM_PROMPT = load_prompt("chat_followup_router_system")


def _build_answer_messages(
    question: str,
    sql: str,
    rows: list[dict],
    columns: list[str],
    stats: dict,
    estate_breakdown: Optional[dict] = None,
    failed_estates: Optional[list[str]] = None,
    data_through: Optional[str] = None,
    period_note: Optional[str] = None,
    notes: Optional[list[str]] = None,
) -> list[dict]:
    preview = rows[:50]
    preview_json = json.dumps(preview, default=str, ensure_ascii=False)
    stats_json   = json.dumps(stats,   default=str, ensure_ascii=False)
    extra = ""
    derived = _derived_facts(columns, rows)
    if derived:
        extra += ("\nDerived facts (computed exactly; quote these, do not calculate your own): "
                  + json.dumps(derived, default=str, ensure_ascii=False))
    if estate_breakdown:
        breakdown_json = json.dumps(estate_breakdown, default=str, ensure_ascii=False)
        extra += f"\nPer-estate breakdown (each estate's own row count + stats): {breakdown_json}"
    if failed_estates:
        extra += (
            f"\nNote: data from {', '.join(failed_estates)} could not be "
            f"retrieved and is excluded from the figures above."
        )
    if data_through:
        extra += (
            f"\nNote: this database holds data only up to {data_through}. If the question "
            f"is about a period after that (today, yesterday, this week) and the result is "
            f"empty, say that the data ends on {data_through} rather than that nothing happened."
        )
    if period_note:
        extra += f"\nPeriod: {period_note}"
    for note in notes or []:
        extra += f"\nNote: {note}"
    return [
        {"role": "system", "content": _CHAT_ANSWER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"SQL executed:\n{sql}\n\n"
                f"Columns: {columns}\n"
                f"Total rows returned: {len(rows)}\n"
                f"Summary stats: {stats_json}{extra}\n\n"
                f"Rows (first 50): {preview_json}"
            ),
        },
    ]


_LAST_12_FULL_MONTHS = re.compile(
    r"dateadd\(month,-12,datefromparts\(year\(getdate\(\)\),month\(getdate\(\)\),1\)\)")


def _period_note(sql: str, today: Optional[date] = None) -> str:
    """For a question that named no period: make the answer say which one ran.

    The query picks a period on its own (it once chose calendar 2025, which
    holds 12 months of Estate C but 7 of Estate A) and the reader cannot tell
    unless the answer says so. The default of the staging rules is resolved to
    dates here; anything else the answer model reads off the SQL.
    """
    today = today or date.today()
    note = ("the question named no period, so the query chose one. In the opening sentence, "
            "say which period the figures cover, read from the date filter in the SQL "
            f"(today is {today.isoformat()}); if the SQL has no date filter, say they cover all the data held.")
    window = _default_window(sql, today)
    if window:
        note += f" Here it is the {window}."
    return note


def _default_window(sql: str, today: Optional[date] = None) -> Optional[str]:
    """"last 12 full months: 1 Nov 2025 to 31 Oct 2026" when the SQL used the
    staging rules' default period, else None."""
    if not _LAST_12_FULL_MONTHS.search(re.sub(r"\s+", "", sql.lower())):
        return None
    end = (today or date.today()).replace(day=1)
    start = end.replace(year=end.year - 1)
    last = date.fromordinal(end.toordinal() - 1)
    return f"last 12 full months: {start.day} {start:%b %Y} to {last.day} {last:%b %Y}"


def _with_period(answer: str, question: str, sql: str) -> str:
    """The answer model names the period only some of the time (2 of 5 runs),
    so when the question gave none and the default ran, the line is added here."""
    window = None if names_period(question) else _default_window(sql)
    if not window or re.search(r"\b12\s+(?:full\s+)?months?\b|\b12\s+bulan\b", answer, re.IGNORECASE):
        return answer
    return f"{answer.rstrip()}\n\n_Covers the {window}, as no period was asked for._"


router = APIRouter(prefix="/chat", tags=["chat"])


# ── request / response models ──────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    history: list[ChatMessage] = []  # legacy field — server-side history is authoritative
    source: Optional[str] = None     # "sql" | "knowledge_base" | "both"; defaults to RAG_SOURCE_DEFAULT
    model: Optional[str] = None      # key into config.CHAT_MODELS; None = automatic (default model, escalating)
    database: Optional[str] = None   # key of a data_sources source ("live" | "staging"); None = live
    # Regenerate / edit: drop the chat's newest turn first, so this message
    # replaces that exchange instead of following it.
    replace_last: bool = False


class ChatResponse(BaseModel):
    answer: str
    session_id: Optional[str] = None
    route: str = "sql"          # "sql" | "context" | "knowledge_base" | "both" | "clarify"
    source: str = "sql"         # which RAG source(s) actually served this answer
    database: str = "live"      # which database the SQL ran against (data_sources id)
    database_label: Optional[str] = None
    options: list[str] = []     # clarify: choices the user can click
    suggestions: list[str] = [] # answer: next questions the user can click
    estates_carried: list[str] = []  # estate labels kept from the previous turn
    sql: Optional[str] = None
    columns: list[str] = []
    rows: list[dict] = []
    row_count: int = 0
    tables_used: list[str] = []
    citations: list[dict] = []  # knowledge_base/both: retrieved chunk excerpts
    provider: Optional[str] = None
    provider_label: Optional[str] = None
    used_llm_fallback: bool = False
    error: Optional[str] = None
    error_type: Optional[str] = None
    estates_used: list[str] = []    # which estate(s) contributed to this answer
    estates_failed: list[str] = []  # estate(s) whose query failed and were excluded
    chart: Optional[dict] = None    # chat_charts payload: labels + series, drawn by the page
    steps: list[dict] = []          # what the pipeline did, in order (chat_events)
    turn_id: Optional[int] = None   # id of the stored turn, for feedback
    elapsed_ms: int = 0
    sql_outcome: Optional[str] = None   # generate_and_run outcome: first_ok, fixed_lint, kept_empty ...
    parts: list[dict] = []          # overview: one entry per measure {measure, label, question, sql, outcome, row_count}


class FeedbackRequest(BaseModel):
    session_id: str
    turn_id: int
    rating: Optional[str] = None    # "up" | "down" | None to clear


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None


@router.get("/models")
def list_models() -> dict:
    """Models the chat page can switch between, for its selector."""
    return {
        "models": [{"key": k, "label": v["label"], "id": v["id"]} for k, v in CHAT_MODELS.items()],
        "default": CHAT_DEFAULT_MODEL if CHAT_DEFAULT_MODEL in CHAT_MODELS else next(iter(CHAT_MODELS)),
    }


@router.get("/databases")
def list_databases() -> dict:
    """Databases the chat page can switch between, for its selector.

    A database that isn't configured (no connection string) is still listed, as
    unavailable, so the page can say why it can't be picked.
    """
    return {
        "databases": [
            {
                "key": s.id,
                "label": s.label,
                "description": s.description,
                "dialect": s.dialect,
                "available": s.available,
                "estates": s.entity_labels(),
                "starters": s.starters,
            }
            for s in data_sources.registry().values()
        ],
        "default": data_sources.LIVE,
    }


# ── helpers ────────────────────────────────────────────────────────────────

def _scope_label(src: "data_sources.DataSource", eid: str) -> str:
    """Display name of a scope id: an estate code on staging, an estate engine on live."""
    return src.entities[eid]["label"] if eid in src.entities else src.scopes[eid]["label"]


def _coerce(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return str(v)
    if isinstance(v, (memoryview, bytes, bytearray)):
        return None
    return v


# Queries running at once on one database. The staging SQL Server is a single
# server: four overview parts at once ran into its 15 s query timeout, where
# each alone takes about a second. Waiting for a slot does not count against
# the timeout.
_DB_CONCURRENCY = 2
_db_slots: dict[int, threading.BoundedSemaphore] = {}
_db_slots_lock = threading.Lock()


def _db_slot(engine_obj) -> threading.BoundedSemaphore:
    with _db_slots_lock:
        return _db_slots.setdefault(id(engine_obj), threading.BoundedSemaphore(_DB_CONCURRENCY))


def _execute_safe(
    sql: str, max_rows: int = 200, engine_obj=None, dialect: str = "postgresql"
) -> tuple[list[str], list[dict], Optional[str]]:
    # `dialect` is passed in rather than read from data_sources.active(): this
    # runs on pool threads, which don't inherit the request's ContextVar.
    engine_obj = engine if engine_obj is None else engine_obj
    try:
        limited = ensure_limit(sql, max_rows, dialect)
        with _db_slot(engine_obj), engine_obj.connect() as conn:
            result = conn.execute(text(limited))
            columns = list(result.keys())
            # fetchmany, not fetchall: T-SQL has no trailing LIMIT, so the cap
            # on the rows we read is what keeps a runaway query small.
            rows = [
                {k: _coerce(v) for k, v in dict(r._mapping).items()}
                for r in result.fetchmany(max_rows)
            ]
        return columns, rows, None
    except SQLAlchemyError as exc:
        err = str(exc.orig) if hasattr(exc, "orig") and exc.orig else str(exc)
        log.warning("[chat] SQL error: %s", err[:300])
        return [], [], err
    except Exception as exc:
        log.warning("[chat] unexpected error: %s", exc)
        return [], [], str(exc)


def _execute_multi(
    sql: str, estate_ids: list[str], max_rows: int = 200
) -> dict[str, tuple[list[str], list[dict], Optional[str]]]:
    """Run the same SQL against each estate's engine concurrently.

    Safe because each estate has its own SQLAlchemy Engine/pool (config.ESTATES)
    — concurrent .connect() calls across distinct engines don't share state.
    """
    src = data_sources.active()
    results: dict[str, tuple[list[str], list[dict], Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=len(estate_ids)) as pool:
        futures = {
            pool.submit(_execute_safe, sql, max_rows, src.scopes[eid]["engine"], src.dialect): eid
            for eid in estate_ids
        }
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return results


def _answer_llm(messages: list[dict], *, max_tokens: int, task: str, tier: str = "main"
                ) -> tuple[str, str, dict]:
    """call_llm() for text the user reads. When the page is watching the request
    (chat_events.streaming) the words are sent as the model writes them."""
    if chat_events.streaming() and LLM_PROVIDER == "bedrock":
        result = llm_client.chat_stream(
            messages, on_delta=lambda piece: chat_events.send("delta", text=piece),
            tier=tier, temperature=0, max_tokens=max_tokens, task=task,
        )
        return result.text, "bedrock", result.usage
    return call_llm(messages, max_tokens=max_tokens, temperature=0, task=task, tier=tier)


def _show_result(columns: list[str], rows: list[dict], row_count: int) -> None:
    """Put the result table on the page before the answer is written."""
    chat_events.send("table", columns=columns, rows=rows[:200], row_count=row_count)


def _start_chart(question: str, columns: list[str], rows: list[dict]):
    """Plan the chart in the background while the answer is being written.

    The chart is attached to the request (and sent to the page) the moment it
    is ready. Returns a future to wait on before the request ends.
    """
    rec = chat_events.current()
    ctx = contextvars.copy_context()     # carries the data source, model choice and recorder

    def work():
        chart = chat_charts.plan_chart(question, columns, rows)
        if chart and rec is not None:
            rec.extras["chart"] = chart
            try:
                rec.send("chart", chart)
            except chat_events.Cancelled:
                pass
        return chart

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ctx.run, work)
    pool.shutdown(wait=False)
    return future


def _wait_chart(future) -> None:
    try:
        future.result(timeout=12)
    except Exception as exc:
        log.info("[chat] chart not ready: %s", exc)


def _derived_facts(columns: list[str], rows: list[dict]) -> dict:
    """Comparisons worked out exactly, for the answer to quote.

    The model is good at saying what matters and bad at arithmetic: left to
    itself it wrote "about 7 times fewer" for a ratio of 13.7. So the usual
    comparisons (who is highest and lowest, by how much, each one's share,
    the change from first to last) are computed here and handed over.
    """
    if not 2 <= len(rows) <= 200:
        return {}
    texts = [c for c in columns if any(isinstance(r.get(c), str) for r in rows)]

    def is_num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def name(r: dict) -> str:
        return " / ".join(str(r.get(c)) for c in texts[:3] if r.get(c) not in (None, "")) or "?"

    out: dict = {}
    # Same reading of the columns as the chart: a year, a month number or a
    # code is a label, "ripe_bunches_last_month" is a measure.
    kinds = chat_charts.profile(columns, rows)[2]
    measures = [c for c in columns if kinds.get(c) == "number"
                and sum(is_num(r.get(c)) for r in rows) >= 2]
    for col in measures[:4]:
        vals = [(name(r), float(r[col])) for r in rows if is_num(r.get(col))]
        total = sum(v for _, v in vals)
        ranked = sorted(vals, key=lambda x: -x[1])
        hi, lo = ranked[0], ranked[-1]
        d: dict = {
            "sum_of_these_rows": round(total, 2),
            "highest": {"which": hi[0], "value": round(hi[1], 2)},
            "lowest": {"which": lo[0], "value": round(lo[1], 2)},
            "highest_minus_lowest": round(hi[1] - lo[1], 2),
        }
        if lo[1] > 0:
            d["highest_is_x_times_lowest"] = round(hi[1] / lo[1], 1)
        if len(ranked) >= 3:
            d["second_highest"] = {"which": ranked[1][0], "value": round(ranked[1][1], 2)}
            d["highest_minus_second"] = round(hi[1] - ranked[1][1], 2)
        if total > 0 and len(vals) <= 12 and all(v >= 0 for _, v in vals):
            d["share_of_sum_pct"] = {n: round(v / total * 100, 1) for n, v in vals}
        first, last = vals[0], vals[-1]
        d["first_row_to_last_row_change"] = round(last[1] - first[1], 2)
        if first[1]:
            d["first_row_to_last_row_change_pct"] = round((last[1] - first[1]) / abs(first[1]) * 100, 1)
        out[col] = d
    return out


def _compute_stats(columns: list[str], rows: list[dict]) -> dict:
    stats: dict = {}
    for col in columns:
        vals = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
        if vals:
            stats[col] = {
                "count": len(vals),
                "sum": round(sum(vals), 4),
                "min": min(vals),
                "max": max(vals),
                "avg": round(sum(vals) / len(vals), 4),
            }
    return stats


_OFF_DOMAIN_ANSWER = (
    "I can only answer questions about your EPMS plantation data — "
    "harvest records, employee information, blocks, estates, and related topics. "
    "Please try asking something related to your plantation operations."
)

_NO_DATA_ANSWER = (
    "I wasn't able to find relevant data for that question. "
    "Try rephrasing or asking about a specific estate, block, employee, or harvest period."
)

_SQL_FAIL_ANSWER = (
    "I found relevant data tables but couldn't produce a working query for that question. "
    "Try rephrasing with more specific terms, such as an estate code, date range, or metric name."
)


# ── conversational context ─────────────────────────────────────────────────

def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _route_followup(message: str, turns: list[dict]) -> str:
    """Decide 'context' vs 'sql' with the cheap routing model.

    Sees text Q&A only (answers truncated) plus a one-line note about which
    turns still have data rows available. Falls back to 'sql' on any failure.
    On /chat the intent router (chat_intent.classify) has usually decided
    already, and its decision stands.
    """
    preset = chat_intent.preset_route()
    if preset:
        return preset
    if chart_followup(message, turns):
        return "context"
    if widens_scope(message):
        return "sql"          # the rows on screen were scoped narrower than this asks
    lines: list[str] = []
    for t in turns[-CHAT_HISTORY_TEXT_TURNS:]:
        lines.append(f"Q: {_truncate(t['question'], 200)}")
        lines.append(f"A: {_truncate(t['answer'], 240)}")
        if t["rows"]:
            lines.append(
                f"[data fetched for this turn: columns {t['columns']}, "
                f"{len(t['rows'])} of {t['row_count']} rows available]"
            )
    user_content = "\n".join(lines) + f"\n\nNew question: {message}"

    decided = llm_client.chat_json(
        [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        name="route", schema=_ROUTE_SCHEMA,
        description="Record whether the new question is answered from the conversation or a new query.",
        tier="routing", max_tokens=150, task="chat_route",
    )
    if decided is None:
        log.warning("[chat] follow-up router gave no decision — defaulting to sql")
        return "sql"
    return decided["route"]


_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"route": {
        "type": "string", "enum": ["context", "sql"],
        "description": "context: answer from the conversation and the rows already fetched; "
                       "sql: run a new database query"}},
    "required": ["route"],
}


def _context_turn(t: dict) -> str:
    """One turn as the context answer sees it: the same text every time it is
    shown, so the conversation is read back from the prompt cache."""
    parts = [f"Q: {_truncate(t['question'], 300)}"]
    if t["sql"]:
        parts.append(f"SQL used (returned {t['row_count']} rows): {t['sql']}")
    parts.append(f"A: {_truncate(t['answer'], 500)}")
    if t["rows"]:
        rows = t["rows"][:CHAT_HISTORY_MAX_ROWS]
        parts.append(
            f"Columns: {t['columns']}\n"
            f"Summary stats (over all {t['row_count']} rows): "
            f"{json.dumps(t['stats'], default=str, ensure_ascii=False)}\n"
            f"Derived facts (computed exactly): "
            f"{json.dumps(_derived_facts(t['columns'], t['rows']), default=str, ensure_ascii=False)}\n"
            f"Rows (first {len(rows)} of {t['row_count']}): "
            f"{json.dumps(rows, default=str, ensure_ascii=False)}")
    return "\n".join(parts) + "\n"


def _build_context_messages(message: str, turns: list[dict],
                            chart_shown: bool = False) -> list[dict]:
    """Prompt for answering from history: the turns of the conversation window
    (chat_intent.window), each with its rows (up to CHAT_HISTORY_MAX_ROWS),
    then the new question. Only the new question and the note change from
    one request to the next."""
    parts: list[str] = ["Conversation so far (with the data fetched for each turn):\n"
                        + chat_intent.transcript(turns, _context_turn)]
    if chart_shown:
        parts.append("NOTE: a chart of the most recent data above is now shown to the user.")
    parts.append(f"Follow-up question: {message}")
    return [
        {"role": "system", "content": _CONTEXT_ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def _sql_history_block(turns: list[dict]) -> str:
    """Text-only history for the SQL writer — last 2 turns, question + SQL."""
    lines: list[str] = []
    for t in turns[-2:]:
        lines.append(f"Q: {_truncate(t['question'], 200)}")
        if t["sql"]:
            lines.append(f"SQL: {t['sql']}")
        else:
            lines.append(f"A: {_truncate(t['answer'], 160)}")
    return "\n".join(lines)


def _answer_from_context(
    sid: str, message: str, turns: list[dict], t0: float
) -> ChatResponse:
    chat_events.step("context", "Using the data already fetched")
    # "Plot that" / "as a pie chart": draw the rows of the last data turn.
    chart, shown = None, None
    if chat_charts.wants_chart(message):
        shown = next((t for t in reversed(turns) if t["rows"]), None)
        if shown:
            chart = chat_charts.plan_chart(
                f"{message} (about: {shown['question']})", shown["columns"], shown["rows"])
            if chart:
                chat_events.attach("chart", chart)
                _show_result(shown["columns"], shown["rows"], shown["row_count"])
                chat_events.send("chart", **chart)
    chat_events.done("context", detail="Chart drawn from the previous result" if chart else None)
    chat_events.step("answer", "Writing the answer")
    messages = _build_context_messages(message, turns, chart_shown=bool(chart))
    # A context answer reasons over data already shown ("is that good or bad?",
    # "bandingkan dengan Q2"): analytical questions go to the reasoning tier.
    answer_raw, provider_used, usage = _answer_llm(
        messages, max_tokens=700, task="chat_context_answer",
        tier=answer_tier(message),
    )
    chat_events.done("answer")
    log.info(
        "[chat] context-answer provider=%s input=%d output=%d",
        provider_used, usage["input_tokens"], usage["output_tokens"],
    )
    answer = answer_raw.strip() or _NO_DATA_ANSWER
    answer = apply_output_guardrails(answer, context="chat")
    log.info("[chat] done %.2fs (from context)", time.monotonic() - t0)
    return ChatResponse(
        answer=answer,
        session_id=sid,
        route="context",
        provider=provider_used,
        provider_label=(usage.get("model") if provider_used == "bedrock" and usage.get("model")
                        else PROVIDER_LABELS.get(provider_used, provider_used)),
        # With a chart, the rows it was drawn from travel with the answer.
        sql=shown["sql"] if chart else None,
        columns=shown["columns"] if chart else [],
        rows=shown["rows"] if chart else [],
        row_count=shown["row_count"] if chart else 0,
    )


# ── endpoint ───────────────────────────────────────────────────────────────

def _prepare(req: ChatRequest):
    """Validate a request. Returns (message, source, model_id, db), or the
    JSONResponse to send back when it cannot be served."""
    message = (req.message or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"detail": "message must not be empty"})
    message = apply_input_guardrails(message, context="chat")

    source = (req.source or RAG_SOURCE_DEFAULT).lower()
    if source not in VALID_SOURCES:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unknown source {source!r}; expected one of {VALID_SOURCES}"},
        )

    model_id = None
    if req.model:
        if req.model not in CHAT_MODELS:
            return JSONResponse(
                status_code=400,
                content={"detail": f"Unknown model {req.model!r}; expected one of {list(CHAT_MODELS)}"},
            )
        model_id = CHAT_MODELS[req.model]["id"]

    db_id = (req.database or data_sources.LIVE).lower()
    try:
        db = data_sources.get(db_id)
    except KeyError:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unknown database {db_id!r}; expected one of {list(data_sources.registry())}"},
        )
    if not db.available:
        return JSONResponse(
            status_code=400,
            content={"detail": f"The {db.label!r} database is not configured on this server."},
        )

    return message, source, model_id, db


@router.post("/message", response_model=ChatResponse)
def chat_message(req: ChatRequest) -> ChatResponse:
    prepared = _prepare(req)
    if isinstance(prepared, JSONResponse):
        return prepared
    message, source, model_id, db = prepared
    # Everything below (retrieval, SQL, execution, follow-ups) reads the source
    # in force from data_sources.active(), so it all runs inside this block.
    with data_sources.use(db.id), chat_events.recording():
        return _chat_message(req, message, source, model_id, db)


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    """/message as server-sent events, so the page can show progress.

    Events: `step` (a pipeline stage started or finished), `table` (the result
    rows), `chart`, `delta` (a piece of the answer as it is written), then
    `final` (the complete ChatResponse) or `error`. Closing the connection
    stops the request: nothing is stored for a stopped message.
    """
    prepared = _prepare(req)
    if isinstance(prepared, JSONResponse):
        return prepared
    message, source, model_id, db = prepared

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()

    def sink(event: str, data: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (event, data))
        except RuntimeError:        # the loop is gone: server shutting down
            pass

    def work() -> None:
        try:
            with data_sources.use(db.id), chat_events.recording(sink, cancel):
                resp = _chat_message(req, message, source, model_id, db)
            sink("final", resp.model_dump())
        except chat_events.Cancelled:
            log.info("[chat] stopped by the user")
        except Exception as exc:
            log.exception("[chat] stream failed")
            sink("error", {"detail": str(exc)[:300]})
        finally:
            sink("end", {})

    threading.Thread(target=work, name="chat-stream", daemon=True).start()

    async def events():
        try:
            while True:
                event, data = await queue.get()
                if event == "end":
                    break
                yield f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"
        finally:
            cancel.set()             # disconnected or finished: unwind the pipeline

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_SMALLTALK_FALLBACK = (
    "Hi! Ask me anything about your plantation data: harvest, deliveries, "
    "attendance, work done, employees or blocks."
)


def _finish(resp: ChatResponse, t0: float) -> ChatResponse:
    rec = chat_events.current()
    resp.steps = rec.summary() if rec else []
    resp.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return resp


def _smalltalk(sid: str, message: str, turns: list[dict], source: str,
               model_id: Optional[str], db: "data_sources.DataSource", t0: float) -> ChatResponse:
    """A greeting, a thank-you or "what can you do?": answered without a query."""
    system = load_prompt("chat_smalltalk_system", database=db.label,
                         estates=", ".join(db.entity_labels()),
                         topics=db.topics or "plantation operations")
    try:
        with llm_client.model_override(model_id):
            text, provider, usage = _answer_llm(
                [{"role": "system", "content": system}, {"role": "user", "content": message}],
                max_tokens=220, task="chat_smalltalk")
    except Exception as exc:
        log.warning("[chat] small talk failed: %s", exc)
        text, provider, usage = "", None, {}
    history = pipeline_turns(turns)
    resp = ChatResponse(
        answer=apply_output_guardrails(text.strip() or _SMALLTALK_FALLBACK, context="chat"),
        session_id=sid, route=ROUTE_CHAT, source=source,
        database=db.id, database_label=db.label,
        provider=provider, provider_label=usage.get("model") or provider,
        # Somewhere to start, but only while the chat has no real question yet.
        suggestions=[] if history else db.starters[:3],
    )
    resp = _finish(resp, t0)
    resp.turn_id = add_turn(sid, message, resp.answer, route=ROUTE_CHAT, meta={
        "estates": list(history[-1]["meta"].get("estates", [])) if history else [],
        "database": db.id,
        "suggestions": resp.suggestions,
        "model": resp.provider_label,
        "elapsed_ms": resp.elapsed_ms,
    })
    return resp


ROUTE_EXPLAIN = "explain"

_EXPLAIN_FALLBACK = (
    "I couldn't look that definition up just now. Try asking for the figure itself, "
    "and I'll show how it was worked out."
)


def _explain(sid: str, message: str, turns: list[dict], source: str,
             model_id: Optional[str], db: "data_sources.DataSource", t0: float) -> ChatResponse:
    """What a term or measure means, how it is worked out, what the data covers:
    answered from the source's reference text (rules, measures, topics), no query."""
    history = pipeline_turns(turns)
    chat_events.step("answer", "Looking it up in the definitions")
    try:
        with llm_client.model_override(model_id):
            text, provider, usage = _answer_llm(
                chat_intent.explain_messages(message, history),
                max_tokens=450, task="chat_explain", tier=answer_tier(message))
    except Exception as exc:
        log.warning("[chat] explain failed: %s", exc)
        text, provider, usage = "", None, {}
    chat_events.done("answer")
    resp = ChatResponse(
        answer=apply_output_guardrails(text.strip() or _EXPLAIN_FALLBACK, context="chat"),
        session_id=sid, route=ROUTE_EXPLAIN, source=source,
        database=db.id, database_label=db.label,
        provider=provider, provider_label=usage.get("model") or provider,
    )
    if text.strip():
        resp.suggestions = suggest_followups(message, resp.answer, [], [], history)
    resp = _finish(resp, t0)
    resp.turn_id = add_turn(sid, message, resp.answer, route=ROUTE_EXPLAIN, meta={
        "estates": list(history[-1]["meta"].get("estates", [])) if history else [],
        "database": db.id,
        "suggestions": resp.suggestions,
        "steps": resp.steps,
        "model": resp.provider_label,
        "elapsed_ms": resp.elapsed_ms,
    })
    return resp


def _off_domain(sid: str, message: str, turns: list[dict], source: str,
                db: "data_sources.DataSource", t0: float) -> ChatResponse:
    """Nothing to do with the plantation: say what the assistant is for, no model call."""
    history = pipeline_turns(turns)
    resp = _finish(ChatResponse(
        answer=_OFF_DOMAIN_ANSWER, session_id=sid, route=ROUTE_CHAT, source=source,
        database=db.id, database_label=db.label, error_type="off_domain",
        suggestions=[] if history else db.starters[:3],
    ), t0)
    resp.turn_id = add_turn(sid, message, resp.answer, route=ROUTE_CHAT, meta={
        "estates": list(history[-1]["meta"].get("estates", [])) if history else [],
        "database": db.id,
        "suggestions": resp.suggestions,
        "error_type": "off_domain",
        "elapsed_ms": resp.elapsed_ms,
    })
    return resp


ROUTE_FORECAST = chat_forecast.ROUTE


def _forecast(sid: str, message: str, turns: list[dict], source: str,
              model_id: Optional[str], db: "data_sources.DataSource", t0: float) -> ChatResponse:
    """The production forecast, read from the forecast page's own figures: no query
    (chat_forecast.py). The retry after a figure check does not stream, so the page
    never shows two drafts."""
    history = pipeline_turns(turns)
    with llm_client.model_override(model_id):
        out = chat_forecast.respond(message, history, db, ask=_answer_llm, ask_quiet=call_llm)
    answer = apply_output_guardrails(out["answer"], context="chat")
    if out.get("link"):
        answer = f"{answer}\n\n{out['link']}"
    resp = ChatResponse(
        answer=answer, session_id=sid, route=ROUTE_FORECAST, source=source,
        database=db.id, database_label=db.label,
        provider=out.get("provider"), provider_label=out.get("provider_label"),
        chart=out.get("chart"), suggestions=out.get("suggestions") or [],
        estates_used=out.get("labels") or [],
    )
    resp = _finish(resp, t0)
    resp.turn_id = add_turn(sid, message, resp.answer, route=ROUTE_FORECAST, meta={
        # The SQL scope is untouched: a forecast turn neither narrows nor widens it.
        "estates": list(history[-1]["meta"].get("estates", [])) if history else [],
        "database": db.id,
        "forecast": out.get("scope"),
        "chart": resp.chart,
        "suggestions": resp.suggestions,
        "steps": resp.steps,
        "model": resp.provider_label,
        "elapsed_ms": resp.elapsed_ms,
    })
    return resp


def _chat_message(req: ChatRequest, message: str, source: str, model_id: Optional[str],
                  db: "data_sources.DataSource") -> ChatResponse:
    t0 = time.monotonic()
    if req.replace_last and req.session_id:
        delete_last_turn(req.session_id)
    sid = ensure_session(req.session_id, message)
    turns = get_turns(sid, limit=max(chat_intent.FETCH_TURNS,
                                     CHAT_HISTORY_TEXT_TURNS + CHAT_HISTORY_DATA_TURNS))
    if req.replace_last and not turns:
        update_session(sid, title=message)      # the first message was edited
    log.info('[chat] message="%s" session=%s source=%s db=%s model=%s turns=%d',
             message[:120], sid[:8], source, db.id, req.model or "auto", len(turns))

    # The knowledge base holds documents; these checks are about database queries.
    uses_data = source != "knowledge_base"
    answering_us = bool(turns) and turns[-1]["route"] == ROUTE_CLARIFY
    # "Run it" agrees to what the last answer offered: nothing to ask about.
    agreeing = bool(turns) and not answering_us and is_confirmation(message)

    intent = None
    if uses_data:
        intent, _ = chat_intent.classify(message, pipeline_turns(turns),
                                         agreeing=agreeing, answering_us=answering_us)
        if intent == chat_intent.CHAT:
            return _smalltalk(sid, message, turns, source, model_id, db, t0)
        if intent == chat_intent.EXPLAIN:
            return _explain(sid, message, turns, source, model_id, db, t0)
        if intent == chat_intent.OFF_DOMAIN:
            return _off_domain(sid, message, turns, source, db, t0)
        if intent == chat_intent.FORECAST:
            return _forecast(sid, message, turns, source, model_id, db, t0)

    # A follow-up about what is on screen points at it: nothing to ask about.
    asked = (clarify_gate(message, turns)
             if uses_data and not agreeing and intent != chat_intent.CONTEXT else None)
    if asked:
        turn_id = add_turn(sid, message, asked["question"], route=ROUTE_CLARIFY,
                           meta={"options": asked["options"], "database": db.id})
        return _finish(ChatResponse(
            answer=asked["question"], session_id=sid, route=ROUTE_CLARIFY,
            source=source, options=asked["options"],
            database=db.id, database_label=db.label, turn_id=turn_id), t0)

    question = resolve_reply(message, turns)
    history = pipeline_turns(turns)
    if agreeing:
        question = resolve_confirmation(message, history)
    session = SessionContext(session_id=sid, turns=history)
    try:
        with llm_client.model_override(model_id), chat_intent.preset(intent), \
                (known_on_domain() if intent else nullcontext()):
            result = dispatch_rag(source, question, session, sql_source=_SQL_SOURCE)
    except Exception as exc:
        log.warning("[chat] source=%s failed: %s", source, exc)
        return _finish(ChatResponse(
            answer=f"The '{source}' source is unavailable right now: {exc}",
            session_id=sid,
            route=source,
            source=source,
            database=db.id,
            database_label=db.label,
            error_type="source_unavailable",
        ), t0)

    resp = ChatResponse(
        answer=result.answer,
        session_id=sid,
        route=result.route,
        source=result.source,
        database=db.id,
        database_label=db.label,
        sql=result.sql,
        columns=result.columns,
        rows=result.rows[:200],
        row_count=result.row_count,
        tables_used=result.tables_used,
        citations=result.citations,
        provider=result.provider,
        provider_label=result.provider_label,
        used_llm_fallback=result.used_llm_fallback,
        error_type=result.error_type,
        estates_used=result.estates_used,
        estates_failed=result.estates_failed,
        sql_outcome=result.sql_outcome,
        parts=result.parts,
    )
    ran_query = result.route in ("sql", "overview") and not resp.error_type
    if ran_query:
        resp.estates_carried = [_scope_label(db, e)
                                for e in carried_estates(question, history)]
    if uses_data and not resp.error_type:
        resp.suggestions = suggest_followups(
            question, resp.answer, resp.columns, resp.tables_used, history)
    rec = chat_events.current()
    extras = rec.extras if rec else {}
    resp.chart = extras.get("chart") if not resp.error_type else None
    resp = _finish(resp, t0)
    resp.turn_id = add_turn(
        sid, message, resp.answer,
        sql=resp.sql if not resp.error_type else None,
        columns=resp.columns, rows=resp.rows,
        row_count=resp.row_count, stats=result.stats,
        route=resp.route,
        meta={
            # Everything attempted, so an estate that failed is not read as
            # the user having narrowed the scope.
            "estates": scope_after(resp.estates_used + resp.estates_failed, history, ran_query),
            "database": db.id,
            "resolved": question if question != message else None,
            "carried": resp.estates_carried,
            "suggestions": resp.suggestions,
            # For showing the turn again as it was: what was done, drawn and used.
            "steps": resp.steps,
            "chart": resp.chart,
            "tables": resp.tables_used,
            "error_type": resp.error_type,
            "model": resp.provider_label,
            "elapsed_ms": resp.elapsed_ms,
            "query_id": extras.get("query_id"),
            "sql_outcome": resp.sql_outcome,
            "parts": resp.parts,
        },
    )
    return resp


def _resolve_scope(message: str, turns: list[dict]) -> tuple[list[str], list[str], str]:
    """(scope ids to run on, staging estate codes, the question as the SQL writer sees it)."""
    estate_ids, entity_codes, sql_question, _ = _scope(message, turns)
    return estate_ids, entity_codes, sql_question


def _scope(message: str, turns: list[dict]) -> tuple[list[str], list[str], str, Optional[str]]:
    """_resolve_scope(), plus the staging scope hint on its own (None on live)."""
    src = data_sources.active()
    entity_codes: list[str] = []
    hint: Optional[str] = None
    # "Estate C against the other estates": named, but set against the rest, so
    # every estate is queried and the one named is only the focus.
    widen = widens_scope(message)
    if src.entities:
        # Staging holds every estate in one database: nothing to fan out over,
        # the estates named (or carried from the last turn) become a WHERE hint.
        estate_ids = list(src.scope_order)
        if widen:
            focus = [src.entities[c]["label"] for c in src.detect_entities(message)]
            hint = ("Scope: EVERY estate, one row per estate, with no filter on WERKS, ESTNR or PRFNR"
                    + (f", so {' and '.join(focus)} can be compared with the others." if focus else "."))
        else:
            entity_codes = src.detect_entities(message) or carried_estates(message, turns)
            hint = src.scope_hint(entity_codes)
        sql_question = f"{message}\n{hint}" if hint else message
    else:
        mentioned_estates = detect_estate_mentions(message)
        if widen:
            estate_ids = list(ESTATE_ORDER)
        else:
            estate_ids = mentioned_estates or carried_estates(message, turns) or list(ESTATE_ORDER)
        # SQL generation only — table retrieval and the final answer still see
        # the original message. See strip_estate_mentions() docstring for why.
        sql_question = strip_estate_mentions(message, mentioned_estates) if mentioned_estates else message
    return estate_ids, entity_codes, sql_question, hint


# ── writing, checking and running one query ────────────────────────────────

_MAX_ROWS = 200

# A question about the most recent days can be empty because the data has not
# been loaded yet; asking the writer to "fix" it would only move the period.
_RECENT = re.compile(
    r"\b(?:today|yesterday|tonight|this\s+(?:week|month)|(?:last|past)\s+\d+\s+(?:days?|weeks?|months?)"
    r"|hari\s+ini|kemarin|minggu\s+ini|bulan\s+ini|\d+\s+(?:hari|minggu|bulan)\s+terakhir)\b", re.IGNORECASE)

_TIMEOUT = re.compile(r"HYT00|timeout expired|statement timeout|canceling statement", re.IGNORECASE)

_EMPTY_FINDING = (
    "Suspicious result: the query returned no rows. Check every filter against the DDL and the business "
    "rules: the date column and range, the case of text values, code formats (WERKS '00EC' against ESTNR "
    "'EC'), and joins that can match nothing. If the filters are right and there is simply no data, "
    "return the same SQL unchanged.")
_ZERO_FINDING = (
    "Suspicious result: the query returned one row whose figures are all zero or empty. Check the filters "
    "and joins as for an empty result. If zero is the true answer, return the same SQL unchanged.")

_FIX_STATUS = {
    "lint": "It broke a data rule, correcting it",
    "schema": "A name in the query does not exist, correcting it",
    "exec": "The database rejected the query",
    "empty": "No rows came back, checking the query",
    "suspicious": "The figures look wrong, checking the query",
}

# What the answer is told when a doubtful result is all there is.
_KEPT_NOTE = {
    "suspicious": "a check found figures that look implausible for this data ({why}). Say in one short "
                  "sentence that these figures may be wrong and should be checked; no technical detail.",
}


@dataclass
class RunResult:
    """What generate_and_run() did: the query that stands, its per-estate results, and how it got there."""
    sql: Optional[str] = None
    exec_results: dict = field(default_factory=dict)
    ok_estates: list = field(default_factory=list)
    failed_estates: list = field(default_factory=list)
    # first_ok | fixed_<kind> | kept_<kind> | failed | no_sql | unsafe, where kind is
    # schema | lint | exec | empty | suspicious (the first problem found)
    outcome: str = "failed"
    provider: str = ""
    attempts: int = 0
    run_step: str = "run"
    findings: list = field(default_factory=list)      # [(kind, message)] in order
    notes: list = field(default_factory=list)         # for the answer prompt
    truncated: bool = False

    @property
    def ran(self) -> bool:
        return bool(self.ok_estates)


def _pre_run_finding(sql: str, question: str) -> tuple[Optional[str], Optional[str]]:
    """(kind, message) for a query that must not run as written, else (None, None)."""
    src = data_sources.active()
    if CHAT_SCHEMA_CHECK != "off":
        finding = sql_schema_check.check(sql, src.dialect, src.id)
        if finding:
            if CHAT_SCHEMA_CHECK == "enforce":
                return "schema", finding
            log.info("[chat] schema check (log only): %s", finding[:160])
    finding = src.lint(sql, question) if src.lint else None
    return ("lint", finding) if finding else (None, None)


def _result_finding(columns: list[str], rows: list[dict], question: str,
                    sql: str) -> tuple[Optional[str], Optional[str]]:
    """(kind, message) for rows that ran but cannot be the answer, else (None, None)."""
    if not rows:
        return "empty", _EMPTY_FINDING
    # One total per estate (or a single one) that is zero everywhere: the
    # query matched nothing, like an empty result. A longer table with some
    # zero rows is a real answer.
    if len(rows) <= len(data_sources.active().scope_order) or len(rows) == 1:
        vals = [v for r in rows for k, v in r.items() if k != "_source_estate" and not isinstance(v, str)]
        if vals and all(v is None or v == 0 for v in vals):
            return "empty", _ZERO_FINDING
    src = data_sources.active()
    finding = src.check_result(columns, rows, question, sql) if src.check_result else None
    return ("suspicious", finding) if finding else (None, None)


def _norm_sql(sql: str) -> str:
    """Whitespace-insensitive, but case-sensitive: 'ec' -> 'EC' is a real correction."""
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";"))


def generate_and_run(sql_question: str, question: str, table_cards, examples, history_block,
                     estate_ids: list[str], *, step_prefix: str = "", what: str = "") -> RunResult:
    """Write the query, check it, run it, and correct it until it stands.

    Before a query runs: the safety validator, then the schema check (every
    table and column exists), then the source's lint (valid SQL that gives
    wrong numbers). After it runs: an estate that failed while others answered
    is run once more as it is (the estates share one schema, so that is a
    connection or timeout, not the SQL); then the rows themselves are checked
    (empty, all zero, figures no real query gives).

    Every problem goes back to the writer with exactly what was wrong, up to
    CHAT_SQL_FIX_ATTEMPTS times, on the reasoning tier. A query still flagged
    before running is never run. A query that ran but looked wrong is kept as
    the fallback, and only replaced by one that passes every check.
    """
    src = data_sources.active()
    res = RunResult()

    def sid_(name: str) -> str:
        return f"{step_prefix}{name}"

    about = f": {what}" if what else ""      # several queries at once (overview): say which is which
    chat_events.step(sid_("sql"), f"Writing the query{about}")
    raw_sql, res.provider, usage = call_llm(
        build_sql_messages(sql_question, table_cards, examples, history_block=history_block),
        max_tokens=800, temperature=0, task="sql_generation")
    log.info("[chat] SQL gen provider=%s input=%d output=%d",
             res.provider, usage["input_tokens"], usage["output_tokens"])
    sql = extract_sql(raw_sql)
    chat_events.done(sid_("sql"), sql=sql or None, status="done" if sql else "error",
                     detail=None if sql else "No query could be written")
    if not sql:
        res.outcome = "no_sql"
        return res

    held: Optional[tuple] = None           # the first query that ran but looked wrong
    exec_results: dict = {}
    for attempt in range(1, CHAT_SQL_FIX_ATTEMPTS + 2):
        res.attempts = attempt
        run_step = sid_("run" if attempt == 1 else ("rerun" if attempt == 2 else f"rerun{attempt - 1}"))
        ok, reason = validate(sql, src.dialect)
        if not ok:
            log.info("[chat] SQL validation failed: %s", reason)
            if attempt == 1:
                chat_events.done(sid_("sql"), status="error", detail="The query was not safe to run", sql=sql)
                res.sql, res.outcome = sql, "unsafe"
                return res
            break

        kind, finding = _pre_run_finding(sql, question)
        if finding:
            # Valid SQL that names something missing or would give wrong numbers: never run it.
            log.info("[chat] query flagged before running (%s): %s", kind, finding[:140])
            chat_events.step(run_step, f"Checking the query{about}")
            exec_results = {e: ([], [], finding) for e in estate_ids}
        else:
            chat_events.step(run_step, (f"Running on {src.label}" if attempt == 1
                                        else f"Running again on {src.label}") + about)
            exec_results = _execute_multi(sql, estate_ids, _MAX_ROWS)
            failed = [e for e in estate_ids if exec_results[e][2]]
            if failed and (len(failed) < len(estate_ids)
                           or all(_TIMEOUT.search(exec_results[e][2]) for e in failed)):
                # Some estates answered (the SQL is fine there), or the server was too
                # busy: run the same SQL once more rather than have it rewritten.
                log.info("[chat] %s failed (%s), running it once more", failed,
                         exec_results[failed[0]][2][:80])
                exec_results.update(_execute_multi(sql, failed, _MAX_ROWS))
        ok_e = [e for e in estate_ids if not exec_results[e][2]]
        failed_e = [e for e in estate_ids if exec_results[e][2]]

        if ok_e:
            columns = exec_results[ok_e[0]][0]
            rows = [r for e in ok_e for r in exec_results[e][1]]
            kind, finding = _result_finding(columns, rows, question, sql)
            if not finding:
                first = res.findings[0][0] if res.findings else None
                res.outcome = f"fixed_{first}" if first else "first_ok"
                res.sql, res.exec_results, res.ok_estates, res.failed_estates = sql, exec_results, ok_e, failed_e
                res.run_step = run_step
                res.truncated = any(len(exec_results[e][1]) >= _MAX_ROWS for e in ok_e)
                return res
            if held is None:
                held = (sql, exec_results, ok_e, failed_e, kind, finding, run_step)
        else:
            kind = kind or "exec"
            finding = finding or exec_results[estate_ids[0]][2]

        res.findings.append((kind, finding))
        no_retry = kind == "empty" and bool(_RECENT.search(question))
        if attempt > CHAT_SQL_FIX_ATTEMPTS or no_retry:
            if not (held and held[6] == run_step):
                chat_events.done(run_step, status="error", detail=_FIX_STATUS.get(kind))
            break
        chat_events.done(run_step, status="error", detail=_FIX_STATUS.get(kind))
        log.info("[chat] attempt %d: %s — asking for a correction: %s", attempt, kind, str(finding)[:140])

        fix_step = sid_("fix" if attempt == 1 else f"fix{attempt}")
        chat_events.step(fix_step, f"Correcting the query{about}")
        prior = finding if len(res.findings) == 1 else (
            f"{finding}\nThe attempt before that also failed: {res.findings[-2][1]}")
        raw_retry, res.provider, _ = call_llm(
            build_sql_messages(sql_question, table_cards, examples, prior_error=prior,
                               prior_sql=sql, history_block=history_block),
            max_tokens=800, temperature=0, task="sql_self_correction",
            # The default model got it wrong: the corrections go to the stronger one.
            tier="reasoning" if CHAT_ESCALATE_TO_REASONING else "main")
        new_sql = extract_sql(raw_retry)
        chat_events.done(fix_step, sql=new_sql or None,
                         detail=None if new_sql and _norm_sql(new_sql) != _norm_sql(sql)
                         else "No different query could be written")
        if not new_sql or _norm_sql(new_sql) == _norm_sql(sql):
            break           # nothing new to try; "unchanged" is also the writer saying the result is right
        sql = new_sql

    if held:
        h_sql, h_exec, h_ok, h_failed, h_kind, h_finding, h_step = held
        res.sql, res.exec_results, res.ok_estates, res.failed_estates = h_sql, h_exec, h_ok, h_failed
        res.outcome, res.run_step = f"kept_{h_kind}", h_step
        res.truncated = any(len(h_exec[e][1]) >= _MAX_ROWS for e in h_ok)
        if h_kind in _KEPT_NOTE:
            res.notes.append(_KEPT_NOTE[h_kind].format(why=h_finding.split(":", 1)[-1].split(".")[0].strip()))
        return res
    res.sql, res.exec_results, res.outcome, res.run_step = sql, exec_results, "failed", run_step
    res.failed_estates = list(estate_ids)
    return res


def _merge_estates(res: RunResult, estate_ids: list[str]) -> tuple[list[str], list[dict], dict]:
    """(columns, rows tagged by estate when several ran, per-estate breakdown)."""
    src = data_sources.active()
    multi_estate = len(estate_ids) > 1
    columns = res.exec_results[res.ok_estates[0]][0]
    combined_rows: list[dict] = []
    breakdown: dict[str, dict] = {}
    for eid in res.ok_estates:
        est_columns, est_rows, _ = res.exec_results[eid]
        label = src.scopes[eid]["label"]
        combined_rows.extend([{**r, "_source_estate": label} for r in est_rows] if multi_estate else est_rows)
        breakdown[label] = {"row_count": len(est_rows), **_compute_stats(est_columns, est_rows)}
    if multi_estate and "_source_estate" not in columns:
        columns = columns + ["_source_estate"]
    return columns, combined_rows, breakdown


# ── broad questions: several measures side by side (chat_overview.py) ──────

_OVERVIEW_SYSTEM_PROMPT = load_prompt("chat_overview_system")
_OVERVIEW_TIMEOUT_S = 60


def _run_overview_pipeline(sid: str, message: str, turns: list[dict], t0: float
                           ) -> Optional[tuple[ChatResponse, Optional[dict]]]:
    """Answer a broad question with 2 to 4 measures, or None to answer it the plain way."""
    src = data_sources.active()
    estate_ids, entity_codes, _, hint = _scope(message, turns)
    single = len(entity_codes) == 1 if src.entities else len(estate_ids) == 1

    chat_events.step("plan", "Choosing what to look at")
    plan = chat_overview.plan(message, src, single_estate=single)
    if plan is None:
        chat_events.done("plan", status="error", detail="Answering it as one question")
        return None
    chat_events.done("plan", detail=", ".join(m.label for m in plan.measures)
                     + f" per {plan.grain}, {plan.period_label}")

    def one(i: int, measure):
        q = chat_overview.sub_question(measure, plan, src)
        cards, _ = retrieve_tables(q, k=5)
        if not cards:
            return q, [], None
        try:
            examples = retrieve_examples(q, k=3)
        except Exception:
            examples = []
        sql_q = f"{q}\n{hint}" if hint else q
        return q, cards, generate_and_run(sql_q, q, cards, examples, None, estate_ids,
                                          step_prefix=f"p{i}.", what=measure.label)

    pool = ThreadPoolExecutor(max_workers=len(plan.measures))
    # One copy of the context per part: it carries the data source, the model
    # choice and the step recorder, and a context cannot be entered twice at once.
    futures = [pool.submit(contextvars.copy_context().run, one, i, m)
               for i, m in enumerate(plan.measures, 1)]
    pool.shutdown(wait=False)
    parts: list[dict] = []
    results: list[tuple] = []
    tables: list[str] = []
    deadline = time.monotonic() + _OVERVIEW_TIMEOUT_S
    for measure, fut in zip(plan.measures, futures):
        try:
            q, cards, res = fut.result(timeout=max(0.1, deadline - time.monotonic()))
        except chat_events.Cancelled:
            raise
        except Exception as exc:
            log.warning("[overview] %s failed: %s", measure.id, exc)
            q, cards, res = chat_overview.sub_question(measure, plan, src), [], None
        tables.extend(c.name for c in cards if c.name not in tables)
        ran = res is not None and res.ran
        part = {"measure": measure.id, "label": measure.label, "question": q,
                "sql": res.sql if res else None, "outcome": res.outcome if res else "failed",
                "findings": [k for k, _ in res.findings] if res else [], "row_count": 0}
        if ran:
            columns, rows, _ = _merge_estates(res, estate_ids)
            part["row_count"] = len(rows)
            if rows:              # a measure with no data is named in the answer, not merged
                results.append((measure, columns, rows, res))
            chat_events.done(res.run_step, rows=len(rows),
                             detail=f"{len(rows):,} row{'' if len(rows) == 1 else 's'}")
        elif res is not None:
            chat_events.done(res.run_step, status="error", detail="The query could not be run")
        parts.append(part)

    outcome = ",".join(p["outcome"] for p in parts)
    chat_events.attach("sql_outcome", f"overview:{outcome}")
    failed = [p["label"] for p in parts if not p["row_count"]]
    if not results:
        return ChatResponse(answer=_SQL_FAIL_ANSWER, session_id=sid, route="overview",
                            tables_used=tables, error_type="no_data", parts=parts,
                            sql_outcome=f"overview:{outcome}"), None

    # A part kept although its figures looked implausible is shown on its own,
    # never side by side with the sound ones.
    sound = [(m, c, r) for m, c, r, res in results if not res.outcome.startswith("kept_suspicious")]
    merged = chat_overview.merge(sound, plan.grain, src)
    if merged:
        columns, rows, in_table = merged
    else:
        _, columns, rows, _ = results[0]
        in_table = [results[0][0].id]
    apart = [(m, c, r) for m, c, r, _ in results if m.id not in in_table]
    _show_result(columns, rows, len(rows))
    chart_future = _start_chart(message, columns, rows)

    notes = [n for *_, res in results for n in res.notes]
    lines = [f"Question: {message}", f"Period: {plan.period_label}",
             "Measures looked at: " + "; ".join(f"{p['label']} ({p['question']})" for p in parts)]
    if failed:
        lines.append("Could not be retrieved: " + ", ".join(failed))
    lines += [f"Note: {n}" for n in notes]
    stats = _compute_stats(columns, rows)
    derived = _derived_facts(columns, rows)
    lines.append(f"Table columns: {columns}\nRows: "
                 + json.dumps(rows[:50], default=str, ensure_ascii=False))
    for m, c, r in apart:
        lines.append(f"Part '{m.label}', shown apart from the table: columns {c}, rows (first 20): "
                     + json.dumps(r[:20], default=str, ensure_ascii=False))
    if derived:
        lines.append("Derived facts (computed exactly; quote these, do not calculate your own): "
                     + json.dumps(derived, default=str, ensure_ascii=False))

    chat_events.step("answer", "Writing the answer")
    answer_raw, provider_used, usage = _answer_llm(
        [{"role": "system", "content": _OVERVIEW_SYSTEM_PROMPT}, {"role": "user", "content": "\n\n".join(lines)}],
        max_tokens=800, task="chat_overview_answer", tier="reasoning")
    chat_events.done("answer")
    _wait_chart(chart_future)
    answer = apply_output_guardrails(answer_raw.strip() or _NO_DATA_ANSWER, context="chat")

    used_labels = ([src.entities[c]["label"] for c in entity_codes] if src.entities
                   else [src.scopes[e]["label"] for e in estate_ids])
    log.info("[overview] done %.2fs measures=%s grain=%s merged=%s outcome=%s",
             time.monotonic() - t0, [m.id for m in plan.measures], plan.grain, bool(merged), outcome)
    return ChatResponse(
        answer=answer, session_id=sid, route="overview",
        sql="\n\n".join(f"-- {p['label']}\n{p['sql']}" for p in parts if p["sql"]),
        columns=columns, rows=rows[:200], row_count=len(rows), tables_used=tables,
        provider=provider_used,
        provider_label=usage.get("model") if provider_used == "bedrock" and usage.get("model")
        else PROVIDER_LABELS.get(provider_used, provider_used),
        estates_used=used_labels, parts=parts, sql_outcome=f"overview:{outcome}",
    ), stats


def _run_sql_pipeline(
    sid: str, message: str, turns: list[dict], t0: float
) -> tuple[ChatResponse, Optional[dict]]:
    src = data_sources.active()
    if src.measures and is_overview(message):
        overview = _run_overview_pipeline(sid, message, turns, t0)
        if overview is not None:
            return overview
    estate_ids, entity_codes, sql_question = _resolve_scope(message, turns)

    # ── 1. Retrieve tables (augmented with the prior question on follow-ups) ─
    chat_events.step("tables", "Finding the right tables")
    retrieval_query = retrieval_context(message, turns)
    table_cards, used_llm_fallback = retrieve_tables(retrieval_query, k=5)
    table_names = [c.name for c in table_cards]
    log.info("[chat] tables: %s (llm_fallback=%s)", table_names, used_llm_fallback)
    chat_events.done("tables", detail=", ".join(table_names) or "Nothing relevant found",
                     status="done" if table_cards else "error", tables=table_names)

    if not table_cards:
        kind = "off_domain" if is_off_domain(message) else "no_data"
        log.info("[chat] no tables — %s", kind)
        answer = _OFF_DOMAIN_ANSWER if kind == "off_domain" else _NO_DATA_ANSWER
        return ChatResponse(
            answer=answer,
            session_id=sid,
            used_llm_fallback=used_llm_fallback,
            error_type=kind,
        ), None

    # ── 2. Few-shot examples ──────────────────────────────────────────────
    try:
        examples = retrieve_examples(message, k=3)
    except Exception:
        examples = []

    # ── 3-5. Write, check, run and correct the query ──────────────────────
    history_block = _sql_history_block(turns) if turns else None
    res = generate_and_run(sql_question, message, table_cards, examples, history_block, estate_ids)
    chat_events.attach("sql_outcome", res.outcome)
    provider_used = res.provider
    sql = res.sql
    run_step = res.run_step

    if res.outcome in ("no_sql", "unsafe"):
        return ChatResponse(
            answer=_NO_DATA_ANSWER if res.outcome == "no_sql" else _SQL_FAIL_ANSWER,
            session_id=sid,
            sql=sql,
            tables_used=table_names,
            provider=provider_used,
            provider_label=PROVIDER_LABELS.get(provider_used, provider_used),
            used_llm_fallback=used_llm_fallback,
            error_type="no_data",
            sql_outcome=res.outcome,
        ), None

    if not res.ran:
        chat_events.done(run_step, status="error", detail="The query could not be run")
        log.info("[chat] SQL still failing after %d attempt(s) on every estate", res.attempts)
        record_query(
            message, sql, provider_used, table_names,
            success=False, error=res.exec_results[estate_ids[0]][2], source=src.id,
        )
        return ChatResponse(
            answer=_SQL_FAIL_ANSWER,
            session_id=sid,
            sql=sql,
            tables_used=table_names,
            provider=provider_used,
            provider_label=PROVIDER_LABELS.get(provider_used, provider_used),
            used_llm_fallback=used_llm_fallback,
            error_type="no_data",
            sql_outcome=res.outcome,
        ), None

    # ── 6. Merge per-estate results, compute stats & build answer ─────────
    multi_estate = len(estate_ids) > 1
    ok_estates, failed_estates = res.ok_estates, res.failed_estates
    columns, combined_rows, estate_breakdown = _merge_estates(res, estate_ids)

    chat_events.done(
        run_step, rows=len(combined_rows),
        detail=f"{len(combined_rows):,} row{'' if len(combined_rows) == 1 else 's'}"
               + (f" from {len(ok_estates)} estates" if multi_estate else ""))
    _show_result(columns, combined_rows, len(combined_rows))
    chart_future = _start_chart(message, columns, combined_rows)

    failed_labels = [src.scopes[e]["label"] for e in failed_estates]
    # On staging the estates "used" are the ones the question was scoped to.
    used_labels = ([src.entities[c]["label"] for c in entity_codes] if src.entities
                   else [src.scopes[e]["label"] for e in ok_estates])
    stats = _compute_stats(columns, combined_rows)
    answer_messages = _build_answer_messages(
        message, sql, combined_rows, columns, stats,
        estate_breakdown=estate_breakdown if multi_estate else None,
        failed_estates=failed_labels or None,
        data_through=src.data_through(),
        period_note=None if names_period(message) else _period_note(sql),
        notes=res.notes + ([
            f"only the first {_MAX_ROWS} rows{' per estate' if multi_estate else ''} were read, so any "
            "sum of these rows is a partial sum: never present it as a total."] if res.truncated else []),
    )
    a_tier = answer_tier(message)
    chat_events.step("answer", "Writing the answer")
    answer_raw, provider_used, usage2 = _answer_llm(
        answer_messages, max_tokens=700, task="chat_answer", tier=a_tier,
    )
    chat_events.done("answer")
    _wait_chart(chart_future)
    log.info(
        "[chat] answer tier=%s model=%s input=%d output=%d",
        a_tier, usage2.get("model"), usage2["input_tokens"], usage2["output_tokens"],
    )
    # Name the model that actually answered (the static label would always show
    # the default model, even after an escalation).
    answer_label = (
        usage2.get("model") if provider_used == "bedrock" and usage2.get("model")
        else PROVIDER_LABELS.get(provider_used, provider_used)
    )

    answer = answer_raw.strip() or _NO_DATA_ANSWER
    answer = apply_output_guardrails(answer, context="chat")
    answer = _with_period(answer, message, sql)

    # ── 7. Record to history & refresh index ─────────────────────────────
    # Only a query that passed every check may become an example for later
    # questions; one kept although it looked wrong (empty, implausible) is
    # recorded but never shown to the SQL writer.
    sound = res.outcome == "first_ok" or res.outcome.startswith("fixed_")
    chat_events.attach("query_id", record_query(
        message, sql, provider_used, table_names, success=sound, source=src.id,
        row_count=len(combined_rows)))
    try:
        from retrieval import refresh_query_history_index
        refresh_query_history_index()
    except Exception:
        pass

    elapsed = time.monotonic() - t0
    log.info(
        "[chat] done %.2fs rows=%d estates=%s failed=%s",
        elapsed, len(combined_rows), used_labels, failed_labels,
    )

    return ChatResponse(
        answer=answer,
        session_id=sid,
        sql=sql,
        columns=columns,
        rows=combined_rows[:200],
        row_count=len(combined_rows),
        tables_used=table_names,
        provider=provider_used,
        provider_label=answer_label,
        used_llm_fallback=used_llm_fallback,
        estates_used=used_labels,
        estates_failed=failed_labels,
        sql_outcome=res.outcome,
    ), stats


# Wraps the functions above as a BaseRagSource (rag_sources.py) — pass-through
# adapter, no logic change. Built here (not in rag_sources.py) so that module
# never has to import chat_router and there's no import cycle.
_SQL_SOURCE = SqlRagSource(_route_followup, _answer_from_context, _run_sql_pipeline)


# ── session endpoints ──────────────────────────────────────────────────────

@router.get("/sessions")
def sessions_index():
    return {"sessions": list_sessions()}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str):
    return {"id": session_id, "turns": get_turns(session_id, limit=100)}


@router.patch("/sessions/{session_id}")
def session_update(session_id: str, req: SessionUpdate):
    """Rename or pin a chat."""
    if not update_session(session_id, title=req.title, pinned=req.pinned):
        return JSONResponse(status_code=404, content={"detail": "session not found or nothing to change"})
    return {"ok": True}


@router.post("/feedback")
def feedback(req: FeedbackRequest):
    """Thumbs up / down on an answer. A thumbs-down also stops that answer's
    SQL from being shown to the SQL writer as an example of a good query."""
    if req.rating not in (None, "up", "down"):
        return JSONResponse(status_code=400, content={"detail": "rating must be 'up', 'down' or null"})
    meta = set_feedback(req.session_id, req.turn_id, req.rating)
    if meta is None:
        return JSONResponse(status_code=404, content={"detail": "turn not found"})
    query_id = meta.get("query_id")
    if query_id:
        if req.rating == "up":
            # A person vouches for it: it becomes a verified example, ranked
            # ahead of the ones that only passed the checks.
            set_verified(query_id)
            from retrieval import remember_example
            remember_example(query_id, meta.get("database"))
        elif req.rating == "down":
            set_success(query_id, False)
            from retrieval import forget_example
            forget_example(query_id, meta.get("database"))
        # A cleared rating changes nothing: the query keeps the standing it earned by its checks.
    return {"ok": True}


@router.delete("/sessions/{session_id}")
def session_remove(session_id: str):
    deleted = delete_session(session_id)
    if not deleted:
        return JSONResponse(status_code=404, content={"detail": "session not found"})
    return {"ok": True}
