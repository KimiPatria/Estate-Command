"""The chat reads the forecast (buildplan_chat_centre.md Part B).

A forecast question is answered from the same payload the forecast page draws
(forecast_router.page_payload): nothing is fitted and no query runs, so the chat
and the page always agree, manual picks, the K3 fallback, stale flags and
unavailable estates included. The source follows the chat's database: Live
estates -> live, EPMS staging -> staging.

The model is handed a FACTS block and may only quote it. Every number in its
reply is checked against the facts (gis.reasoning.audit_figures); one retry names
the strays, and a reply built from the facts without a model is the last resort.

Wording follows /forecast (the outlook page): "expected", "likely range", a plain
model name only when asked, and tonnes and budget "not available yet".
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Callable, Optional

from gis.reasoning import audit_figures
from prompts import load_prompt

log = logging.getLogger("epms-chat")

ROUTE = "forecast"
LIVE, STAGING = "live", "staging"
GROUP = "all"
MAX_STEPS = 6            # Chronos-2 serves steps 1-6 on both sources
DEFAULT_MONTHS = 3
HISTORY_MONTHS = 12      # actual months drawn before the forecast, as on the outlook page


# ── what a forecast question looks like ────────────────────────────────────

_FORECAST_WORDS = re.compile(
    r"\b(?:forecasts?|forecasted|forecasting|outlooks?|projections?|projected|predict(?:ions?|ed|s)?"
    r"|ramalan|perkiraan|proyeksi|prediksi|diperkirakan|diprediksi|diproyeksikan|diramalkan)\b",
    re.IGNORECASE)
_FUTURE = re.compile(
    r"\b(?:next|coming|upcoming|following)\s+(?:\d+\s+|few\s+|couple\s+of\s+)?(?:months?|quarters?|year|half)"
    r"|\b(?:bulan|kuartal|tahun|minggu)\s+(?:depan|mendatang|berikutnya|ke\s*depan)"
    r"|\b\d+\s+bulan\s+(?:ke\s*depan|mendatang|berikutnya)"
    r"|\b(?:expected|expect|will\s+(?:we|production|harvest|output|it)|going\s+to|akan)\b",
    re.IGNORECASE)
_PRODUCTION = re.compile(
    r"\b(?:production|produce|harvest\w*|output|yield|bunches|tonnes|tonnage|ffb|tbs"
    r"|panen|produksi|tandan|hasil|buah)\b", re.IGNORECASE)
# After a forecast answer, these keep the conversation on the forecast.
_FOLLOWUP = re.compile(
    r"\b(?:how\s+(?:sure|confident|accurate|certain|reliable|good)|range|band|uncertain\w*|confidence"
    r"|accuracy|accurate|reliable|last\s+year|year\s+before|a\s+year\s+ago|which\s+model|what\s+model|model"
    r"|why|chart|graph|plot|other\s+estates?|each\s+estate|every\s+estate|all\s+estates|per\s+estate"
    r"|compare|comparison|versus|vs|lower|higher|drop|rise|up|down"
    r"|seberapa|yakin|akurat|rentang|tahun\s+lalu|grafik|kenapa|mengapa|bandingkan|kebun\s+lain"
    r"|turun|naik|semua\s+kebun|tiap\s+kebun|setiap\s+kebun)\b", re.IGNORECASE)
# A question about what already happened goes back to the database.
_PAST = re.compile(
    r"\b(?:last|past|previous)\s+(?:\d+\s+)?(?:days?|weeks?|months?)|\byesterday\b|\bkemarin\b"
    r"|\b(?:minggu|bulan)\s+(?:lalu|kemarin)\b|\bytd\b|\bso\s+far\b", re.IGNORECASE)
_WHY = re.compile(r"\b(?:why|kenapa|mengapa|lower|higher|drop|turun|naik)\b", re.IGNORECASE)
# "How does the forecast work?", "how is it made?": the meaning, not a figure.
_HOW_WORKS = re.compile(
    r"\bhow\s+(?:does|do|is|are|was|were)\b.{0,40}\b(?:work\w*|made|built|calculated|computed|generated"
    r"|produced|trained|done)\b|\b(?:bagaimana\s+(?:cara|kerja)|cara\s+kerja|apa\s+itu)\b", re.IGNORECASE)
_EVERY = re.compile(
    r"\b(?:all|every|each|per|by|across|other)\s+(?:the\s+)?(?:three\s+|3\s+)?estates?\b|\ball\s+three\b"
    r"|\bcompare\s+(?:the\s+)?estates\b|\b(?:semua|seluruh|setiap|tiap|per)\s+kebun\b|\bkebun\s+lain\b",
    re.IGNORECASE)
_ASKS_SURE = re.compile(r"\b(?:how\s+(?:sure|confident|certain|reliable)|range|band|seberapa\s+yakin|rentang)\b",
                        re.IGNORECASE)
_ASKS_ACCURACY = re.compile(r"\b(?:accura\w+|track\s+record|right|akurat)\b", re.IGNORECASE)
_ASKS_LAST_YEAR = re.compile(r"\b(?:last\s+year|year\s+before|a\s+year\s+ago|tahun\s+lalu|yoy)\b", re.IGNORECASE)
_INDONESIAN = re.compile(
    r"\b(?:apa|apakah|berapa|bagaimana|mengapa|kenapa|bulan|tahun|untuk|dengan|dari|dan|kebun|depan"
    r"|ramalan|perkiraan|proyeksi|prediksi|produksi|panen|tandan|seberapa|yakin|tampilkan|bandingkan)\b",
    re.IGNORECASE)


def wants_forecast(message: str) -> bool:
    """The message is about the production forecast: no model call needed to tell."""
    m = message or ""
    if _FORECAST_WORDS.search(m):
        return True
    return bool(_FUTURE.search(m) and _PRODUCTION.search(m))


def asks_how_it_works(message: str) -> bool:
    """A forecast question that wants the meaning, not a figure: the explain route."""
    from chat_intent import asks_meaning
    m = message or ""
    return bool(asks_meaning(m) or _HOW_WORKS.search(m)) and not _WHY.search(m)


def follows_up(message: str, history: list[dict]) -> bool:
    """After a forecast answer, a short or pointed follow-up stays on the forecast.
    `history` is pipeline_turns(). A question about what already happened does not."""
    if not history or history[-1].get("route") != ROUTE:
        return False
    m = message or ""
    if _PAST.search(m) and not _FOLLOWUP.search(m):
        return False
    from chat_conversation import _follows_up
    return bool(_follows_up(m) or _FOLLOWUP.search(m) or mentioned_estates(m))


# ── estates ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _alias_patterns() -> dict[str, re.Pattern]:
    """Forecast estate id -> pattern of the words that name it, over both sources.
    BA is on the live roster without a model; naming it gets an honest reply."""
    ids: list[str] = []
    try:
        import forecast_sources as fs
        for sid in (fs.LIVE, fs.STAGING):
            for eid in fs.SOURCE_ESTATES[sid]:
                if eid not in ids:
                    ids.append(eid)
    except Exception:                       # the forecast stack is missing: a fixed list
        ids = ["k3", "ec", "eb", "ea"]
    if "ba" not in ids:
        ids.append("ba")
    out = {}
    for eid in ids:
        code = eid.upper()
        names = [code, f"estate {code}", f"kebun {code}"]
        if len(code) == 2 and code[0] == "E":
            names += [f"estate {code[1]}", f"kebun {code[1]}"]
        if code == "K3":
            names.append("kebun 3")
        out[eid] = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE)
    return out


def mentioned_estates(message: str) -> list[str]:
    return [eid for eid, pat in _alias_patterns().items() if pat.search(message or "")]


def source_of(db_id: str) -> str:
    return STAGING if (db_id or "").lower() == STAGING else LIVE


def _roster(source: str) -> list[str]:
    """Estate ids the source's forecast page lists, modelled or not."""
    try:
        import forecast_sources as fs
        ids = list(fs.SOURCE_ESTATES[source])
    except Exception:
        ids = ["k3", "ec", "eb", "ea"] if source == LIVE else ["ea", "eb", "ec"]
    if source == LIVE and "ba" not in ids:
        ids.append("ba")
    return ids


def scope(message: str, history: list[dict], source: str) -> dict:
    """Which estates to read: named in the message, kept from the last forecast
    turn, or the page's default (the staging group; K3 on live, which is what
    the live group view shows)."""
    roster = _roster(source)
    named = mentioned_estates(message)
    out = {"source": source, "estates": [], "named": [], "every": False, "default": False,
           "elsewhere": [e for e in named if e not in roster]}
    in_roster = [e for e in named if e in roster]
    last = history[-1] if history else None
    kept = (last or {}).get("meta", {}).get("forecast") or {}
    if _EVERY.search(message or "") and not in_roster:
        out["every"] = True
        out["estates"] = roster if source == LIVE else [GROUP, *roster]
    elif in_roster:
        out["named"] = in_roster
        out["estates"] = in_roster
    elif last and last.get("route") == ROUTE and kept.get("estates") and kept.get("source") == source:
        out["estates"] = list(kept["estates"])
        out["every"] = bool(kept.get("every"))
    else:
        out["default"] = True
        out["estates"] = [GROUP] if source == STAGING else ["k3"]
    return out


# ── months ─────────────────────────────────────────────────────────────────

_MONTHS = {
    "january": 1, "jan": 1, "januari": 1, "february": 2, "feb": 2, "februari": 2, "march": 3, "mar": 3,
    "maret": 3, "april": 4, "apr": 4, "may": 5, "mei": 5, "june": 6, "jun": 6, "juni": 6, "july": 7,
    "jul": 7, "juli": 7, "august": 8, "aug": 8, "agustus": 8, "agu": 8, "september": 9, "sep": 9,
    "sept": 9, "october": 10, "oct": 10, "oktober": 10, "okt": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12, "desember": 12, "des": 12,
}
_MONTH_RE = re.compile(
    r"(?:\b(in|for|by|until|till|through|to|bulan|sampai|hingga|pada)\s+)?\b("
    + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b(?:\s+(20\d{2}))?", re.IGNORECASE)
_N_MONTHS = re.compile(r"\b(\d{1,2})\s*(?:months?|bulan)\b", re.IGNORECASE)
_QUARTER = re.compile(r"\b(?:quarter|kuartal|triwulan|3\s*bulan)\b", re.IGNORECASE)
_HALF = re.compile(r"\b(?:6\s*months?|half\s+(?:a\s+)?year|semester|setengah\s+tahun|6\s*bulan)\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:12\s*months?|a\s+year|one\s+year|next\s+year|tahun\s+depan|setahun|12\s*bulan)\b",
                   re.IGNORECASE)


def requested(message: str, history: list[dict]) -> dict:
    """{"months": n, "month": (m, year|None) | None}: how far ahead the question looks."""
    m = message or ""
    months: Optional[int] = None
    hit = _N_MONTHS.search(m)
    if hit:
        months = int(hit.group(1))
    elif _YEAR.search(m):
        months = 12
    elif _HALF.search(m):
        months = 6
    elif _QUARTER.search(m):
        months = 3
    named = None
    for prep, name, year in _MONTH_RE.findall(m):
        if name.lower() == "may" and not (prep or year):
            continue                       # "may I", "it may drop"
        named = (_MONTHS[name.lower()], int(year) if year else None)
        break
    if months is None:
        last = history[-1] if history else None
        kept = (last or {}).get("meta", {}).get("forecast") or {}
        months = int(kept.get("months") or DEFAULT_MONTHS) if last and last.get("route") == ROUTE else DEFAULT_MONTHS
    return {"months": max(1, months), "month": named}


def _add_months(ym: str, n: int) -> str:
    y, mo = int(ym[:4]), int(ym[5:7])
    mo += n
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return f"{y:04d}-{mo:02d}"


def _steps_to(anchor: str, month: tuple[int, Optional[int]]) -> int:
    """Months from the last actual month to a named month (next occurrence when no year)."""
    ay, am = int(anchor[:4]), int(anchor[5:7])
    tm, ty = month
    if ty is None:
        ty = ay if tm > am else ay + 1
    return (ty - ay) * 12 + (tm - am)


# ── reading ────────────────────────────────────────────────────────────────

def _read(estate: str, horizon: int, source: str) -> dict:
    import forecast_router as fr
    return fr.page_payload(estate, horizon=horizon, source=source)


def _label(estate: str, source: str) -> str:
    if estate == GROUP:
        return "All estates" if source == STAGING else "K3"
    return estate.upper()


def read_all(estates: list[str], months: int, source: str) -> list[dict]:
    """One entry per estate: {"id", "label", "payload" | None, "reason"}."""
    horizon = MAX_STEPS if months > DEFAULT_MONTHS else DEFAULT_MONTHS
    out = []
    for eid in estates:
        entry = {"id": eid, "label": _label(eid, source), "payload": None, "reason": ""}
        try:
            entry["payload"] = _read(eid, horizon, source)
            entry["label"] = entry["payload"].get("series_label") or entry["label"]
        except Exception as exc:              # forecast_sources.Unavailable, or a broken file
            entry["reason"] = str(exc) or f"{type(exc).__name__}"
            log.info("[chat] forecast unavailable for %s/%s: %s", source, eid, entry["reason"])
        out.append(entry)
    return out


# ── facts ──────────────────────────────────────────────────────────────────

def _n(v) -> str:
    return "n/a" if v is None else f"{float(v):,.0f}"


def _pct(v) -> str:
    return "n/a" if v is None else f"{float(v):+.1f}%"


def _lo(f: dict):
    return f.get("lower80") if f.get("lower80") is not None else f.get("lower")


def _hi(f: dict):
    return f.get("upper80") if f.get("upper80") is not None else f.get("upper")


def _model_line(p: dict) -> str:
    label = p.get("model_label") or p.get("model_name") or "the served model"
    tags = []
    if (p.get("serving") or {}).get("mode") == "manual":
        tags.append("a manual pick by the forecasting team")
    if p.get("fallback"):
        tags.append(f"the fallback model, because {p['fallback'].get('reason') or 'the usual one is unavailable'}")
    if p.get("stale"):
        tags.append(f"older than the data: {p.get('stale_note') or 'built before the latest month'}")
    return label + (f" ({'; '.join(tags)})" if tags else "")


def _accuracy_lines(p: dict) -> list[str]:
    out = []
    acc = p.get("accuracy_multih") or {}
    per = {h.get("step"): h for h in acc.get("per_horizon") or []}
    s1 = (per.get(1) or {}).get("smape", p.get("smape_1step"))
    head = p.get("headline_smape")
    if s1 is not None or head is not None:
        bits = []
        if s1 is not None:
            bits.append(f"one month ahead {s1:.1f}%")
        if head is not None and acc.get("max_step"):
            bits.append(f"over {acc['max_step']} months {head:.1f}%")
        out.append("Typical error of past forecasts on this estate: " + ", ".join(bits) + ".")
    tr = p.get("track_record") or {}
    if tr.get("n"):
        out.append(f"Of the last {tr['n']} one-month-ahead forecasts, {tr.get('within', 0)} of {tr['n']} "
                   f"landed within {tr.get('tol_pct', 10)}% of the actual.")
    months, hits = tr.get("months") or [], tr.get("hits") or []
    if months and len(months) == len(hits):
        recent = list(zip(months, hits))[-6:]
        out.append("Past one-month-ahead forecasts, within "
                   f"{tr.get('tol_pct', 10)}% of that month's actual? "
                   + ", ".join(f"{m}: {'yes' if h else 'no'}" for m, h in recent)
                   + ". The forecast figures made for past months are not available here.")
    outer = (p.get("interval") or {}).get("outer") or {}
    if outer.get("coverage") is not None and outer.get("n"):
        out.append(f"The likely range held {round(outer['coverage'] * 100)}% of past actuals "
                   f"({outer['n']} checked).")
    else:
        s = ((p.get("interval") or {}).get("per_step") or {}).get("1") or {}
        if s.get("coverage") is not None and s.get("coverage_n"):
            out.append(f"The likely range held {round(s['coverage'] * 100)}% of past one-month-ahead actuals "
                       f"({s['coverage_n']} checked).")
    return out


def facts(entries: list[dict], req: dict, sc: dict, db_label: str) -> dict:
    """The FACTS text the model quotes from, plus what the answer code needs."""
    source = sc["source"]
    months = min(req["months"], MAX_STEPS)
    notes: list[str] = []
    if req["months"] > MAX_STEPS:
        notes.append(f"The forecast reaches {MAX_STEPS} months ahead, not {req['months']}: "
                     f"the figures below cover the {MAX_STEPS} months it has.")
    lines = [f"DATABASE: {db_label}. Forecast unit: bunches per month, per estate. "
             f"Tonnes and budget: not available yet."]
    first_payload = next((e["payload"] for e in entries if e["payload"]), None)
    if first_payload and first_payload.get("data_through"):
        lines.append(f"DATA RUNS TO: {first_payload['data_through']}.")
    if sc.get("default"):
        notes.append("No estate was named." + (
            " The staging group figure is EA + EB + EC together; each estate is also available on its own."
            if source == STAGING else
            " The live group view shows K3; EC and EB have their own forecasts (ask for them by name)."))

    shown: list[dict] = []
    for e in entries:
        p = e["payload"]
        if not p:
            continue
        fc = list(p.get("forecast") or [])
        anchor = p.get("anchor") or {}
        hist = {h["month"]: h["value"] for h in p.get("history") or []}
        take = months
        if req["month"] and anchor.get("month"):
            steps = _steps_to(anchor["month"], req["month"])
            target = _add_months(anchor["month"], steps)
            if steps <= 0:
                have = hist.get(target)
                notes.append(f"{e['label']}: {target} is already in the actuals"
                             + (f": {_n(have)} bunches." if have is not None else ", but that month is not in the history."))
            elif steps > len(fc):
                notes.append(f"{e['label']}: {target} is beyond the forecast, which reaches {fc[-1]['month'] if fc else 'n/a'}.")
            else:
                take = max(take, steps)
        fc = fc[:take]
        if not fc:
            e["reason"] = e["reason"] or "no forecast rows"
            continue
        lines.append("")
        lines.append(f"ESTATE {e['label']}")
        lines.append(f"- Model: {_model_line(p)}.")
        if anchor.get("month"):
            lines.append(f"- Last actual month {anchor['month']}: {_n(anchor.get('value'))} bunches.")
        ly_vals = []
        for f in fc:
            ly_month = f"{int(f['month'][:4]) - 1}{f['month'][4:]}"
            ly = hist.get(ly_month)
            ly_vals.append(ly)
            lines.append(f"- {f['month']} (step {f['step']}): expected {_n(f['ensemble'])}; "
                         f"likely range {_n(_lo(f))} to {_n(_hi(f))}"
                         + (f"; last year {ly_month}: {_n(ly)}" if ly is not None else "; last year: not in the data")
                         + ".")
        total = sum(f["ensemble"] for f in fc)
        span = f"{fc[0]['month']} to {fc[-1]['month']}" if len(fc) > 1 else fc[0]["month"]
        line = f"- Total for the {len(fc)} month{'s' if len(fc) > 1 else ''} ({span}): {_n(total)} bunches"
        if all(v is not None for v in ly_vals) and ly_vals:
            ly_total = sum(ly_vals)
            line += f"; same months last year: {_n(ly_total)}"
            if ly_total:
                line += f"; change vs last year: {_pct((total - ly_total) / ly_total * 100)}"
        lines.append(line + ".")
        for a in _accuracy_lines(p):
            lines.append(f"- {a}")
        shown.append({"id": e["id"], "label": e["label"], "months": [f["month"] for f in fc],
                      "total": total, "payload": p, "forecast": fc})

    missing = [e for e in entries if e["id"] not in {s["id"] for s in shown}]
    if missing:
        lines.append("")
        lines.append("UNAVAILABLE (no forecast)")
        for e in missing:
            lines.append(f"- {e['label']}: {e['reason'] or 'no forecast'}")
    for eid in sc.get("elsewhere") or []:
        other = "Live estates" if source == STAGING else "EPMS staging"
        lines.append(f"- {eid.upper()}: not in this database's forecast; it is on the {other} database.")
    if notes:
        lines.append("")
        lines.append("NOTES")
        lines.extend(f"- {n}" for n in notes)
    return {"text": "\n".join(lines), "shown": shown, "missing": missing, "notes": notes, "months": months}


# ── the chart ──────────────────────────────────────────────────────────────

def chart(shown: list[dict]) -> Optional[dict]:
    """One estate: actuals, the forecast from the last actual month, the likely range.
    Several: the total for the months asked, one bar per estate (last year beside it)."""
    if not shown:
        return None
    if len(shown) == 1:
        s = shown[0]
        p, fc = s["payload"], s["forecast"]
        hist = (p.get("history") or [])[-HISTORY_MONTHS:]
        labels = [h["month"] for h in hist] + [f["month"] for f in fc]
        n, m = len(hist), len(fc)
        actual = [h["value"] for h in hist] + [None] * m
        joined = [None] * n + [f["ensemble"] for f in fc]
        low = [None] * n + [_lo(f) for f in fc]
        high = [None] * n + [_hi(f) for f in fc]
        if n:
            joined[n - 1] = low[n - 1] = high[n - 1] = hist[-1]["value"]   # the fan starts at the anchor
        return {"type": "forecast", "title": f"{s['label']} · bunches per month", "x_kind": "date",
                "x_label": "Month", "y_label": "Bunches", "labels": labels, "truncated": False,
                "series": [{"name": "Actual", "values": actual}, {"name": "Forecast", "values": joined}],
                "band": {"name": "Likely range", "low": low, "high": high}}
    labels = [s["label"] for s in shown]
    months = shown[0]["months"]
    span = f"{months[0]} to {months[-1]}" if len(months) > 1 else months[0]
    series = [{"name": f"Expected ({span})", "values": [s["total"] for s in shown]}]
    ly = []
    for s in shown:
        hist = {h["month"]: h["value"] for h in s["payload"].get("history") or []}
        vals = [hist.get(f"{int(mo[:4]) - 1}{mo[4:]}") for mo in s["months"]]
        ly.append(sum(vals) if vals and all(v is not None for v in vals) else None)
    if any(v is not None for v in ly):
        series.append({"name": "Same months last year", "values": ly})
    return {"type": "bar", "title": "Expected bunches by estate", "x_kind": "category",
            "x_label": "Estate", "y_label": "Bunches", "labels": labels, "series": series, "truncated": False}


# ── answering ──────────────────────────────────────────────────────────────

_SUGGEST_EN = ["How sure is it?", "Compare with last year", "What about the other estates?",
               "Show the next 6 months", "How accurate has it been?"]
_SUGGEST_ID = ["Seberapa yakin perkiraan ini?", "Bandingkan dengan tahun lalu", "Bagaimana dengan kebun lain?",
               "Tampilkan 6 bulan ke depan", "Seberapa akurat selama ini?"]


def language(message: str) -> str:
    return "Indonesian" if _INDONESIAN.search(message or "") else "English"


def suggestions(message: str, sc: dict, req: dict) -> list[str]:
    pool = _SUGGEST_ID if language(message) == "Indonesian" else _SUGGEST_EN
    out = []
    for i, text in enumerate(pool):
        if i == 0 and _ASKS_SURE.search(message):
            continue
        if i == 1 and _ASKS_LAST_YEAR.search(message):
            continue
        if i == 2 and (sc.get("every") or len(sc.get("estates") or []) > 1):
            continue
        if i == 3 and req["months"] >= MAX_STEPS:
            continue
        if i == 4 and _ASKS_ACCURACY.search(message):
            continue
        out.append(text)
    return out[:3]


def template_answer(fx: dict, sc: dict, lang: str) -> str:
    """The answer without a model: every figure straight from the facts."""
    ind = lang == "Indonesian"
    parts = []
    for s in fx["shown"]:
        fc = s["forecast"]
        span = f"{fc[0]['month']} – {fc[-1]['month']}" if len(fc) > 1 else fc[0]["month"]
        head = (f"**{s['label']}**: {'perkiraan' if ind else 'expected'} **{_n(s['total'])} "
                f"{'tandan' if ind else 'bunches'}** ({span}).")
        rows = [f"- {f['month']}: {_n(f['ensemble'])} ({'kisaran' if ind else 'likely'} {_n(_lo(f))} – {_n(_hi(f))})"
                for f in fc]
        parts.append(head + "\n" + "\n".join(rows))
    for e in fx["missing"]:
        parts.append(f"**{e['label']}**: {'belum ada perkiraan' if ind else 'no forecast yet'} ({e['reason']}).")
    for n in fx["notes"]:
        parts.append(n)
    return "\n\n".join(parts) or ("Belum ada perkiraan untuk permintaan ini." if ind else
                                  "No forecast is available for this request.")


def link(sc: dict, fx: dict) -> str:
    shown = fx["shown"]
    est = shown[0]["id"] if len(shown) == 1 else (GROUP if sc["source"] == STAGING else (shown[0]["id"] if shown else "k3"))
    return f"[Open in Forecast](/forecast?db={sc['source']}&estate={est})"


def messages(message: str, history: list[dict], fx: dict, lang: str) -> list[dict]:
    from chat_intent import _route_line, transcript
    chat = transcript(history, _route_line) or "(nothing yet)\n"
    return [
        {"role": "system", "content": load_prompt("chat_forecast_system")},
        {"role": "user", "content": (f"CONVERSATION SO FAR:\n{chat}\nFACTS:\n{fx['text']}\n\n"
                                     f"QUESTION: {message}\nREPLY LANGUAGE: {lang}")},
    ]


def respond(message: str, history: list[dict], db, ask: Callable, ask_quiet: Optional[Callable] = None) -> dict:
    """Answer a forecast question. `ask(messages, max_tokens=, task=, tier=)` returns
    (text, provider, usage); `ask_quiet` is used for the retry (no streaming)."""
    import chat_events
    from chat_charts import wants_chart

    sc = scope(message, history, source_of(db.id))
    req = requested(message, history)
    lang = language(message)
    chat_events.step("read", "Reading the forecast")
    entries = read_all(sc["estates"], req["months"], sc["source"])
    fx = facts(entries, req, sc, db.label)
    chat_events.done("read", ", ".join(s["label"] for s in fx["shown"]) or "nothing available")
    meta = {"source": sc["source"], "estates": [e["id"] for e in entries], "every": sc["every"],
            "months": fx["months"], "shown": [s["id"] for s in fx["shown"]]}
    out = {"scope": meta, "chart": chart(fx["shown"]), "suggestions": suggestions(message, sc, req),
           "labels": [s["label"] for s in fx["shown"]], "provider": None, "provider_label": None,
           "facts": fx["text"], "link": link(sc, fx) if fx["shown"] else "", "audit": None}

    if not fx["shown"]:
        out["answer"] = template_answer(fx, sc, lang)
        return out
    last_was_forecast = bool(history) and history[-1].get("route") == ROUTE
    if last_was_forecast and wants_chart(message) and len((message or "").split()) <= 6:
        out["answer"] = ("Ini grafik perkiraannya." if lang == "Indonesian" else "Here is the forecast chart.")
        return out

    chat_events.step("answer", "Writing the answer")
    msgs = messages(message, history, fx, lang)
    text, provider, usage = "", None, {}
    try:
        text, provider, usage = ask(msgs, max_tokens=600, task="chat_forecast", tier="main")
        audit = audit_figures(text, fx["text"])
        if not audit["clean"]:
            log.info("[chat] forecast answer quoted figures not in the facts: %s", audit["unverified"])
            retry = msgs + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "These figures are not in FACTS: "
                 + ", ".join(audit["unverified"][:8])
                 + ". Rewrite the reply quoting only figures that appear in FACTS, written exactly as there."},
            ]
            text, provider, usage = (ask_quiet or ask)(retry, max_tokens=600, task="chat_forecast_retry", tier="main")
            audit = audit_figures(text, fx["text"])
            if not audit["clean"]:
                log.warning("[chat] forecast answer still off the facts (%s): template used", audit["unverified"])
                text = ""
        out["audit"] = audit
    except Exception as exc:
        log.warning("[chat] forecast answer failed: %s", exc)
        text = ""
    chat_events.done("answer")
    out["provider"], out["provider_label"] = provider, (usage or {}).get("model") or provider
    out["answer"] = text.strip() or template_answer(fx, sc, lang)
    return out


# ── for the explain route ──────────────────────────────────────────────────

EXPLAIN_NOTES = (
    "The production forecast (the Forecast page) gives the expected bunches per month for each "
    "estate, 3 to 6 months ahead. It is made by Chronos-2, a pretrained time-series model used as "
    "it comes (not trained on this company's data), from the estate's own monthly history. Each "
    "month comes with a likely range taken from how far the model's past forecasts for that estate "
    "missed, so the range is wider where the model has been less right. Accuracy is reported as the "
    "typical percentage error of past forecasts, and as how many of the last one-month-ahead "
    "forecasts landed within 10% of the actual. The forecast is rebuilt monthly. It uses no weather, "
    "budget or work data, except K3, whose model also sees weather. Tonnes and budget comparisons "
    "are not available yet. On the EPMS staging database the forecast covers EA, EB and EC and "
    "their total; on the live estates K3, EC and EB (EA has too little history, BA no model). The "
    "chat answers forecast questions from the same figures the Forecast page shows."
)
