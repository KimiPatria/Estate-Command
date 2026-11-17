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
    BEDROCK_REASONING_MODEL_ID,
    BEDROCK_ROUTING_MODEL_ID,
    GROQ_API_KEY,
    GROQ_MODEL,
    LLM_LOG_PATH,
    LLM_PROVIDER,
    PROMPT_CACHE,
    ROUTING_MODEL,
)

log = logging.getLogger(__name__)
_call_log = logging.getLogger("llm.calls")

_SUPPORTED_PROVIDERS = ("groq", "bedrock")

# Model tiers: call sites that don't care about exact model ids ask for a tier
# and each provider maps it to its own id. "main" is the default (retrieval,
# SQL generation, short answers); "reasoning" is the stronger model callers
# escalate to for analysis and agents; "routing" is the cheap/fast classifier
# tier. Groq has a single model, so "reasoning" is the same as "main" there.
_TIER_MODELS = {
    "groq":    {"main": GROQ_MODEL,       "reasoning": GROQ_MODEL,
                "routing": ROUTING_MODEL},
    "bedrock": {"main": BEDROCK_MODEL_ID, "reasoning": BEDROCK_REASONING_MODEL_ID,
                "routing": BEDROCK_ROUTING_MODEL_ID},
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


# Per-request model override (chat page model selector). Applies only to the
# "main" and "reasoning" tiers, so classifiers on the "routing" tier are
# untouched, and only under Bedrock (the ids are Bedrock ids). A ContextVar,
# not a global, so concurrent requests with different choices cannot collide.
_model_override_var: ContextVar[str | None] = ContextVar("model_override", default=None)


@contextmanager
def model_override(model_id: str | None):
    """Force every main/reasoning-tier chat() call in this block onto `model_id`."""
    token = _model_override_var.set(model_id)
    try:
        yield
    finally:
        _model_override_var.reset(token)


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


# ── prompt caching ─────────────────────────────────────────────────────────
#
# Bedrock caches a prompt prefix up to an explicit cachePoint block for five
# minutes (the clock restarts on every hit), and bills tokens read back from
# it at a discount. Nova caches system and message content, not tools, and only
# a prefix of about 1,000 tokens or more; up to four points per request.
#
# Call sites keep plain-string content (tests and logs read it as text) and
# put CACHE_POINT where everything before it stays the same from one request
# to the next. _prepare_messages() turns the marker into a cachePoint block for
# Bedrock, and simply drops it for Groq or when PROMPT_CACHE is off. The end of
# the first system message always gets one: system prompts here are fixed text.
#
# Measured on this account (Nova Lite, Nova 2 Lite, Nova Pro, Nov 2026): there
# is no practical minimum (a 7-token prefix was cached); a point in the middle
# of a message works; a transcript that grew by one turn reads the previous
# request's prefix back, as long as that spot still carries a point; and a new
# entry takes a second or two to become readable.

CACHE_POINT = "\x00cache-point\x00"
_MAX_CACHE_POINTS = 4


def _prepare_messages(messages: list[dict]) -> list[dict]:
    use_cache = PROMPT_CACHE and LLM_PROVIDER == "bedrock"
    if use_cache:
        first_system = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)
        if first_system is not None:
            m = messages[first_system]
            if isinstance(m.get("content"), str) and m["content"] and CACHE_POINT not in m["content"]:
                messages = list(messages)
                messages[first_system] = {**m, "content": m["content"] + CACHE_POINT}
    if not any(isinstance(m.get("content"), str) and CACHE_POINT in m["content"] for m in messages):
        return messages
    # Points beyond the limit are dropped from the front: the later ones
    # cover the longer, more valuable prefixes.
    total = sum(m["content"].count(CACHE_POINT) for m in messages if isinstance(m.get("content"), str))
    skip = max(0, total - _MAX_CACHE_POINTS)
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or CACHE_POINT not in content:
            out.append(m)
            continue
        pieces = content.split(CACHE_POINT)
        if not use_cache:
            out.append({**m, "content": "".join(pieces)})
            continue
        blocks: list[dict] = []
        for i, piece in enumerate(pieces):
            if piece:
                blocks.append({"type": "text", "text": piece})
            if i < len(pieces) - 1:
                if skip:
                    skip -= 1
                elif blocks and "cachePoint" not in blocks[-1]:
                    blocks.append({"cachePoint": {"type": "default"}})
        out.append({**m, "content": blocks or ""})
    return out


def _usage_record(usage: dict) -> dict:
    """Token counts for the call log. input_tokens includes cached tokens."""
    details = usage.get("input_token_details") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cache_read_tokens": int(details.get("cache_read") or 0),
        "cache_write_tokens": int(details.get("cache_creation") or 0),
    }


def _log_call(record: dict) -> None:
    _call_log.info(
        "token_usage task=%s provider=%s model=%s input=%d output=%d cache_read=%d cache_write=%d "
        "latency_ms=%d ok=%s",
        record["task"], record["provider"], record["model"],
        record["input_tokens"], record["output_tokens"],
        record.get("cache_read_tokens", 0), record.get("cache_write_tokens", 0),
        record["latency_ms"], record["ok"],
    )
    if not LLM_LOG_PATH:
        return
    try:
        with open(LLM_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # never let telemetry break a request
        log.debug("llm call log write failed: %s", exc)


def _collect(task: str, record: dict) -> None:
    collector = _rag_collector_var.get()
    if collector is not None:
        collector.append({
            "task": task,
            "input_tokens": record["input_tokens"],
            "output_tokens": record["output_tokens"],
            "cache_read_tokens": record.get("cache_read_tokens", 0),
            "latency_ms": record["latency_ms"],
        })


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
    if model is None and LLM_PROVIDER == "bedrock" and tier in ("main", "reasoning"):
        model = _model_override_var.get()
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
        ai_message = runnable.invoke(_prepare_messages(messages))
    except Exception as exc:
        record["latency_ms"] = int((time.monotonic() - t0) * 1000)
        record["error"] = str(exc)[:300]
        _log_call(record)
        raise

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = getattr(ai_message, "usage_metadata", None) or {}
    record.update(**_usage_record(usage), latency_ms=latency_ms, ok=True)
    _log_call(record)
    _collect(task, record)

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


# ── structured output ──────────────────────────────────────────────────────
#
# Nova on the Converse API has no JSON mode, and asked for "JSON only" it
# sometimes prefixes a sentence, truncates the object or invents a value. A
# single tool, forced with tool_choice, makes it return arguments that the
# Converse API has already parsed (Nova Lite, Nova 2 Lite and Nova Pro all
# honour a forced tool; Nova 2 Lite with reasoningConfig on does not, so it is
# never combined with this). The arguments are then checked against the same
# schema, and one bad reply gets one repair attempt that says what was wrong.

def _conform(value: Any, schema: dict, path: str) -> tuple[Any, list[str]]:
    """`value` coerced to `schema` where that is unambiguous, plus what is still wrong.

    Coercions are only the ones a model makes by accident: a list or object
    sent as a JSON string, "true"/"false" as text, a number as text, an enum
    value in the wrong case. maxItems trims instead of failing.
    """
    kind = schema.get("type")
    if kind in ("object", "array") and isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
    if kind == "object":
        if not isinstance(value, dict):
            return value, [f"{path} must be an object"]
        out, errors = dict(value), []
        for key in schema.get("required", []):
            if out.get(key) is None:
                errors.append(f"{path}.{key} is required")
        for key, sub in (schema.get("properties") or {}).items():
            if out.get(key) is not None:
                out[key], errs = _conform(out[key], sub, f"{path}.{key}")
                errors += errs
        return out, errors
    if kind == "array":
        if not isinstance(value, list):
            return value, [f"{path} must be a list"]
        items, errors, out = schema.get("items"), [], []
        for i, item in enumerate(value):
            if items:
                item, errs = _conform(item, items, f"{path}[{i}]")
                errors += errs
            out.append(item)
        if "maxItems" in schema:
            out = out[: schema["maxItems"]]
        if len(out) < schema.get("minItems", 0):
            errors.append(f"{path} needs at least {schema['minItems']} items")
        return out, errors
    if kind == "boolean":
        if isinstance(value, bool):
            return value, []
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", []
        return value, [f"{path} must be true or false"]
    if kind in ("integer", "number"):
        if isinstance(value, str):
            try:
                value = float(value.replace(",", ""))
            except ValueError:
                return value, [f"{path} must be a number"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value, [f"{path} must be a number"]
        return (int(value) if kind == "integer" else value), []
    if kind == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            return value, [f"{path} must be text"]
        if "enum" in schema:
            canon = {str(e).casefold(): e for e in schema["enum"]}
            hit = canon.get(value.strip().casefold())
            if hit is None:
                return value, [f"{path} must be one of {'|'.join(map(str, schema['enum']))}"]
            return hit, []
        return value, []
    return value, []


def _parse_text_json(text: str) -> Any:
    from gis.reasoning import parse_json   # lazy: gis imports this module
    return parse_json(text or "")


def _log_json(task: str, via: str, attempts: int, errors: list[str]) -> None:
    """One JSONL line per structured call (kind=json_outcome), next to the
    per-call token records, so the share of tool / repair / text / fail can be
    counted from llm_calls.jsonl."""
    _call_log.info("json_outcome task=%s via=%s attempts=%d", task, via, attempts)
    if not LLM_LOG_PATH:
        return
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": "json_outcome",
              "task": task, "provider": LLM_PROVIDER, "json_via": via,
              "attempts": attempts, "errors": errors[:5]}
    try:
        with open(LLM_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("json outcome log write failed: %s", exc)


def chat_json_ex(
    messages: list[dict],
    *,
    name: str,
    schema: dict,
    description: str = "",
    tier: str = "routing",
    temperature: float = 0.0,
    max_tokens: int = 300,
    task: str = "json",
) -> tuple[dict | None, str]:
    """chat_json(), also returning how the object was obtained:
    "tool" | "text" | "repair" | "fail"."""
    from tool_schemas import make_tool, to_openai_tools

    use_tools = LLM_PROVIDER == "bedrock"
    tools = to_openai_tools([make_tool(name, description or name, schema.get("properties"),
                                       schema.get("required"))]) if use_tools else None
    # The text path (Groq, or a reply with no tool call) needs the shape in
    # words; Groq's JSON mode also requires the word JSON in the prompt.
    shape = (f"Reply with one JSON object only, no other text, matching this JSON schema: "
             f"{json.dumps(schema, ensure_ascii=False)}")
    msgs = list(messages) if use_tools else [*messages, {"role": "system", "content": shape}]

    errors: list[str] = []
    real_call = False
    for attempt in (1, 2):
        try:
            result = chat(msgs, tier=tier, temperature=temperature, max_tokens=max_tokens,
                          json_mode=not use_tools, tools=tools,
                          tool_choice=name if use_tools else None, task=task)
        except Exception as exc:
            # Nova sometimes breaks off a tool call ("Model produced invalid
            # sequence as part of ToolUse"): the repair asks for the same object
            # as plain JSON text instead. Any other failure (throttling,
            # network) is not something a repair can fix.
            if attempt == 1 and use_tools and "ToolUse" in str(exc):
                log.info("[json] %s: the tool call broke off, asking for text instead", task)
                use_tools, tools = False, None
                msgs = [*messages, {"role": "system", "content": shape}]
                errors = ["the tool call broke off"]
                real_call = True
                continue
            log.warning("[json] %s call failed: %s", task, exc)
            if real_call or "ToolUse" in str(exc):
                _log_json(task, "fail", attempt, [str(exc)[:200]])
            return None, "fail"
        real_call = real_call or isinstance(result, LLMResult)
        calls = getattr(getattr(result, "message", None), "tool_calls", None) or []
        args = next((c.get("args") for c in calls if c.get("name") == name), None)
        via = "tool" if args is not None else "text"
        raw = args if args is not None else _parse_text_json(getattr(result, "text", ""))
        data, errors = _conform(raw, schema, "$")
        if raw is None:
            errors = ["the reply held no JSON object"]
        if not errors:
            via = "repair" if attempt == 2 else via
            if real_call:
                _log_json(task, via, attempt, [])
            return data, via
        if attempt == 2:
            break
        shown = json.dumps(raw, ensure_ascii=False, default=str) if raw is not None \
            else (getattr(result, "text", "") or "")
        fix = (f"Your previous reply could not be used: {'; '.join(errors)}. "
               f"It was: {shown[:400]}. ")
        fix += (f"Call {name} again with valid arguments." if use_tools else shape)
        msgs = [*msgs, {"role": "user", "content": fix}]
    log.info("[json] %s gave no valid object after a repair: %s", task, "; ".join(errors)[:200])
    if real_call:
        _log_json(task, "fail", 2, errors)
    return None, "fail"


def chat_json(messages: list[dict], **kwargs) -> dict | None:
    """Ask for one JSON object matching `schema`; None when none could be had.

    Under Bedrock the object comes back as the arguments of a single forced
    tool named `name`; elsewhere (Groq) as JSON-mode text. Either way it is
    checked against `schema` (types, required keys, enums), and a reply that
    fails gets one repair attempt. Callers keep their own safe default for None.
    """
    return chat_json_ex(messages, **kwargs)[0]


def chat_stream(
    messages: list[dict],
    *,
    on_delta,
    model: str | None = None,
    tier: str = "main",
    temperature: float = 0.0,
    max_tokens: int = 800,
    task: str = "general",
) -> LLMResult:
    """chat(), but the text is handed to `on_delta(str)` piece by piece as the
    model writes it. Returns the same LLMResult once the answer is complete.

    If the provider refuses to stream (some accounts lack the streaming
    permission) the call falls back to a normal one, as long as nothing has
    been shown yet, so the caller always gets an answer.
    """
    if model is None and LLM_PROVIDER == "bedrock" and tier in ("main", "reasoning"):
        model = _model_override_var.get()
    chat_model = get_chat_model(model=model, tier=tier, temperature=temperature,
                                max_tokens=max_tokens)
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
        "stream": True,
    }
    t0 = time.monotonic()
    parts: list[str] = []
    merged = None
    try:
        for chunk in chat_model.stream(_prepare_messages(messages)):
            piece = _flatten_content(chunk.content)
            if piece:
                parts.append(piece)
                on_delta(piece)
            merged = chunk if merged is None else merged + chunk
    except Exception as exc:
        if parts:
            record["latency_ms"] = int((time.monotonic() - t0) * 1000)
            record["error"] = str(exc)[:300]
            _log_call(record)
            raise
        log.warning("streaming failed before any text (%s), retrying without it", exc)
        result = chat(messages, model=model, tier=tier, temperature=temperature,
                      max_tokens=max_tokens, task=task)
        if result.text:
            on_delta(result.text)
        return result

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = getattr(merged, "usage_metadata", None) or {}
    record.update(**_usage_record(usage), latency_ms=latency_ms, ok=True)
    _log_call(record)
    _collect(task, record)
    return LLMResult(
        text="".join(parts).strip(),
        provider=LLM_PROVIDER,
        model_id=model_id,
        input_tokens=record["input_tokens"],
        output_tokens=record["output_tokens"],
        total_tokens=record["total_tokens"],
        latency_ms=latency_ms,
        message=merged,
    )
