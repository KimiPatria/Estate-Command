"""
EPMS Data Chatbot — FastAPI router
Mounted at /chat on dashboard_server.py (port 8001).

POST /chat/message — conversational Q&A backed by the EPMS database.
Session-scoped context: a lightweight router decides per message whether to
answer from conversation history (no SQL) or run the full pipeline:
retrieve tables → generate SQL → execute → reason → respond.
"""

import decimal
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from chat_history import (
    add_turn,
    delete_session,
    ensure_session,
    get_turns,
    list_sessions,
)
from config import (
    engine,
    CHAT_HISTORY_DATA_TURNS,
    CHAT_HISTORY_MAX_ROWS,
    CHAT_HISTORY_TEXT_TURNS,
    ESTATE_ORDER,
    ESTATES,
    RAG_SOURCE_DEFAULT,
)
from error_handler import is_off_domain
from estates import detect_estate_mentions, strip_estate_mentions
from guardrails import apply_input_guardrails, apply_output_guardrails
import llm_client
from llm import call_llm, get_provider, PROVIDER_LABELS
from prompt_builder import build_sql_messages, extract_sql
from prompts import load_prompt
from query_history import record_query
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
) -> list[dict]:
    preview = rows[:50]
    preview_json = json.dumps(preview, default=str, ensure_ascii=False)
    stats_json   = json.dumps(stats,   default=str, ensure_ascii=False)
    extra = ""
    if estate_breakdown:
        breakdown_json = json.dumps(estate_breakdown, default=str, ensure_ascii=False)
        extra += f"\nPer-estate breakdown (each estate's own row count + stats): {breakdown_json}"
    if failed_estates:
        extra += (
            f"\nNote: data from {', '.join(failed_estates)} could not be "
            f"retrieved and is excluded from the figures above."
        )
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


class ChatResponse(BaseModel):
    answer: str
    session_id: Optional[str] = None
    route: str = "sql"          # "sql" | "context" | "knowledge_base" | "both"
    source: str = "sql"         # which RAG source(s) actually served this answer
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


# ── helpers ────────────────────────────────────────────────────────────────

def _coerce(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return str(v)
    if isinstance(v, (memoryview, bytes, bytearray)):
        return None
    return v


def _execute_safe(
    sql: str, max_rows: int = 200, engine_obj=None
) -> tuple[list[str], list[dict], Optional[str]]:
    engine_obj = engine if engine_obj is None else engine_obj
    try:
        limited = ensure_limit(sql, max_rows)
        with engine_obj.connect() as conn:
            result = conn.execute(text(limited))
            columns = list(result.keys())
            rows = [
                {k: _coerce(v) for k, v in dict(r._mapping).items()}
                for r in result.fetchall()
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
    results: dict[str, tuple[list[str], list[dict], Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=len(estate_ids)) as pool:
        futures = {
            pool.submit(_execute_safe, sql, max_rows, ESTATES[eid]["engine"]): eid
            for eid in estate_ids
        }
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return results


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


def _build_context_messages(message: str, turns: list[dict]) -> list[dict]:
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
            f"Rows (first {len(rows)} of {t['row_count']}): {rows_json}"
        )

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
    messages = _build_context_messages(message, turns)
    answer_raw, provider_used, usage = call_llm(
        messages, max_tokens=600, temperature=0, task="chat_context_answer"
    )
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
        provider_label=PROVIDER_LABELS.get(provider_used, provider_used),
    )


# ── endpoint ───────────────────────────────────────────────────────────────

@router.post("/message", response_model=ChatResponse)
def chat_message(req: ChatRequest) -> ChatResponse:
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

    sid = ensure_session(req.session_id, message)
    turns = get_turns(sid, limit=CHAT_HISTORY_TEXT_TURNS + CHAT_HISTORY_DATA_TURNS)
    log.info('[chat] message="%s" session=%s source=%s turns=%d', message[:120], sid[:8], source, len(turns))

    session = SessionContext(session_id=sid, turns=turns)
    try:
        result = dispatch_rag(source, message, session, sql_source=_SQL_SOURCE)
    except Exception as exc:
        log.warning("[chat] source=%s failed: %s", source, exc)
        return ChatResponse(
            answer=f"The '{source}' source is unavailable right now: {exc}",
            session_id=sid,
            route=source,
            source=source,
            error_type="source_unavailable",
        )

    resp = ChatResponse(
        answer=result.answer,
        session_id=sid,
        route=result.route,
        source=result.source,
        sql=result.sql,
        columns=result.columns,
        rows=result.rows[:100],
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
    add_turn(
        sid, message, resp.answer,
        sql=resp.sql if not resp.error_type else None,
        columns=resp.columns, rows=resp.rows,
        row_count=resp.row_count, stats=result.stats,
        route=resp.route,
    )
    return resp


def _run_sql_pipeline(
    sid: str, message: str, turns: list[dict], t0: float
) -> tuple[ChatResponse, Optional[dict]]:
    mentioned_estates = detect_estate_mentions(message)
    estate_ids = mentioned_estates or list(ESTATE_ORDER)
    # SQL generation only — table retrieval and the final answer still see
    # the original message. See strip_estate_mentions() docstring for why.
    sql_question = strip_estate_mentions(message, mentioned_estates) if mentioned_estates else message

    # ── 1. Retrieve tables (augmented with the prior question on follow-ups) ─
    retrieval_query = message
    if turns:
        retrieval_query = f"{turns[-1]['question']}\n{message}"
    table_cards, used_llm_fallback = retrieve_tables(retrieval_query, k=5)
    table_names = [c.name for c in table_cards]
    log.info("[chat] tables: %s (llm_fallback=%s)", table_names, used_llm_fallback)

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
    ok, reason = validate(sql)
    if not ok:
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
    exec_results = _execute_multi(sql, estate_ids)
    ok_estates = [e for e in estate_ids if not exec_results[e][2]]
    failed_estates = [e for e in estate_ids if exec_results[e][2]]

    if not ok_estates:
        first_err = exec_results[estate_ids[0]][2]
        log.info("[chat] SQL exec error on all estates — retrying once: %s", first_err[:120])
        retry_messages = build_sql_messages(
            sql_question, table_cards, examples,
            prior_error=first_err, prior_sql=sql,
            history_block=history_block,
        )
        raw_retry, provider_used, usage = call_llm(
            retry_messages, max_tokens=800, temperature=0, task="sql_self_correction"
        )
        sql_retry = extract_sql(raw_retry)
        if sql_retry:
            ok2, _ = validate(sql_retry)
            if ok2:
                exec_results = _execute_multi(sql_retry, estate_ids)
                ok_estates = [e for e in estate_ids if not exec_results[e][2]]
                failed_estates = [e for e in estate_ids if exec_results[e][2]]
                if ok_estates:
                    sql = sql_retry

    if not ok_estates:
        log.info("[chat] SQL still failing after retry on every estate")
        record_query(
            message, sql, provider_used, table_names,
            success=False, error=exec_results[estate_ids[0]][2],
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
        label = ESTATES[eid]["label"]
        tagged_rows = (
            [{**r, "_source_estate": label} for r in est_rows] if multi_estate else est_rows
        )
        combined_rows.extend(tagged_rows)
        estate_breakdown[label] = {"row_count": len(est_rows), **_compute_stats(est_columns, est_rows)}
    if multi_estate and "_source_estate" not in columns:
        columns = columns + ["_source_estate"]

    failed_labels = [ESTATES[e]["label"] for e in failed_estates]
    stats = _compute_stats(columns, combined_rows)
    answer_messages = _build_answer_messages(
        message, sql, combined_rows, columns, stats,
        estate_breakdown=estate_breakdown if multi_estate else None,
        failed_estates=failed_labels or None,
    )
    answer_raw, provider_used, usage2 = call_llm(
        answer_messages, max_tokens=600, temperature=0, task="chat_answer"
    )
    log.info(
        "[chat] reasoning provider=%s input=%d output=%d",
        provider_used, usage2["input_tokens"], usage2["output_tokens"],
    )

    answer = answer_raw.strip() or _NO_DATA_ANSWER
    answer = apply_output_guardrails(answer, context="chat")

    # ── 7. Record to history & refresh index ─────────────────────────────
    record_query(message, sql, provider_used, table_names, success=True)
    try:
        from retrieval import refresh_query_history_index
        refresh_query_history_index()
    except Exception:
        pass

    elapsed = time.monotonic() - t0
    log.info(
        "[chat] done %.2fs rows=%d estates=%s failed=%s",
        elapsed, len(combined_rows), [ESTATES[e]["label"] for e in ok_estates], failed_labels,
    )

    return ChatResponse(
        answer=answer,
        session_id=sid,
        sql=sql,
        columns=columns,
        rows=combined_rows[:100],
        row_count=len(combined_rows),
        tables_used=table_names,
        provider=provider_used,
        provider_label=PROVIDER_LABELS.get(provider_used, provider_used),
        used_llm_fallback=used_llm_fallback,
        estates_used=[ESTATES[e]["label"] for e in ok_estates],
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


@router.delete("/sessions/{session_id}")
def session_remove(session_id: str):
    deleted = delete_session(session_id)
    if not deleted:
        return JSONResponse(status_code=404, content={"detail": "session not found"})
    return {"ok": True}
