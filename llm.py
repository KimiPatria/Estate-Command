"""
LLM entry points for the pipeline.

NAMING NOTE: the "providers" in this module (groq / gpt120b / gpt20b) are the
user-facing MODEL TOGGLE exposed in the UI — all of them are served by Groq
today. The infrastructure provider (Groq vs. Amazon Bedrock) is a separate
concept: config.LLM_PROVIDER, resolved inside llm_client.py. When
LLM_PROVIDER="bedrock", the toggle is inert and every call goes to
BEDROCK_MODEL_ID / BEDROCK_ROUTING_MODEL_ID instead.

All calls run through llm_client.chat() (LangChain BaseChatModel underneath),
which normalizes messages/usage across providers and writes the structured
per-call log.
"""

import json
import logging
from threading import Lock

import llm_client
from config import (
    GROQ_API_KEY,
    GROQ_MODEL,
    GROQ_MODEL_120B,
    GROQ_MODEL_20B,
    DEFAULT_PROVIDER,
    LLM_PROVIDER,
    BEDROCK_MODEL_ID,
)
from prompts import load_prompt

log = logging.getLogger(__name__)

VALID_PROVIDERS = {"groq", "gpt120b", "gpt20b"}

PROVIDER_LABELS = {
    "groq":    "GPT OSS 120B" if "gpt-oss-120b" in GROQ_MODEL else GROQ_MODEL,
    "gpt120b": "GPT OSS 120B",
    "gpt20b":  "GPT OSS 20B",
    "bedrock": BEDROCK_MODEL_ID,
}

_PROVIDER_MODELS = {
    "groq":    GROQ_MODEL,
    "gpt120b": GROQ_MODEL_120B,
    "gpt20b":  GROQ_MODEL_20B,
}

_state_lock = Lock()


def _initial_provider() -> str:
    p = DEFAULT_PROVIDER if DEFAULT_PROVIDER in VALID_PROVIDERS else "groq"
    if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is required but not set.")
    return p


_active_provider: str = _initial_provider()


def get_provider() -> str:
    return _active_provider


def list_providers() -> dict[str, dict]:
    # The toggle only selects among Groq-hosted models; under bedrock it is inert.
    available = LLM_PROVIDER == "groq" and bool(GROQ_API_KEY)
    return {
        key: {
            "label": PROVIDER_LABELS[key],
            "model": _PROVIDER_MODELS[key],
            "available": available,
        }
        for key in ("groq", "gpt120b", "gpt20b")
    }


def set_provider(name: str) -> tuple[bool, str | None]:
    name = (name or "").lower().strip()
    if name not in VALID_PROVIDERS:
        return False, f"Unknown provider '{name}'. Use one of: {', '.join(sorted(VALID_PROVIDERS))}."
    if LLM_PROVIDER != "groq":
        return False, (
            f"Model toggle applies to the Groq provider only; "
            f"LLM_PROVIDER={LLM_PROVIDER} uses {BEDROCK_MODEL_ID}."
        )
    if not GROQ_API_KEY:
        return False, "Groq API key not configured"
    global _active_provider
    with _state_lock:
        _active_provider = name
    return True, None


def call_llm(
    messages: list[dict],
    *,
    temperature: float = 0.0,
    max_tokens: int = 800,
    provider: str | None = None,
    task: str = "general",
) -> tuple[str, str, dict]:
    """Route a chat-style messages list to the active (or specified) model.

    Returns (text, provider_used, usage_dict).
    """
    p = (provider or _active_provider).lower()
    if p not in VALID_PROVIDERS:
        raise ValueError(f"Unknown provider: {p}")
    if LLM_PROVIDER == "bedrock":
        result = llm_client.chat(
            messages, tier="main",
            temperature=temperature, max_tokens=max_tokens, task=task,
        )
        return result.text, "bedrock", result.usage
    result = llm_client.chat(
        messages, model=_PROVIDER_MODELS[p],
        temperature=temperature, max_tokens=max_tokens, task=task,
    )
    return result.text, p, result.usage


def call_normalization_llm(sql: str, ddl_context: str) -> dict:
    """Normalize a SQL query into a date-parameterised template.

    Uses the routing tier (faster/cheaper than the main LLM).
    Returns a dict with keys: template_sql, has_granularity,
    inferred_date_column, date_injection_needed.
    Raises RuntimeError on failure.
    """
    system = load_prompt("sql_normalizer_system")
    user_content = f"SQL:\n{sql}\n\nSchema context:\n{ddl_context}" if ddl_context else f"SQL:\n{sql}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_content},
    ]

    try:
        result = llm_client.chat(
            messages, tier="routing",
            temperature=0, max_tokens=600, json_mode=True, task="sql_normalize",
        )
    except Exception as exc:
        raise RuntimeError(f"Normalization LLM call failed: {exc}") from exc

    try:
        data = json.loads(result.text)
    except Exception as exc:
        raise RuntimeError(f"Normalization LLM returned non-JSON: {result.text[:200]}") from exc

    required = {"template_sql", "has_granularity", "inferred_date_column", "date_injection_needed"}
    missing = required - set(data.keys())
    if missing:
        raise RuntimeError(f"Normalization LLM response missing fields: {missing}")

    return data


def call_routing_llm(messages: list[dict]) -> list[str]:
    """Call the routing tier and parse a JSON {"tables": [...]} response.

    Always uses the cheap routing model (independent of the user-facing toggle).
    Returns [] on any failure — caller is expected to fall back to a heuristic.
    """
    try:
        result = llm_client.chat(
            messages, tier="routing",
            temperature=0, max_tokens=400, json_mode=True, task="table_routing",
        )
    except Exception as e:
        log.warning("Routing LLM call failed: %s", e)
        return []

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        log.warning("Routing LLM returned non-JSON: %r", result.text[:200])
        return []

    tables = data.get("tables") if isinstance(data, dict) else None
    if not isinstance(tables, list):
        log.warning("Routing LLM JSON missing 'tables' list: %r", data)
        return []
    return [str(t).strip() for t in tables if isinstance(t, (str,)) and t.strip()]
