"""
Manual RAG source toggle: SQL pipeline vs. Bedrock Knowledge Base, side by side.

This is a POC toggle, not a router. Callers pick a source explicitly per
request ("sql" | "knowledge_base" | "both") via config.RAG_SOURCE_DEFAULT or
ChatRequest.source — there is no classifier or agentic tool-selection here.
That is a deliberately separate, future task once both sources are validated
independently (see the task brief). Until then:

  * SqlRagSource wraps chat_router's existing pipeline (_route_followup /
    _answer_from_context / _run_sql_pipeline) completely unchanged — this
    module never reimplements or edits that logic, only adapts its output.
  * KnowledgeBaseRagSource is the new path: Bedrock RetrieveAndGenerate
    against a Knowledge Base of ingested contract documents.
  * "both" mode runs the two independently and asks the model to synthesize
    one answer from both contexts. This is a naive parallel-merge — no
    ID-handoff chaining between SQL results and KB metadata filtering. That
    chaining is future work once the router/tool-selection layer exists.

Every retrieve_and_answer() call is wrapped in llm_client.rag_instrumentation
so its LLM calls are tagged with the source and their usage is summed; dispatch()
then writes one structured JSONL line per request to config.RAG_LOG_PATH so
SQL-only / KB-only / both-mode runs can be compared (item #5 of the task).
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import llm_client
from config import BEDROCK_KB_ID, BEDROCK_KB_MODEL_ID, AWS_REGION, KB_TOP_K, RAG_LOG_PATH
from prompts import load_prompt

log = logging.getLogger(__name__)

VALID_SOURCES = ("sql", "knowledge_base", "both")


# ── shared types ──────────────────────────────────────────────────────────

@dataclass
class SessionContext:
    """Everything a retriever needs to know about the conversation so far.

    `turns` is the existing chat_history payload (chat_history.get_turns) —
    already used by the SQL pipeline for follow-up routing/context replay.
    """
    session_id: str
    turns: list[dict] = field(default_factory=list)


@dataclass
class RetrievalResult:
    """Normalized output of any RAG source — the only shape chat_router reads."""
    answer: str
    source: str                       # "sql" | "knowledge_base" | "both"
    route: str = "sql"                # UI sub-route: "sql" | "context" | "knowledge_base" | "both"
    sql: Optional[str] = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    tables_used: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)   # KB-only: retrieved chunk excerpts
    provider: Optional[str] = None
    provider_label: Optional[str] = None
    used_llm_fallback: bool = False
    error_type: Optional[str] = None
    stats: Optional[dict] = None      # forwarded to chat_history.add_turn(stats=...)
    estates_used: list[str] = field(default_factory=list)    # sql-only: estate(s) that answered
    estates_failed: list[str] = field(default_factory=list)  # sql-only: estate(s) excluded on error
    # instrumentation (item #5)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class BaseRagSource(ABC):
    """Common interface both retrieval paths implement."""

    name: str

    @abstractmethod
    def retrieve_and_answer(self, query: str, session: SessionContext) -> RetrievalResult:
        ...


# ── SQL RAG (existing pipeline, wrapped not rewritten) ──────────────────────

class SqlRagSource(BaseRagSource):
    """Pass-through adapter around chat_router's existing SQL pipeline.

    Takes the pipeline's own functions as callables (dependency injection)
    rather than importing chat_router at module scope, so this module has no
    hard dependency on the router and stays trivially unit-testable. The
    control flow here is exactly what chat_message() used to do inline:
    try the cheap context-router first, fall back to the full SQL pipeline.
    """

    name = "sql"

    def __init__(
        self,
        route_followup_fn: Callable[[str, list[dict]], str],
        answer_from_context_fn: Callable[[str, str, list[dict], float], Any],
        run_sql_pipeline_fn: Callable[[str, str, list[dict], float], tuple[Any, Optional[dict]]],
    ):
        self._route_followup = route_followup_fn
        self._answer_from_context = answer_from_context_fn
        self._run_sql_pipeline = run_sql_pipeline_fn

    def retrieve_and_answer(self, query: str, session: SessionContext) -> RetrievalResult:
        t0 = time.monotonic()
        with llm_client.rag_instrumentation(self.name) as usage_log:
            if session.turns and self._route_followup(query, session.turns) == "context":
                resp = self._answer_from_context(session.session_id, query, session.turns, t0)
                stats = None
                route = "context"
            else:
                resp, stats = self._run_sql_pipeline(session.session_id, query, session.turns, t0)
                route = "sql"
        latency_ms = int((time.monotonic() - t0) * 1000)

        return RetrievalResult(
            answer=resp.answer,
            source=self.name,
            route=route,
            sql=resp.sql,
            columns=resp.columns,
            rows=resp.rows,
            row_count=resp.row_count,
            tables_used=resp.tables_used,
            provider=resp.provider,
            provider_label=resp.provider_label,
            used_llm_fallback=resp.used_llm_fallback,
            error_type=resp.error_type,
            stats=stats,
            estates_used=resp.estates_used,
            estates_failed=resp.estates_failed,
            llm_calls=len(usage_log),
            input_tokens=sum(u["input_tokens"] for u in usage_log),
            output_tokens=sum(u["output_tokens"] for u in usage_log),
            latency_ms=latency_ms,
        )


# ── Bedrock Knowledge Base RAG (new, POC) ────────────────────────────────────

# In-process only — Bedrock's own multi-turn session id (distinct from our
# chat session_id), so a KB conversation continues its managed memory across
# turns. Lost on restart; fine for a POC toggle. Whether/how much this
# actually saves in tokens vs. re-sending context has NOT been measured —
# check llm_calls.jsonl / rag_calls.jsonl once real AWS access exists.
_kb_sessions: dict[str, str] = {}


class KnowledgeBaseRagSource(BaseRagSource):
    """Bedrock Knowledge Base retriever, using the fully-managed
    RetrieveAndGenerate API by default.

    Extension point: to get tighter prompt-template control (e.g. a custom
    system prompt around the retrieved chunks, or to post-process citations
    before generation), swap this for a manual Retrieve + Converse
    implementation — call bedrock-agent-runtime.retrieve() for chunks, then
    llm_client.chat() to generate. That pairs naturally with this module's
    existing instrumentation (rag_instrumentation) since it would go through
    llm_client like every other call. Not implemented here — out of scope for
    the toggle; RetrieveAndGenerate is the default until we need that control.
    """

    name = "knowledge_base"

    def __init__(
        self,
        knowledge_base_id: str | None = None,
        model_id: str | None = None,
        top_k: int | None = None,
        region: str | None = None,
    ):
        self.knowledge_base_id = knowledge_base_id or BEDROCK_KB_ID
        self.model_id = model_id or BEDROCK_KB_MODEL_ID
        self.top_k = top_k or KB_TOP_K
        self.region = region or AWS_REGION
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-agent-runtime", region_name=self.region)
        return self._client

    def retrieve_and_answer(self, query: str, session: SessionContext) -> RetrievalResult:
        if not self.knowledge_base_id:
            raise RuntimeError(
                "BEDROCK_KB_ID is not configured — the Knowledge Base hasn't been "
                "provisioned yet (see infra/terraform/bedrock_kb.tf, marked NOT YET "
                "APPLIED). Set BEDROCK_KB_ID once it exists."
            )

        model_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{self.model_id}"
        request: dict = {
            "input": {"text": query},
            "retrieveAndGenerateConfiguration": {
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": self.knowledge_base_id,
                    "modelArn": model_arn,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {"numberOfResults": self.top_k},
                    },
                },
            },
        }
        kb_session_id = _kb_sessions.get(session.session_id)
        if kb_session_id:
            request["sessionId"] = kb_session_id

        t0 = time.monotonic()
        try:
            client = self._get_client()
            response = client.retrieve_and_generate(**request)
        except Exception as exc:
            raise RuntimeError(f"Bedrock KB retrieve_and_generate failed: {exc}") from exc
        latency_ms = int((time.monotonic() - t0) * 1000)

        new_kb_session_id = response.get("sessionId")
        if new_kb_session_id:
            _kb_sessions[session.session_id] = new_kb_session_id

        answer = (response.get("output") or {}).get("text", "") or ""
        citations: list[dict] = []
        for c in response.get("citations", []):
            for ref in c.get("retrievedReferences", []):
                citations.append({
                    "text": ((ref.get("content") or {}).get("text") or "")[:500],
                    "location": ref.get("location", {}),
                })

        return RetrievalResult(
            answer=answer.strip(),
            source=self.name,
            route=self.name,
            citations=citations,
            llm_calls=1,
            # RetrieveAndGenerate does not currently return token usage in its
            # response — unlike a direct Converse call. Leave at 0 rather than
            # guess; if per-call cost tracking is needed, switch to the manual
            # Retrieve + Converse path (see class docstring), whose Converse
            # call does return usage_metadata via llm_client.
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
        )


def _default_kb_source() -> KnowledgeBaseRagSource:
    return KnowledgeBaseRagSource()


# ── "both" mode: naive parallel merge (placeholder pending future router) ──

def _run_both(
    query: str,
    session: SessionContext,
    sql_source: BaseRagSource,
    kb_source: BaseRagSource,
) -> RetrievalResult:
    sql_result = sql_source.retrieve_and_answer(query, session)

    try:
        kb_result = kb_source.retrieve_and_answer(query, session)
    except Exception as exc:
        log.warning("[rag] KB source failed in 'both' mode — degrading to SQL-only answer: %s", exc)
        sql_result.source = "both"
        return sql_result

    # NAIVE PLACEHOLDER STRATEGY (explicitly out of scope to improve here):
    # just concatenate both answers and ask the model to synthesize one reply.
    # No ID-handoff chaining (e.g. a contract id from the SQL result narrowing
    # a metadata-filtered KB query) — that's future router work.
    synthesis_messages = [
        {"role": "system", "content": load_prompt("chat_synthesis_system")},
        {
            "role": "user",
            "content": (
                f"Question: {query}\n\n"
                f"--- Structured database answer (SQL) ---\n{sql_result.answer or '(no data found)'}\n\n"
                f"--- Knowledge base answer (documents) ---\n{kb_result.answer or '(nothing found)'}"
            ),
        },
    ]
    t0 = time.monotonic()
    with llm_client.rag_instrumentation("both") as synth_log:
        synth = llm_client.chat(
            synthesis_messages, tier="main", temperature=0, max_tokens=600,
            task="rag_synthesis_both",
        )
    synth_latency_ms = int((time.monotonic() - t0) * 1000)

    return RetrievalResult(
        answer=synth.text.strip() or sql_result.answer,
        source="both",
        route="both",
        sql=sql_result.sql,
        columns=sql_result.columns,
        rows=sql_result.rows,
        row_count=sql_result.row_count,
        tables_used=sql_result.tables_used,
        citations=kb_result.citations,
        provider=sql_result.provider,
        provider_label=sql_result.provider_label,
        used_llm_fallback=sql_result.used_llm_fallback,
        stats=sql_result.stats,
        estates_used=sql_result.estates_used,
        estates_failed=sql_result.estates_failed,
        llm_calls=sql_result.llm_calls + kb_result.llm_calls + len(synth_log),
        input_tokens=sql_result.input_tokens + kb_result.input_tokens
                     + sum(u["input_tokens"] for u in synth_log),
        output_tokens=sql_result.output_tokens + kb_result.output_tokens
                      + sum(u["output_tokens"] for u in synth_log),
        latency_ms=sql_result.latency_ms + kb_result.latency_ms + synth_latency_ms,
    )


# ── entry point ──────────────────────────────────────────────────────────

def _log_rag_summary(**fields) -> None:
    """One JSONL line per /chat/message request — item #5 instrumentation.
    Fail-open: telemetry must never break a request."""
    record = {"ts": datetime.now().isoformat(timespec="seconds"), **fields}
    log.info(
        "rag_call source=%s route=%s ok=%s llm_calls=%s input=%s output=%s latency_ms=%s",
        record.get("source"), record.get("route"), record.get("ok"),
        record.get("llm_calls"), record.get("input_tokens"), record.get("output_tokens"),
        record.get("latency_ms"),
    )
    if not RAG_LOG_PATH:
        return
    try:
        with open(RAG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        log.debug("rag call log write failed: %s", exc)


def dispatch(
    source: str,
    query: str,
    session: SessionContext,
    *,
    sql_source: BaseRagSource,
    kb_source: BaseRagSource | None = None,
) -> RetrievalResult:
    """Route a request to the selected RAG source(s). Raises on failure —
    callers decide how to surface that to the user."""
    if source not in VALID_SOURCES:
        raise ValueError(f"Unknown RAG source {source!r}; expected one of {VALID_SOURCES}")

    kb_source = kb_source or _default_kb_source()
    t0 = time.monotonic()
    try:
        if source == "sql":
            result = sql_source.retrieve_and_answer(query, session)
        elif source == "knowledge_base":
            result = kb_source.retrieve_and_answer(query, session)
        else:  # both
            result = _run_both(query, session, sql_source, kb_source)
    except Exception as exc:
        _log_rag_summary(
            source=source, session_id=session.session_id, query=query[:200],
            ok=False, error=str(exc)[:300],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        raise

    _log_rag_summary(
        source=result.source, route=result.route, session_id=session.session_id,
        query=query[:200], ok=True, llm_calls=result.llm_calls,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
    )
    return result
