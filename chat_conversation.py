"""The conversation around a chat answer: asking back, suggesting next, keeping scope.

Everything here sits around the SQL pipeline rather than inside it, and every
model call is optional: a failure means no question back or no buttons, never
a lost answer. What the model writes is checked before it reaches the page, so
a question or a button cannot carry a figure nobody retrieved.
"""

import logging
import re

import data_sources
import llm_client
from config import ESTATES, ESTATE_ORDER
from estates import detect_estate_mentions
from gis.reasoning import audit_figures, parse_json
from prompts import load_prompt

log = logging.getLogger("epms-chat")

MAX_OPTIONS = 4
MAX_SUGGESTIONS = 3
MAX_TOPIC_TURNS = 4    # how far a run of follow-ups is walked back for its topic
ROUTE_CLARIFY = "clarify"
ROUTE_CHAT = "chat"       # greeting / thanks / "what can you do?": no query runs

# The whole message is a pleasantry or a question about the assistant itself.
# Anchored at both ends so "hi, total harvest last month?" still runs a query.
_SMALLTALK = re.compile(
    r"^\s*(?:"
    r"(?:hi|hello|hey|halo|hai|hallo|yo)(?:\s+(?:there|all|semua|bot))?"
    r"|good\s+(?:morning|afternoon|evening|day)"
    r"|selamat\s+(?:pagi|siang|sore|malam)"
    r"|thanks?(?:\s+(?:a\s+lot|so\s+much|very\s+much|you(?:\s+(?:so|very)\s+much)?))?"
    r"|thx|ty|terima\s*kasih(?:\s+banyak)?|makasih(?:\s+ya)?"
    r"|ok(?:ay)?|oke|great|nice|cool|perfect|got\s+it|mantap|sip|baik"
    r"|who\s+are\s+you|siapa\s+(?:kamu|anda)"
    r"|what\s+can\s+you\s+do|what\s+can\s+i\s+ask(?:\s+you)?|what\s+do\s+you\s+do"
    r"|what\s+(?:kind\s+of\s+)?data\s+(?:do\s+you\s+have|is\s+(?:here|available))"
    r"|how\s+(?:do|can)\s+(?:you|i)\s+(?:work|use\s+(?:this|you))"
    r"|help(?:\s+me)?|bantuan|tolong"
    r"|apa\s+(?:saja\s+)?yang\s+bisa\s+(?:kamu|anda)\s+(?:lakukan|bantu|jawab)"
    r"|(?:kamu|anda)\s+bisa\s+apa(?:\s+saja)?|bisa\s+(?:bantu\s+)?apa(?:\s+saja)?"
    r")\s*[!.?,]*\s*$",
    re.IGNORECASE)

# A chart request that points back at what is already on screen.
_POINTS_BACK = re.compile(
    r"\b(?:that|this|it|those|these|them|above|same|result|results|data"
    r"|itu|ini|tersebut|tadi|hasilnya|datanya|hasil)\b", re.IGNORECASE)


def is_smalltalk(message: str) -> bool:
    return bool(_SMALLTALK.match(message or ""))


def chart_followup(message: str, history: list[dict]) -> bool:
    """"Plot that", "as a pie chart": draw the rows already fetched, no new query.

    Only when nothing in the message names plantation data: "chart the harvest
    per month" is a new question and goes through the pipeline.
    """
    from chat_charts import wants_chart
    from error_handler import _DOMAIN_TERMS

    if not wants_chart(message) or not any(t.get("rows") for t in history):
        return False
    if _DOMAIN_TERMS.search(message):
        return False
    return bool(_POINTS_BACK.search(message)) or len(message.split()) <= 6

# Only these can leave a question unanswerable as written: a word pointing at
# something, or a judgement with no measure. Anything else skips the model, so
# a complete question costs nothing extra and cannot be over-asked.
_MAY_NEED_ASKING = re.compile(
    r"\b(?:that|this|it|its|them|they|those|these|ones?|itu|ini|tersebut|mereka"
    r"|best|worst|top|good|bad|better|worse|perform\w*|productiv\w*|efficien\w*"
    r"|terbaik|terburuk|bagus|buruk|kinerja|performa|produktiv\w*|produktif|efisien\w*)\b",
    re.IGNORECASE)

# Told to the gate outright: asked to match the message's language, Nova Pro
# answered English questions in Indonesian.
_INDONESIAN = re.compile(
    r"\b(?:apa|apakah|siapa|mana|yang|berapa|bagaimana|mengapa|kenapa|itu|ini|tersebut"
    r"|kinerja|terbaik|terburuk|bulan|tahun|dengan|dari|dan|untuk|pemanen|panen"
    r"|tandan|tampilkan|bandingkan|kebun|divisi|paling)\b",
    re.IGNORECASE)
_OPTION_INSTRUCTION = re.compile(r"^(?:please|silakan|tolong|mohon)\b", re.IGNORECASE)
_LISTED_OPTIONS = re.compile(r"\s*\b(?:options? include|pilihan\w*)\b.*$",
                             re.IGNORECASE | re.DOTALL)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")

# Shapes of a follow-up. "This" and "ini" are left out on purpose: "this month"
# and "bulan ini" open plenty of brand-new questions.
_CONTINUES = re.compile(
    r"^\s*(?:(?:what|how) about|and|then|also|same|compared?|versus|vs"
    r"|bagaimana (?:dengan|kalau|jika)|kalau|lalu|terus|dan|sama|bandingkan)\b"
    r"|\b(?:that|those|these|them|they|it|its|same|itu|tersebut|mereka)\b",
    re.IGNORECASE)
_EVERY_ESTATE = re.compile(r"\b(?:all|every|each|semua|seluruh|setiap|tiap)\b", re.IGNORECASE)

# The estate named is set against the rest, or the filter is to come off: the
# question needs every estate even though it names one. "Estate C against other
# estates" was scoped to Estate C alone and could only ever return Estate C.
_WIDENS = re.compile(
    r"\b(?:other|remaining)\s+(?:estates?|kebun|plantations?|plasmas?)\b"
    r"|\bthe\s+others\b|\bothers\b(?=\s*[?.!,]|\s*$)|\bthe\s+rest\b(?!\s+of\b)"
    r"|\b(?:kebun|estate)\s+(?:yang\s+)?lain(?:nya)?\b|\byang\s+lain(?:nya)?\b|\blainnya\b"
    r"|\bwithout\s+(?:the\s+|any\s+)?(?:estate\s+)?filter\w*|\bunfiltered\b|\bno\s+(?:estate\s+)?filter\b"
    r"|\bremov\w*\s+(?:the\s+)?(?:estate\s+)?filter\b|\btanpa\s+filter\b",
    re.IGNORECASE)

# A bare go-ahead to what the last answer offered: "run it", "yes please".
_CONFIRM = re.compile(
    r"^\s*(?:(?:yes|yeah|yep|yup|sure|please|ok(?:ay)?|ya|iya|boleh|oke)[\s,!.]*)*"
    r"(?:(?:please\s+)?(?:run|do|try|show)\s+(?:it|that|this|so|the\s+(?:query|same))(?:\s+again)?"
    r"|go\s+ahead|do\s+it|go\s+for\s+it|yes|yeah|yep|yup|sure|please|ya|iya|boleh"
    r"|jalankan(?:\s+(?:saja|aja))?|lanjut(?:kan)?|silakan|coba)"
    r"(?:\s+please)?[\s!.]*$",
    re.IGNORECASE)
# The sentence of an answer that proposes something to do next.
_OFFER = re.compile(
    r"\b(?:I\s+can|I\s+could|we\s+can|we\s+could|we\s+(?:would\s+|'d\s+)?need\s+to|you\s+(?:can|could|may)"
    r"|would\s+you\s+like|shall\s+I|should\s+I|want\s+me\s+to|if\s+you(?:'d|\s+would)?\s+like"
    r"|to\s+compare\b|run(?:ning)?\s+(?:it|the\s+query|this)|without\s+filter\w*)", re.IGNORECASE)


# ── asking back ────────────────────────────────────────────────────────────

def parse_clarification(raw) -> dict | None:
    """The gate's question, tidied. None when there is nothing to ask."""
    if not isinstance(raw, dict) or raw.get("clarify") is not True:
        return None
    # Underscores because the model offers column names as choices.
    question = str(raw.get("question") or "").replace("_", " ").strip()[:240]
    if not question:
        return None
    options: list[str] = []
    choices = raw.get("options")
    for opt in choices if isinstance(choices, list) else []:
        text = str(opt).replace("_", " ").strip()[:60]
        # A button sends its own text as the user's reply, so "Please name the
        # block" is not a choice anyone can make.
        if _OPTION_INSTRUCTION.match(text):
            continue
        if text and text.casefold() not in {o.casefold() for o in options}:
            options.append(text)
    options = options[:MAX_OPTIONS] if len(options) >= 2 else []
    if options:
        # Nova writes the choices into the sentence as well as the list.
        question = _LISTED_OPTIONS.sub("", question).strip() or question
    return {"question": question, "options": options}


def clarify_gate(message: str, turns: list[dict]) -> dict | None:
    """Should we ask before querying? Returns {question, options} or None.

    Never twice running: someone who has just answered a question gets an
    answer. The reasoning tier because the cheap one could not do this
    reliably on the Estate Command copilot (5 of 15 against 15 of 15).
    """
    if (turns and turns[-1]["route"] == ROUTE_CLARIFY) or not _MAY_NEED_ASKING.search(message):
        return None
    chat = "\n".join(
        f"Q: {t['question'][:200]}\nA: {t['answer'][:240]}" for t in turns[-3:]
    ) or "(nothing yet: this is the first message)"
    src = data_sources.active()
    estates = ", ".join(src.entity_labels())
    language = "Indonesian" if _INDONESIAN.search(message) else "English"
    try:
        result = llm_client.chat(
            [{"role": "system", "content": load_prompt("chat_clarify_system")},
             {"role": "user", "content": (f"ESTATES: {estates}\n\nCHAT SO FAR:\n{chat}\n\n"
                                          f"MESSAGE: {message}\n\nREPLY LANGUAGE: {language}")}],
            tier="reasoning", temperature=0, max_tokens=160, json_mode=True,
            task="chat_clarify",
        )
    except Exception as exc:
        log.warning("[chat] clarify gate failed: %s", exc)
        return None
    asked = parse_clarification(parse_json(result.text or ""))
    if not asked:
        return None
    # Years are dropped first: "2024 or 2025?" states no measurement.
    text = "\n".join([asked["question"], *asked["options"]])
    evidence = [message, [[t["question"], t["answer"]] for t in turns]]
    if not audit_figures(_YEAR.sub(" ", text), evidence)["clean"]:
        log.info("[chat] clarification held back, it carried a figure: %s", asked["question"])
        return None
    return asked


def resolve_reply(message: str, turns: list[dict]) -> str:
    """Fold a reply to our question into the question it answers.

    The pipeline needs a standalone question: "total ripe bunches" means
    nothing to the SQL writer. A long message or a new question is taken as
    the user moving on, and goes through as typed. The wording matters: the
    same reply in parentheses sent table retrieval to the wrong tables and
    failed 2 of 3 runs, where this ran clean on 9 of 9.
    """
    if not turns or turns[-1]["route"] != ROUTE_CLARIFY:
        return message
    last = turns[-1]
    options = {o.casefold() for o in last["meta"].get("options", [])}
    if message.casefold() in options or ("?" not in message and len(message.split()) <= 8):
        return f"{last['question']} Specifically: {message}"
    return message


def is_confirmation(message: str) -> bool:
    return bool(_CONFIRM.match(message or ""))


def resolve_confirmation(message: str, history: list[dict]) -> str:
    """Turn "run it" into the question it agrees to.

    "Run it" holds nothing to query: on its own it inherited the last turn's
    estate and ran the very query the answer had just said was too narrow. It is
    read as the previous question plus what the answer offered, so "...without
    filtering by estate" reaches the scope rules. `history` is pipeline_turns().
    """
    if not history or not is_confirmation(message):
        return message
    last = history[-1]
    sentences = re.split(r"(?<=[.!?])\s+", (last.get("answer") or "").strip())
    offers = [s for s in sentences if _OFFER.search(s)]
    question = last["question"].rstrip(" ?.")
    return f"{question}. {offers[-1].rstrip()}" if offers else question


_MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july"
           "|august|september|october|november|december|januari|februari|maret|mei|juni|juli|agustus"
           "|oktober|desember")
_NAMES_PERIOD = re.compile(
    r"\b(?:19|20)\d{2}\b|\b(?:" + _MONTHS + r")\b"
    r"|\b(?:today|yesterday|tonight|week\w*|month\w*|year\w*|quarter\w*|q[1-4]|days?|daily|annual\w*"
    r"|ytd|mtd|since|between|until|season\w*|period|date[sd]?|latest|recent\w*|current\w*|now"
    r"|hari\w*|minggu\w*|bulan\w*|tahun\w*|kuartal|kemarin|semester|terbaru|terakhir|sekarang|periode)\b"
    r"|\d{1,2}[/-]\d{1,2}",
    re.IGNORECASE)


def names_period(text: str) -> bool:
    """Does the question say which period it is about?"""
    return bool(_NAMES_PERIOD.search(text or ""))


def widens_scope(message: str) -> bool:
    """The question needs every estate although it may name one."""
    return bool(_WIDENS.search(message or ""))


def pipeline_turns(turns: list[dict]) -> list[dict]:
    """History as the pipeline should see it: real exchanges, whole questions.

    Clarifying turns hold no data and would use up the short window of turns
    the SQL writer gets. A reply is shown as typed but read as what it meant.
    """
    return [{**t, "question": t["meta"].get("resolved") or t["question"]}
            for t in turns if t["route"] not in (ROUTE_CLARIFY, ROUTE_CHAT)]


# ── keeping scope ──────────────────────────────────────────────────────────

def _follows_up(message: str) -> bool:
    return bool(_CONTINUES.search(message)) or len(message.split()) <= 3


def carried_estates(message: str, history: list[dict]) -> list[str]:
    """Estate ids a follow-up keeps from the turn before, or [] for no change.

    Estates are chosen per message from the text, so "what about last month?"
    after a question about BA would otherwise run on every estate. Only an
    evident follow-up inherits, so a new question stays on the full default.

    On the staging database the estates are WERKS codes rather than engines
    (EA, EB, EC, P1, P2); the same rule applies to them.
    """
    src = data_sources.active()
    if widens_scope(message):
        return []
    if src.entities:
        if not history or src.detect_entities(message) or _EVERY_ESTATE.search(message):
            return []
        scope = [c for c in history[-1]["meta"].get("estates", []) if c in src.entities]
        return scope if scope and _follows_up(message) else []
    if not history or detect_estate_mentions(message) or _EVERY_ESTATE.search(message):
        return []
    scope = [e for e in history[-1]["meta"].get("estates", []) if e in ESTATES]
    if not scope or len(scope) >= len(ESTATE_ORDER):
        return []
    return scope if _follows_up(message) else []


def retrieval_context(message: str, history: list[dict]) -> str:
    """Text to find tables with: the message plus the topic it continues.

    "and March?" after "what about February?" holds no plantation word at all,
    so a run of follow-ups is walked back to the question that set the topic.
    Anything else keeps the single previous question.
    """
    if not history:
        return message
    if not _follows_up(message):
        return f"{history[-1]['question']}\n{message}"
    chain: list[str] = []
    for turn in reversed(history[-MAX_TOPIC_TURNS:]):
        chain.insert(0, turn["question"])
        if not _follows_up(turn["question"]):
            break
    return "\n".join([*chain, message])


def scope_after(labels: list[str], history: list[dict], ran_query: bool = True) -> list[str]:
    """Estate ids this turn leaves in play. A turn that ran no query keeps them.

    On staging an all-estates question names none, and that is a real answer:
    it clears the scope, where a turn that never reached the database keeps it.
    """
    src = data_sources.active()
    if src.entities:
        ids = [c for c, e in src.entities.items() if e["label"] in labels]
        if ids or ran_query:
            return ids
        return list(history[-1]["meta"].get("estates", [])) if history else []
    ids = [e for e in ESTATE_ORDER if ESTATES[e]["label"] in labels]
    if ids:
        return ids
    return list(history[-1]["meta"].get("estates", [])) if history else []


# ── suggesting next ────────────────────────────────────────────────────────

def clean_suggestions(raw, question: str, answer: str,
                      asked: list[str] | None = None) -> list[str]:
    """Keep only the suggestions safe to put on a button."""
    seen = {q.strip().casefold() for q in [question, *(asked or [])]}
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        text = re.sub(r"\s+", " ", item).strip()
        if not 8 <= len(text) <= 100 or text.casefold() in seen or "_" in text:
            continue
        if not audit_figures(text, [question, answer])["clean"]:
            continue
        seen.add(text.casefold())
        out.append(text)
    return out[:MAX_SUGGESTIONS]


def suggest_followups(question: str, answer: str, columns: list[str],
                      tables: list[str], turns: list[dict]) -> list[str]:
    """Next questions for the buttons under an answer, or [] on any failure."""
    try:
        result = llm_client.chat(
            [{"role": "system", "content": load_prompt("chat_followups_system")},
             {"role": "user", "content": (f"TABLES USED: {', '.join(tables) or 'none'}\n"
                                          f"COLUMNS RETURNED: {', '.join(columns) or 'none'}\n\n"
                                          f"QUESTION: {question}\n\nANSWER: {answer[:2000]}")}],
            tier="routing", temperature=0.3, max_tokens=220, json_mode=True,
            task="chat_followups",
        )
    except Exception as exc:
        log.warning("[chat] follow-up suggestions failed: %s", exc)
        return []
    parsed = parse_json(result.text or "") or {}
    return clean_suggestions(parsed.get("suggestions"), question, answer,
                             [t["question"] for t in turns])
