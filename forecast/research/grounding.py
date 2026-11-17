"""Nova 2 Lite Web Grounding: one web-searched question in, text plus citations out.

`ChatBedrockConverse` does not pass a `systemTool` through, so this calls boto3
`converse` directly. Grounding works in US regions only, needs
`bedrock:InvokeTool` on `amazon.nova_grounding`, and is billed on top of
inference, so every call is logged to llm_calls.jsonl with `grounded_queries`
for the cost check (buildplan_chat_centre.md C.6 #4).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config import AWS_REGION, LLM_LOG_PATH

log = logging.getLogger(__name__)

MODEL_ID = "us.amazon.nova-2-lite-v1:0"
TOOL_CONFIG = {"tools": [{"systemTool": {"name": "nova_grounding"}}]}

_client = None


def _bedrock():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        region = AWS_REGION if AWS_REGION.startswith("us-") else "us-east-1"
        # Sign with the IAM credentials (SigV4), not AWS_BEARER_TOKEN_BEDROCK: the
        # Bedrock API-key identity has no bedrock:InvokeTool, the IAM user does.
        _client = boto3.client(
            "bedrock-runtime", region_name=region,
            config=Config(signature_version="v4", read_timeout=300,
                          retries={"max_attempts": 2, "mode": "standard"}),
        )
    return _client


@dataclass
class Grounded:
    text: str                      # answer text with citation markers stripped
    citations: list[dict]          # [{"url", "domain"}], in order, deduped
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    raw: dict | None = None        # the converse response (for debugging / trial)


def _parse(response: dict) -> tuple[str, list[dict]]:
    parts: list[str] = []
    cites: list[dict] = []
    seen: set[str] = set()
    for block in response.get("output", {}).get("message", {}).get("content", []):
        if "text" in block:
            parts.append(block["text"])
        elif "citationsContent" in block:
            for c in block["citationsContent"].get("citations", []):
                web = (c.get("location") or {}).get("web") or {}
                url = web.get("url")
                if url and url not in seen:
                    seen.add(url)
                    cites.append({"url": url, "domain": web.get("domain", "")})
    text = _THINKING.sub("", "".join(parts))
    return text.strip(), cites


# Nova 2 Lite sometimes emits its reasoning inline; it is never part of the answer.
_THINKING = re.compile(r"<thinking>.*?(</thinking>|$)", re.S | re.I)


def _log(task: str, usage: dict, latency_ms: int, ok: bool, n_cites: int, error: str = "") -> None:
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "task": task, "provider": "bedrock", "model": MODEL_ID, "rag_source": None,
        "input_tokens": int(usage.get("inputTokens") or 0),
        "output_tokens": int(usage.get("outputTokens") or 0),
        "total_tokens": int(usage.get("totalTokens") or 0),
        "latency_ms": latency_ms, "ok": ok,
        "grounded_queries": 1, "citations": n_cites,
    }
    if error:
        record["error"] = error[:300]
    if not LLM_LOG_PATH:
        return
    try:
        with open(LLM_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # telemetry never breaks a run
        log.debug("grounding log write failed: %s", exc)


def ask(question: str, *, system: str | None = None, task: str = "research_grounding",
        max_tokens: int = 1200, keep_raw: bool = False) -> Grounded:
    """Run one grounded question. Raises the boto3 error on failure (after logging it)."""
    kwargs: dict[str, Any] = {
        "modelId": MODEL_ID,
        "messages": [{"role": "user", "content": [{"text": question}]}],
        "toolConfig": TOOL_CONFIG,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    t0 = time.perf_counter()
    try:
        response = _bedrock().converse(**kwargs)
    except Exception as exc:
        _log(task, {}, int((time.perf_counter() - t0) * 1000), False, 0, f"{type(exc).__name__}: {exc}")
        raise
    latency = int((time.perf_counter() - t0) * 1000)
    text, cites = _parse(response)
    usage = response.get("usage", {})
    _log(task, usage, latency, True, len(cites))
    return Grounded(text=text, citations=cites, usage=usage, latency_ms=latency,
                    raw=response if keep_raw else None)
