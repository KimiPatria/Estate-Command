"""Shared LLM plumbing for Estate Command.

Everything on this page that reasons in language goes through here, so the
provider seam stays where the rest of the app put it: business logic calls
llm_client, never a provider SDK. Today that resolves to Amazon Nova Pro
(main tier) and Nova Lite (routing tier) over Bedrock Converse.

Three things this module exists to guarantee:

  * JSON survives Nova. The Converse API has no JSON mode, so a model that
    wraps its object in a code fence or adds a sentence of preamble must not
    take an endpoint down. ``json_chat`` strips fences, falls back to the
    first balanced object in the text, and returns a typed failure instead of
    raising.

  * A dead model never takes the map with it. Every caller gets a payload
    with ``available: false`` and a reason, and the UI keeps rendering the
    deterministic layer underneath. Nothing here is load-bearing for the map.

  * Nothing is recomputed for free. TTLCache keeps the per-panel briefings
    off the critical path of a demo where the same block gets clicked twice.

The numeric guardrail lives in the prompts (prompts/estate_*.txt), not here,
because it is a phrasing problem: the model is handed figures and told it may
quote them and never derive new ones. Server-computed numbers are what the UI
renders; the model's text is commentary beside them.
"""

import json
import logging
import re
import time
from threading import Lock

log = logging.getLogger("estate-command.reasoning")

# Rough ceiling on any single serialised tool result or context block handed
# to the model. Nova Pro's window is far larger; the cap is about keeping the
# demo's latency and cost honest, not about fitting.
MAX_CONTEXT_CHARS = 14000


def model_info() -> dict:
    """Which provider and models are actually wired, for the UI badge."""
    try:
        import llm_client
        from config import LLM_PROVIDER

        return {
            "provider": LLM_PROVIDER,
            "main": llm_client.resolve_model(None, "main"),
            "fast": llm_client.resolve_model(None, "routing"),
        }
    except Exception as exc:  # pragma: no cover - config-level failure
        return {"provider": "unavailable", "error": str(exc)[:200]}


def unavailable(reason: str, **extra) -> dict:
    """The shape every AI endpoint returns when the model could not answer."""
    out = {"available": False, "reason": reason}
    out.update(extra)
    return out


def clip(value, limit: int = MAX_CONTEXT_CHARS) -> str:
    """Serialise anything to JSON, truncated to a sane prompt budget."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[...truncated, {len(text) - limit} chars omitted]"


# ── JSON out of a model with no JSON mode ──────────────────────────────────

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json(raw: str) -> dict | None:
    """Best-effort object out of model text. None when nothing parses."""
    if not raw:
        return None
    text = _FENCE.sub("", raw.strip())
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # Nova occasionally prefixes a sentence, or trails one. Take the first
    # balanced object rather than a greedy regex, which would swallow prose
    # between two objects.
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def json_chat(system: str, user: str, *, tier: str = "routing",
              max_tokens: int = 900, temperature: float = 0.2,
              task: str = "estate_command") -> tuple[dict | None, dict]:
    """One JSON-answering call. Returns (parsed_or_None, meta).

    meta always carries model / latency_ms / usage, and ``error`` or ``raw``
    when the call failed or the text did not parse, so a caller can surface
    the failure honestly instead of pretending the layer does not exist.
    """
    import llm_client

    t0 = time.monotonic()
    try:
        result = llm_client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            tier=tier, temperature=temperature, max_tokens=max_tokens,
            json_mode=True, task=task,
        )
    except Exception as exc:
        log.warning("[reasoning] %s failed: %s", task, exc)
        return None, {"error": str(exc)[:300],
                      "latency_ms": int((time.monotonic() - t0) * 1000)}

    meta = {
        "model": result.model_id,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "usage": result.usage,
    }
    parsed = parse_json(result.text)
    if parsed is None:
        meta["raw"] = (result.text or "")[:600]
        log.warning("[reasoning] %s returned unparseable JSON", task)
    return parsed, meta


def text_chat(system: str, user: str, *, tier: str = "routing",
              max_tokens: int = 700, temperature: float = 0.3,
              task: str = "estate_command") -> tuple[str | None, dict]:
    """One prose-answering call, for the places a schema would only get in the way."""
    import llm_client

    t0 = time.monotonic()
    try:
        result = llm_client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            tier=tier, temperature=temperature, max_tokens=max_tokens,
            task=task,
        )
    except Exception as exc:
        log.warning("[reasoning] %s failed: %s", task, exc)
        return None, {"error": str(exc)[:300],
                      "latency_ms": int((time.monotonic() - t0) * 1000)}
    return (result.text or "").strip(), {
        "model": result.model_id,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "usage": result.usage,
    }


# ── caching ────────────────────────────────────────────────────────────────

class TTLCache:
    """Small in-process cache. Same policy the forecast intelligence layer uses.

    Bounded by ``maxsize`` because the block briefing is keyed per block and
    291 blocks times a few months would otherwise sit in memory for the life
    of the process.
    """

    def __init__(self, ttl_seconds: int, maxsize: int = 256):
        self.ttl = ttl_seconds
        self.maxsize = maxsize
        self._slots: dict = {}
        self._lock = Lock()

    def get(self, key):
        now = time.time()
        with self._lock:
            slot = self._slots.get(key)
            if slot and (now - slot["ts"]) < self.ttl:
                return slot["value"]
            if slot:
                self._slots.pop(key, None)
        return None

    def put(self, key, value):
        with self._lock:
            if len(self._slots) >= self.maxsize:
                oldest = min(self._slots, key=lambda k: self._slots[k]["ts"])
                self._slots.pop(oldest, None)
            self._slots[key] = {"value": value, "ts": time.time()}

    def clear(self) -> int:
        with self._lock:
            n = len(self._slots)
            self._slots.clear()
        return n


# ── the numeric guardrail, checked rather than trusted ─────────────────────
#
# Every prompt in this system forbids deriving a new figure. Prompts are not
# enforcement. Nova Pro, asked what covering a shortfall would cost, will
# happily multiply tonnes by a per-kilo price and present the product as a
# fact - which is the single failure mode that would make this whole page
# untrustworthy in front of a client.
#
# So the answer is audited against its own evidence: every number in the text
# is looked for in the tool results that produced it. What is not found is
# reported as unverified, and the UI marks it. This catches invention and
# arithmetic alike, because a product of two real figures is itself not a
# figure anyone measured.
#
# It is deliberately blunt. A false positive costs a small warning chip; a
# false negative costs the client's trust.

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# A block label - 34-38 - is one token, not the numbers 34 and -38.
_LABEL = re.compile(r"\b\d{1,3}-\d{1,3}\b")

# "1. " / "12) " at the head of a line: an enumeration, not a measurement.
_LIST_MARKER = re.compile(r"^\s*\d{1,2}[.)]\s+")

# Numbers a sentence needs to be readable, that no tool would ever return.
_STOPWORDS = {0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
              100.0, 1000.0}


def _to_float(token: str):
    try:
        return abs(float(token.replace(",", "")))
    except ValueError:
        return None


def collect_numbers(obj, out: set | None = None) -> set:
    """Every number anywhere in a tool result, including inside its strings."""
    if out is None:
        out = set()
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(abs(float(obj)))
    elif isinstance(obj, str):
        for token in _NUMBER.findall(obj):
            value = _to_float(token)
            if value is not None:
                out.add(value)
    elif isinstance(obj, dict):
        for value in obj.values():
            collect_numbers(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            collect_numbers(value, out)
    return out


def _matches(value: float, known: set, tolerance: float) -> bool:
    if value in known or value in _STOPWORDS:
        return True
    for candidate in known:
        if candidate == 0:
            continue
        if abs(value - candidate) <= tolerance * candidate:
            return True
        # The model rounds when it writes prose: 0.6294 becomes 0.63, 2004.5
        # becomes 2005. Accept a rounding of a known figure at any precision
        # the text plausibly used.
        for digits in range(0, 4):
            if round(candidate, digits) == value:
                return True
    return False


def collect_labels(obj, out: set | None = None) -> set:
    """Every block label anywhere in a tool result, as whole tokens."""
    if out is None:
        out = set()
    if isinstance(obj, str):
        out.update(_LABEL.findall(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            collect_labels(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            collect_labels(value, out)
    return out


def audit_figures(answer: str, sources, tolerance: float = 0.005) -> dict:
    """Check every number in an answer against the evidence behind it.

    ``sources`` is anything JSON-ish: the tool trace, a grounding payload, or
    a list of both. Returns the unverified figures in the order they appear.

    Two kinds of token are handled before the arithmetic. Block labels read as
    "34-38" and would otherwise be scanned as the numbers 34 and -38, so they
    are matched whole against the labels in the evidence - which means a block
    the model invented is reported as the block it is, not as two orphan
    numbers. Ordered-list markers are stripped: a model answering "which
    blocks" with a numbered list is not claiming that 13 is a measurement, and
    flagging it turns a guardrail people trust into one they learn to ignore.
    """
    known = collect_numbers(sources)
    labels = collect_labels(sources)
    seen, unverified = [], []

    def check(token: str, value):
        if token in seen:
            return
        seen.append(token)
        if value is None or not _matches(value, known, tolerance):
            unverified.append(token)

    for line in (answer or "").split("\n"):
        line = _LIST_MARKER.sub("", line)
        for label in _LABEL.findall(line):
            if label in seen:
                continue
            seen.append(label)
            if label not in labels:
                unverified.append(label)
        line = _LABEL.sub(" ", line)
        for raw in _NUMBER.findall(line):
            # "short by 50, and" ends the match on a comma that belongs to the
            # sentence, not the number. Reporting "50," as an unverified figure
            # is a false positive that costs the reader's confidence in the
            # audit.
            token = raw.rstrip(",.")
            check(token, _to_float(token))

    return {
        "checked": len(seen),
        "unverified": unverified,
        "clean": not unverified,
        "note": ("Figures and block labels in the text that appear in no result "
                 "behind it. A total the model worked out itself lands here, "
                 "which is the point: nothing in this system is allowed to "
                 "compute."),
    }
