"""What a /chat message wants, decided before anything is queried.

Every message used to go to SQL unless it was exactly one known greeting, so
"hello, what can you do??" ran a query. Now one cheap routing-tier call sorts a
message into:

  data        a database query (the SQL pipeline, as before)
  context     answered from the conversation and the rows already fetched
  explain     what a term or measure means, how it is worked out, what the data
              covers: answered from the source's reference text, no query
  chat        greeting, thanks, "what can you do?": the small-talk reply
  off_domain  nothing to do with the plantation: a fixed reply
  forecast    the production forecast: answered from the forecast page's own
              figures (chat_forecast.py), no query

The call replaces two that ran before (the domain check on a first message and
the follow-up router on later ones), and is skipped whenever code can already
tell: a greeting, "chart that", "compare with the other estates", a reply to
our own question, or a first message that names plantation data and asks no
"what does X mean". A failed call means "data", which is what happened before.

This module also holds the conversation window shared by the prompts that
carry history. The window only grows between fixed cut points, so a prompt's
transcript is the previous request's transcript plus one turn, and Bedrock
reads the earlier part back from its prompt cache (llm_client.CACHE_POINT).
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Optional

import chat_forecast
import data_sources
import llm_client
from chat_conversation import _INDONESIAN, chart_followup, is_smalltalk, widens_scope
from config import CHAT_HISTORY_TEXT_TURNS
from estates import detect_estate_mentions
from prompts import load_prompt

log = logging.getLogger("epms-chat")

DATA, CONTEXT, EXPLAIN, CHAT, OFF_DOMAIN = "data", "context", "explain", "chat", "off_domain"
FORECAST = chat_forecast.ROUTE
ROUTES = (DATA, CONTEXT, EXPLAIN, CHAT, OFF_DOMAIN, FORECAST)

# ── the conversation window ────────────────────────────────────────────────
# The window starts at a multiple of WINDOW_STEP turns and moves only when it
# would hold more than WINDOW_SIZE + WINDOW_STEP - 1 turns: between moves every
# prompt repeats the one before it plus a turn, which the prompt cache reads
# back. With 4 and 4: turns 0-4, 0-5, 0-6, then 4-7, 4-8 ... (positions in the
# session, so clarifying and small-talk turns count but are not shown).
WINDOW_SIZE = CHAT_HISTORY_TEXT_TURNS
WINDOW_STEP = max(1, int(os.getenv("CHAT_HISTORY_WINDOW_STEP", "4")))
FETCH_TURNS = WINDOW_SIZE + WINDOW_STEP + 2      # enough raw turns to fill any window


def window(turns: list[dict]) -> list[dict]:
    """The turns a history-carrying prompt shows, from a fixed cut point."""
    if not turns:
        return []
    last = turns[-1].get("pos")
    if last is None:                       # turns not read from the store (tests)
        return turns[-WINDOW_SIZE:]
    count = last + 1
    start = ((count - WINDOW_SIZE) // WINDOW_STEP) * WINDOW_STEP if count > WINDOW_SIZE else 0
    return [t for t in turns if t.get("pos", 0) >= start]


def transcript(turns: list[dict], render: Callable[[dict], str]) -> str:
    """The window's turns rendered one by one, with a cache point after each of
    the last two: the first is where the previous request's prompt ended (read
    back from the cache), the second is where the next one will find this one.
    `render` must give the same text for the same turn every time."""
    shown = window(turns)
    out: list[str] = []
    for i, t in enumerate(shown):
        out.append(render(t).rstrip() + "\n")
        if i >= len(shown) - 2:
            out.append(llm_client.CACHE_POINT)
    return "".join(out)


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _route_line(t: dict) -> str:
    lines = [f"Q: {_truncate(t['question'], 200)}", f"A: {_truncate(t['answer'], 240)}"]
    if t.get("rows"):
        lines.append(f"[data fetched for this turn: columns {t['columns']}, "
                     f"{len(t['rows'])} of {t['row_count']} rows available]")
    return "\n".join(lines)


# ── deciding ───────────────────────────────────────────────────────────────

# A first message naming plantation data is a data question, unless it asks
# what something means or how it is worked out. Those go to the router, which
# may still say "data" ("explain the drop in harvest").
_EXPLAIN_CUE = re.compile(
    r"\b(?:mean|means|meaning|stand\s+for|stands\s+for|define[sd]?|definition|explain\w*"
    r"|how\s+(?:is|are|do\s+you|does\s+it)\s+.{0,40}\b(?:calculat\w*|comput\w*|defin\w*|measur\w*"
    r"|count(?:ed)?|work\w*\s+out)"
    r"|what\s+counts\s+as|difference\s+between"
    r"|apa\s+(?:itu|arti|artinya|maksud|maksudnya|bedanya)|artinya|maksudnya|definisi|jelaskan|pengertian"
    r"|cara\s+(?:menghitung|hitung)|dihitung|perbedaan)\b",
    re.IGNORECASE)
# "What is brondolan?" (a term) but not "what is the total harvest?" (a figure).
_WHAT_IS = re.compile(r"^\s*what\s+(?:is|are)\s+(?:a|an|the)?\s*[\w-]+(?:\s+[\w-]+)?\s*\?*\s*$", re.IGNORECASE)
_ASKS_FIGURE = re.compile(
    r"\b(?:total|sum|count|how\s+many|how\s+much|number\s+of|average|avg|top|most|least|highest|lowest"
    r"|best|worst|list|show|trend|per|by|berapa|jumlah|rata|tertinggi|terendah|tampilkan)\b"
    r"|\b(?:19|20)\d{2}\b", re.IGNORECASE)


def asks_meaning(message: str) -> bool:
    return bool(_EXPLAIN_CUE.search(message)) or (
        bool(_WHAT_IS.match(message)) and not _ASKS_FIGURE.search(message))

_INTENT_SCHEMA = {
    "type": "object",
    "required": ["route"],
    "properties": {"route": {
        "type": "string", "enum": list(ROUTES),
        "description": "data: needs a database query; context: answered from the conversation and "
                       "data already fetched; explain: a definition or how-it-works question needing "
                       "no figure; chat: greeting, thanks, about the assistant; off_domain: unrelated; "
                       "forecast: the production forecast or outlook for coming months"}},
}


def _names_data(message: str) -> bool:
    from error_handler import _DOMAIN_TERMS
    return bool(_DOMAIN_TERMS.search(message) or detect_estate_mentions(message)
                or data_sources.active().detect_entities(message))


def classify(message: str, history: list[dict], *, agreeing: bool = False,
             answering_us: bool = False) -> tuple[str, bool]:
    """(route, whether a model was asked). `history` is pipeline_turns()."""
    if answering_us or agreeing:
        return DATA, False                 # a reply to us, or "run it": the pipeline handles both
    if is_smalltalk(message):
        return CHAT, False
    # The forecast is read from its page, not queried: a forecast word decides it,
    # and a definition question about it goes to explain with the forecast notes.
    if chat_forecast.wants_forecast(message):
        return (EXPLAIN if chat_forecast.asks_how_it_works(message) else FORECAST), False
    if history and chart_followup(message, history):
        return CONTEXT, False
    if history and chat_forecast.follows_up(message, history):
        return FORECAST, False
    if widens_scope(message):
        return DATA, False                 # the rows on screen were scoped narrower than this asks
    if not history and _names_data(message) and not asks_meaning(message):
        return DATA, False

    chat = transcript(history, _route_line) or "(nothing yet: this is the first message)\n"
    decided = llm_client.chat_json(
        [{"role": "system", "content": load_prompt("chat_intent_system")},
         {"role": "user", "content": f"CONVERSATION SO FAR:\n{chat}\nNEW MESSAGE: {message}"}],
        name="route", schema=_INTENT_SCHEMA,
        description="Record which route serves the new message.",
        tier="routing", max_tokens=60, task="chat_intent",
    )
    if decided is None:
        log.warning("[chat] intent router gave no decision — treating it as a data question")
        return DATA, True
    route = decided["route"]
    if route == CONTEXT and not history:
        route = DATA
    log.info("[chat] intent=%s message=%r", route, message[:80])
    return route, True


# The decision travels to the SQL source's follow-up check (chat_router
# ._route_followup), so it is not asked a second time.
_preset: ContextVar[Optional[str]] = ContextVar("chat_intent_route", default=None)


@contextmanager
def preset(route: Optional[str]):
    token = _preset.set(route)
    try:
        yield
    finally:
        _preset.reset(token)


def preset_route() -> Optional[str]:
    """'context' | 'sql' when the intent router already decided, else None."""
    route = _preset.get()
    if route == CONTEXT:
        return "context"
    if route == DATA:
        return "sql"
    return None


# ── explaining ─────────────────────────────────────────────────────────────

def _measures_text(src: "data_sources.DataSource") -> str:
    lines = []
    for m in src.measures:
        q = re.sub(r"\{\w+\}", "", m.question).strip()
        lines.append(f"- {m.label}: {q}")
    return "\n".join(lines) or "(none listed)"


def explain_system(src: "data_sources.DataSource") -> str:
    """The explain prompt for a source: fixed text, so it is read from the cache."""
    import prompt_builder
    return load_prompt(
        "chat_explain_system", terms=load_prompt("plantation_terms"),
        database=src.label, description=src.description or src.label,
        topics=src.topics or "plantation operations", estates=", ".join(src.entity_labels()),
        measures=_measures_text(src), rules=prompt_builder._rules(src) or "(none recorded)")


def explain_messages(message: str, history: list[dict]) -> list[dict]:
    src = data_sources.active()
    chat = transcript(history, _route_line) or "(nothing yet)\n"
    language = "Indonesian" if _INDONESIAN.search(message) else "English"
    through = src.data_through()
    tail = f"The data runs to {through}.\n" if through else ""
    if chat_forecast.wants_forecast(message):
        tail += f"ABOUT THE FORECAST (use this for forecast questions):\n{chat_forecast.EXPLAIN_NOTES}\n"
    return [
        {"role": "system", "content": explain_system(src)},
        {"role": "user", "content": (f"CONVERSATION SO FAR:\n{chat}\n{tail}"
                                     f"MESSAGE: {message}\nREPLY LANGUAGE: {language}")},
    ]
