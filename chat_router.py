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
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import chat_charts
import chat_events
from chat_conversation import (
    ROUTE_CHAT,
    ROUTE_CLARIFY,
    carried_estates,
    chart_followup,
    clarify_gate,
    is_confirmation,
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
    ESTATE_ORDER,
    ESTATES,
    RAG_SOURCE_DEFAULT,
)
import data_sources
from error_handler import is_off_domain
from estates import detect_estate_mentions, strip_estate_mentions
from guardrails import apply_input_guardrails, apply_output_guardrails
import llm_client
from llm import call_llm, get_provider, PROVIDER_LABELS
from model_routing import answer_tier
from prompt_builder import build_sql_messages, extract_sql
from prompts import load_prompt
from query_history import record_query, set_success
from rag_sources import SessionContext, SqlRagSource, VALID_SOURCES, dispatch as dispatch_rag
from retrieval import retrieve_tables, retrieve_examples
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


def _execute_safe(
    sql: str, max_rows: int = 200, engine_obj=None, dialect: str = "postgresql"
) -> tuple[list[str], list[dict], Optional[str]]:
    # `dialect` is passed in rather than read from data_sources.active(): this
    # runs on pool threads, which don't inherit the request's ContextVar.
    engine_obj = engine if engine_obj is None else engine_obj
    try:
        limited = ensure_limit(sql, max_rows, dialect)
        with engine_obj.connect() as conn:
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
    for col in measures[:3]:
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
    """
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

    try:
        result = llm_client.chat(
            [
                {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            tier="routing",
            temperature=0,
            # 20 tokens sometimes truncated the JSON mid-object; 60 is safe.
            max_tokens=60,
            json_mode=True,
            task="chat_route",
        )
        route = json.loads(result.text).get("route", "sql")
        return route if route in ("context", "sql") else "sql"
    except Exception as exc:
        log.warning("[chat] follow-up router failed: %s — defaulting to sql", exc)
        return "sql"


def _build_context_messages(message: str, turns: list[dict],
                            chart_shown: bool = False) -> list[dict]:
    """Prompt for answering from history. Tiered payload: text Q&A for recent
    turns, full rows only for the last CHAT_HISTORY_DATA_TURNS data-bearing
    turns, capped at CHAT_HISTORY_MAX_ROWS rows each."""
    text_turns = turns[-CHAT_HISTORY_TEXT_TURNS:]
    data_turns = [t for t in turns if t["rows"]][-CHAT_HISTORY_DATA_TURNS:]

    parts: list[str] = ["Conversation so far:"]
    for t in text_turns:
        parts.append(f"Q: {_truncate(t['question'], 300)}")
        # Rows for data-bearing turns are appended below (with their SQL);
        # for row-less turns the SQL alone still grounds meta-questions.
        if t["sql"] and t not in data_turns:
            parts.append(f"SQL used (returned {t['row_count']} rows): {t['sql']}")
        parts.append(f"A: {_truncate(t['answer'], 500)}")

    for t in data_turns:
        rows = t["rows"][:CHAT_HISTORY_MAX_ROWS]
        rows_json  = json.dumps(rows, default=str, ensure_ascii=False)
        stats_json = json.dumps(t["stats"], default=str, ensure_ascii=False)
        parts.append(
            f"\nData fetched earlier for: {_truncate(t['question'], 200)}\n"
            f"SQL: {t['sql']}\n"
            f"Columns: {t['columns']}\n"
            f"Summary stats (over all {t['row_count']} rows): {stats_json}\n"
            f"Derived facts (computed exactly): "
            f"{json.dumps(_derived_facts(t['columns'], t['rows']), default=str, ensure_ascii=False)}\n"
            f"Rows (first {len(rows)} of {t['row_count']}): {rows_json}"
        )

    if chart_shown:
        parts.append("\nNOTE: a chart of the most recent data above is now shown to the user.")
    parts.append(f"\nFollow-up question: {message}")
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


def _chat_message(req: ChatRequest, message: str, source: str, model_id: Optional[str],
                  db: "data_sources.DataSource") -> ChatResponse:
    t0 = time.monotonic()
    if req.replace_last and req.session_id:
        delete_last_turn(req.session_id)
    sid = ensure_session(req.session_id, message)
    turns = get_turns(sid, limit=CHAT_HISTORY_TEXT_TURNS + CHAT_HISTORY_DATA_TURNS)
    if req.replace_last and not turns:
        update_session(sid, title=message)      # the first message was edited
    log.info('[chat] message="%s" session=%s source=%s db=%s model=%s turns=%d',
             message[:120], sid[:8], source, db.id, req.model or "auto", len(turns))

    # The knowledge base holds documents; these checks are about database queries.
    uses_data = source != "knowledge_base"
    answering_us = bool(turns) and turns[-1]["route"] == ROUTE_CLARIFY
    if uses_data and is_smalltalk(message) and not answering_us:
        return _smalltalk(sid, message, turns, source, model_id, db, t0)

    # "Run it" agrees to what the last answer offered: nothing to ask about.
    agreeing = bool(turns) and not answering_us and is_confirmation(message)
    asked = clarify_gate(message, turns) if uses_data and not agreeing else None
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
        with llm_client.model_override(model_id):
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
    )
    ran_query = result.route == "sql" and not resp.error_type
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
        },
    )
    return resp


def _run_sql_pipeline(
    sid: str, message: str, turns: list[dict], t0: float
) -> tuple[ChatResponse, Optional[dict]]:
    src = data_sources.active()
    entity_codes: list[str] = []
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
        examples = retrieve_examples(message, k=2)
    except Exception:
        examples = []

    # ── 3. Generate SQL (text-only history so follow-ups resolve) ────────
    chat_events.step("sql", "Writing the query")
    history_block = _sql_history_block(turns) if turns else None
    sql_messages = build_sql_messages(
        sql_question, table_cards, examples, history_block=history_block,
    )
    raw_sql, provider_used, usage = call_llm(
        sql_messages, max_tokens=800, temperature=0, task="sql_generation"
    )
    log.info(
        "[chat] SQL gen provider=%s input=%d output=%d",
        provider_used, usage["input_tokens"], usage["output_tokens"],
    )

    sql = extract_sql(raw_sql)
    chat_events.done("sql", sql=sql or None, status="done" if sql else "error",
                     detail=None if sql else "No query could be written")
    if not sql:
        log.info("[chat] no SQL extracted")
        return ChatResponse(
            answer=_NO_DATA_ANSWER,
            session_id=sid,
            tables_used=table_names,
            provider=provider_used,
            provider_label=PROVIDER_LABELS.get(provider_used, provider_used),
            used_llm_fallback=used_llm_fallback,
            error_type="no_data",
        ), None

    # ── 4. Validate SQL ───────────────────────────────────────────────────
    ok, reason = validate(sql, src.dialect)
    if not ok:
        chat_events.done("sql", status="error", detail="The query was not safe to run", sql=sql)
        log.info("[chat] SQL validation failed: %s", reason)
        return ChatResponse(
            answer=_SQL_FAIL_ANSWER,
            session_id=sid,
            sql=sql,
            tables_used=table_names,
            provider=provider_used,
            provider_label=PROVIDER_LABELS.get(provider_used, provider_used),
            used_llm_fallback=used_llm_fallback,
            error_type="no_data",
        ), None

    # ── 5. Execute across the relevant estate(s) (retry once if ALL fail) ──
    run_step = "run"
    lint_err = src.lint(sql, message) if src.lint else None
    if lint_err:
        # Valid SQL that would give wrong numbers: don't run it, have it corrected.
        log.info("[chat] query flagged before running: %s", lint_err[:120])
        chat_events.step(run_step, "Checking the query")
        exec_results = {e: ([], [], lint_err) for e in estate_ids}
    else:
        chat_events.step(run_step, f"Running on {src.label}")
        exec_results = _execute_multi(sql, estate_ids)
    ok_estates = [e for e in estate_ids if not exec_results[e][2]]
    failed_estates = [e for e in estate_ids if exec_results[e][2]]

    if not ok_estates:
        first_err = exec_results[estate_ids[0]][2]
        log.info("[chat] SQL exec error on all estates — retrying once: %s", first_err[:120])
        chat_events.done(run_step, status="error",
                         detail="It broke a data rule, correcting it" if lint_err
                         else "The database rejected the query")
        chat_events.step("fix", "Correcting the query")
        retry_messages = build_sql_messages(
            sql_question, table_cards, examples,
            prior_error=first_err, prior_sql=sql,
            history_block=history_block,
        )
        # The default model's first attempt failed: escalate the single retry.
        raw_retry, provider_used, usage = call_llm(
            retry_messages, max_tokens=800, temperature=0, task="sql_self_correction",
            tier="reasoning" if CHAT_ESCALATE_TO_REASONING else "main",
        )
        sql_retry = extract_sql(raw_retry)
        chat_events.done("fix", sql=sql_retry or None)
        if sql_retry:
            ok2, _ = validate(sql_retry, src.dialect)
            if ok2 and src.lint and src.lint(sql_retry, message):
                ok2 = False              # still wrong: better no answer than a wrong one
            if ok2:
                run_step = "rerun"
                chat_events.step(run_step, f"Running again on {src.label}")
                exec_results = _execute_multi(sql_retry, estate_ids)
                ok_estates = [e for e in estate_ids if not exec_results[e][2]]
                failed_estates = [e for e in estate_ids if exec_results[e][2]]
                if ok_estates:
                    sql = sql_retry

    if not ok_estates:
        chat_events.done(run_step, status="error", detail="The query could not be run")
        log.info("[chat] SQL still failing after retry on every estate")
        record_query(
            message, sql, provider_used, table_names,
            success=False, error=exec_results[estate_ids[0]][2], source=src.id,
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
        ), None

    # ── 6. Merge per-estate results, compute stats & build answer ─────────
    multi_estate = len(estate_ids) > 1
    columns = exec_results[ok_estates[0]][0]
    combined_rows: list[dict] = []
    estate_breakdown: dict[str, dict] = {}
    for eid in ok_estates:
        est_columns, est_rows, _ = exec_results[eid]
        label = src.scopes[eid]["label"]
        tagged_rows = (
            [{**r, "_source_estate": label} for r in est_rows] if multi_estate else est_rows
        )
        combined_rows.extend(tagged_rows)
        estate_breakdown[label] = {"row_count": len(est_rows), **_compute_stats(est_columns, est_rows)}
    if multi_estate and "_source_estate" not in columns:
        columns = columns + ["_source_estate"]

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
    chat_events.attach("query_id", record_query(
        message, sql, provider_used, table_names, success=True, source=src.id))
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
        set_success(query_id, req.rating != "down")
        if req.rating == "down":
            from retrieval import forget_example
            forget_example(query_id, meta.get("database"))
    return {"ok": True}


@router.delete("/sessions/{session_id}")
def session_remove(session_id: str):
    deleted = delete_session(session_id)
    if not deleted:
        return JSONResponse(status_code=404, content={"detail": "session not found"})
    return {"ok": True}
