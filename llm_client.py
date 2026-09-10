"""
Provider-agnostic LLM client layer (Bedrock migration seam).

Every LLM call in the app goes through chat() here. The underlying chat model
is a LangChain BaseChatModel resolved from config.LLM_PROVIDER:

  * "groq"    -> langchain_groq.ChatGroq            (today's default)
  * "bedrock" -> langchain_aws.ChatBedrockConverse  (inert until AWS creds exist)

Call sites pass OpenAI-style message dicts ({"role", "content"}, plus
tool-call dicts for agents) and get back a normalized LLMResult — no
provider-specific response shapes leak out of this module. Swapping Groq for
Bedrock Nova is therefore a config change (LLM_PROVIDER + BEDROCK_MODEL_ID),
not a code change at call sites.

Structured logging (#9 of the migration plan): every call emits one JSONL
record {ts, task, provider, model, input_tokens, output_tokens, latency_ms,
ok} to config.LLM_LOG_PATH (fail-open) and a token_usage log line — the
"before" numbers for any future Groq-vs-Nova comparison.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

from config import (
    AWS_REGION,
    BEDROCK_MODEL_ID,
    BEDROCK_ROUTING_MODEL_ID,
    GROQ_API_KEY,
    GROQ_MODEL,
    LLM_LOG_PATH,
    LLM_PROVIDER,
    ROUTING_MODEL,
)

log = logging.getLogger(__name__)
_call_log = logging.getLogger("llm.calls")

_SUPPORTED_PROVIDERS = ("groq", "bedrock")

# Model tiers: call sites that don't care about exact model ids ask for a tier
# and each provider maps it to its own id. "main" answers/generates SQL;
# "routing" is the cheap/fast classifier tier.
_TIER_MODELS = {
    "groq":    {"main": GROQ_MODEL,       "routing": ROUTING_MODEL},
    "bedrock": {"main": BEDROCK_MODEL_ID, "routing": BEDROCK_ROUTING_MODEL_ID},
}

_model_cache: dict[tuple, Any] = {}
_cache_lock = Lock()

# Cross-cutting per-request instrumentation for the manual RAG source toggle
# (rag_sources.py). Every chat() call already writes a structured JSONL record
# via _log_call(); this just tags that record with which RAG source triggered
# it and, when a collector list is active, appends the call's usage to it — so
# an adapter (e.g. SqlRagSource) can report exact llm_calls/tokens/latency for
# a whole pipeline run without any call site needing to know about it.
_rag_source_var: ContextVar[str | None] = ContextVar("rag_source", default=None)
_rag_collector_var: ContextVar[list[dict] | None] = ContextVar("rag_collector", default=None)


@contextmanager
def rag_instrumentation(source: str):
    """Tag every chat() call made within this block with `source` and collect
    its usage. Yields the collector list (one dict per call: task, tokens, latency_ms)."""
    collector: list[dict] = []
    src_token = _rag_source_var.set(source)
    col_token = _rag_collector_var.set(collector)
    try:
        yield collector
    finally:
        _rag_source_var.reset(src_token)
        _rag_collector_var.reset(col_token)


@dataclass
class LLMResult:
    """Normalized response — the only shape callers ever see."""
    text: str
    provider: str
    model_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int
    message: Any = None   # the LangChain AIMessage (tool_calls etc.), provider-normalized

    @property
    def usage(self) -> dict:
        """Usage dict in the legacy call_llm() shape."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model_id,
        }


def resolve_model(model: str | None = None, tier: str = "main") -> str:
    """Explicit model id wins; otherwise the active provider's model for `tier`."""
    if model:
        return model
    return _TIER_MODELS[LLM_PROVIDER][tier]


def get_chat_model(
    *,
    model: str | None = None,
    tier: str = "main",
    temperature: float = 0.0,
    max_tokens: int = 800,
    json_mode: bool = False,
    extra_kwargs: dict | None = None,
):
    """Build (or reuse) the LangChain chat model for the active provider.

    Never called at import time, so a missing optional dependency or missing
    credentials only fail when a call is actually attempted.
    """
    if LLM_PROVIDER not in _SUPPORTED_PROVIDERS:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER={LLM_PROVIDER!r}; expected one of {_SUPPORTED_PROVIDERS}"
        )

    model_id = resolve_model(model, tier)
    key = (
        LLM_PROVIDER, model_id, temperature, max_tokens, json_mode,
        tuple(sorted((extra_kwargs or {}).items())),
    )
    with _cache_lock:
        cached = _model_cache.get(key)
    if cached is not None:
        return cached

    if LLM_PROVIDER == "groq":
        if not GROQ_API_KEY:
            raise RuntimeError("Groq API key not configured")
        from langchain_groq import ChatGroq

        model_kwargs = dict(extra_kwargs or {})
        # ChatGroq rejects params it defines as first-class fields when they
        # arrive via model_kwargs — lift those out.
        init_kwargs = {k: model_kwargs.pop(k)
                       for k in ("reasoning_effort", "reasoning_format")
                       if k in model_kwargs}
        if json_mode:
            model_kwargs["response_format"] = {"type": "json_object"}
        instance = ChatGroq(
            api_key=GROQ_API_KEY,
            model=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            model_kwargs=model_kwargs,
            **init_kwargs,
        )
    else:  # bedrock
        try:
            from langchain_aws import ChatBedrockConverse
        except ImportError as exc:
            raise RuntimeError(
                "LLM_PROVIDER=bedrock but langchain-aws is not installed. "
                "Run: pip install langchain-aws"
            ) from exc
        # Nova (Converse API) has no JSON mode; prompts already demand JSON and
        # callers parse defensively, so json_mode is a no-op here.
        instance = ChatBedrockConverse(
            model=model_id,
            region_name=AWS_REGION,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    with _cache_lock:
        _model_cache[key] = instance
    return instance


def _flatten_content(content: Any) -> str:
    """LangChain content may be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _log_call(record: dict) -> None:
    _call_log.info(
        "token_usage task=%s provider=%s model=%s input=%d output=%d latency_ms=%d ok=%s",
        record["task"], record["provider"], record["model"],
        record["input_tokens"], record["output_tokens"],
        record["latency_ms"], record["ok"],
    )
    if not LLM_LOG_PATH:
        return
    try:
        with open(LLM_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # never let telemetry break a request
        log.debug("llm call log write failed: %s", exc)


def chat(
    messages: list[dict],
    *,
    model: str | None = None,
    tier: str = "main",
    temperature: float = 0.0,
    max_tokens: int = 800,
    json_mode: bool = False,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    task: str = "general",
    extra_kwargs: dict | None = None,
) -> LLMResult:
    """Send OpenAI-style message dicts to the configured provider.

    tools: OpenAI/JSON-schema-format tool definitions (see tool_schemas.py);
    LangChain translates them to each provider's wire format via bind_tools.
    task: short label for the structured call log (e.g. "sql_generation").
    """
    chat_model = get_chat_model(
        model=model, tier=tier, temperature=temperature,
        max_tokens=max_tokens, json_mode=json_mode, extra_kwargs=extra_kwargs,
    )
    runnable = chat_model
    if tools:
        bind_kwargs = {"tool_choice": tool_choice} if tool_choice else {}
        runnable = chat_model.bind_tools(tools, **bind_kwargs)

    model_id = resolve_model(model, tier)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "task": task,
        "provider": LLM_PROVIDER,
        "model": model_id,
        "rag_source": _rag_source_var.get(),
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "latency_ms": 0,
        "ok": False,
    }
    t0 = time.monotonic()
    try:
        ai_message = runnable.invoke(messages)
    except Exception as exc:
        record["latency_ms"] = int((time.monotonic() - t0) * 1000)
        record["error"] = str(exc)[:300]
        _log_call(record)
        raise

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = getattr(ai_message, "usage_metadata", None) or {}
    record.update(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        latency_ms=latency_ms,
        ok=True,
    )
    _log_call(record)

    collector = _rag_collector_var.get()
    if collector is not None:
        collector.append({
            "task": task,
            "input_tokens": record["input_tokens"],
            "output_tokens": record["output_tokens"],
            "latency_ms": latency_ms,
        })

    return LLMResult(
        text=_flatten_content(ai_message.content).strip(),
        provider=LLM_PROVIDER,
        model_id=model_id,
        input_tokens=record["input_tokens"],
        output_tokens=record["output_tokens"],
        total_tokens=record["total_tokens"],
        latency_ms=latency_ms,
        message=ai_message,
    )
