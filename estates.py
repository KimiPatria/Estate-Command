"""
Multi-estate scope detection for the Head Office SQL RAG pipeline.

Both estate databases (config.ESTATES) share an identical schema, so table
retrieval and SQL generation in chat_router.py stay single-shot — the LLM
writes one SQL string that's valid against every estate. Only execution fans
out per estate (see chat_router._execute_multi).

Chat and the Dashboard resolve scope purely from free text (word-boundary,
case-insensitive alias match) — no UI selector, since both take a message/
goal with nowhere else to put an estate choice. The Report page has fixed-SQL
presets and saved templates with no free text to parse, so it has an explicit
estate dropdown instead; resolve_scope() below is the shared entry point that
lets an explicit choice (the dropdown) take priority over text detection when
both are available (e.g. Report's /generate). All paths default to every
estate in ESTATE_ORDER unless something narrows the scope.
"""

import re

from config import ESTATES, ESTATE_ORDER

_ALIAS_PATTERNS = {
    eid: re.compile(
        r"\b(" + "|".join(re.escape(a) for a in cfg["aliases"]) + r")\b",
        re.IGNORECASE,
    )
    for eid, cfg in ESTATES.items()
}


def detect_estate_mentions(message: str) -> list[str]:
    """Estate ids explicitly named in the message (order = ESTATE_ORDER).
    Empty if none are named — distinct from detect_estate_scope's fallback,
    since callers also need to know *whether* an estate was named (e.g. to
    warn the SQL writer off treating the name as a data value) even when
    every estate happens to be named explicitly."""
    return [eid for eid in ESTATE_ORDER if _ALIAS_PATTERNS[eid].search(message)]


def detect_estate_scope(message: str) -> list[str]:
    """Estate ids to query for this message. Defaults to every estate in
    ESTATE_ORDER unless one or more are named explicitly in the text."""
    return detect_estate_mentions(message) or list(ESTATE_ORDER)


def strip_estate_mentions(message: str, mentioned_estates: list[str]) -> str:
    """Remove estate name/alias tokens for the given estates from the text
    before it reaches SQL generation.

    Telling the model in an instruction not to filter on the estate name
    wasn't reliable in testing — short codes like "BA"/"K3" look enough like
    real per-row estate/company codes already in the schema (e.g. "K1", "K2")
    that the model filtered on them anyway. Removing the literal token is a
    more robust fix: table retrieval and the final answer still see the
    original message, only the SQL-writer's question text is stripped.
    """
    stripped = message
    for eid in mentioned_estates:
        stripped = _ALIAS_PATTERNS[eid].sub("", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def resolve_scope(explicit: str | None, text: str | None = None) -> list[str]:
    """Resolve estate ids from an explicit choice (e.g. a dropdown value) and/
    or free text, with the explicit choice winning when it names one estate.

    - explicit is a single estate id (e.g. "ba") or "all"/None/"" for no
      preference.
    - text, if given, is scanned with detect_estate_mentions() when explicit
      doesn't already pick a single estate.
    - Falls back to every estate in ESTATE_ORDER when neither narrows it.
    """
    if explicit and explicit != "all" and explicit in ESTATES:
        return [explicit]
    if text:
        mentioned = detect_estate_mentions(text)
        if mentioned:
            return mentioned
    return list(ESTATE_ORDER)
