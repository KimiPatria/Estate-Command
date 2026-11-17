"""Broad questions ("how is Estate C performing?") answered with several measures.

No single figure answers "operational efficiency", and asking "which measure?"
every time is a dead end for an overview. Instead:

  1. plan()          Nova picks 2 to 4 measures from the data source's catalog
                     (data_sources.Measure) and a breakdown (estate, division,
                     block, month). The choice is an enum in a forced tool
                     call, so it cannot invent a measure; the code then checks
                     the breakdown against what each measure supports.
  2. sub_question()  Each measure becomes a plain question from its template,
                     written by code, so it reads the way the seed examples and
                     the lint rules expect. Every part gets the same period.
  3. (chat_router)   Each part goes through the normal write-check-run-correct
                     loop (generate_and_run), in parallel.
  4. merge()         The results are joined on the breakdown key, in code, into
                     one table: estate | measure A | measure B ... The figures
                     are the database's; nothing is computed across measures.

The trigger (chat_conversation.is_overview) is a deterministic rule, so a
complete question never reaches the planner.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

import data_sources
import llm_client
from chat_conversation import names_period
from prompts import load_prompt

log = logging.getLogger("epms-chat")

MAX_MEASURES = 4
GRAINS = ("estate", "division", "block", "month")


@dataclass
class Plan:
    measures: list            # data_sources.Measure, 2..MAX_MEASURES
    grain: str                # estate | division | block | month
    period: str               # appended to each sub-question: " in September 2026"
    period_label: str         # for the answer: "September 2026", "the last 12 full months"


def _schema(src: "data_sources.DataSource") -> dict:
    return {
        "type": "object",
        "required": ["measures", "grain"],
        "properties": {
            "measures": {"type": "array", "minItems": 2, "maxItems": MAX_MEASURES,
                         "items": {"type": "string", "enum": [m.id for m in src.measures]},
                         "description": "2 to 4 measure ids that together answer the question"},
            "grain": {"type": "string", "enum": list(GRAINS),
                      "description": "how each measure is broken down"},
            "period": {"type": "string",
                       "description": "the period the question names, in its words ('last month', "
                                      "'in 2025', 'September 2026'); empty when it names none"},
        },
    }


# ── the default period ─────────────────────────────────────────────────────

_latest: dict[str, tuple[float, Optional[int]]] = {}


def _last_full_year(src: "data_sources.DataSource") -> Optional[int]:
    """The latest calendar year every live estate database holds in full."""
    hit = _latest.get(src.id)
    if hit and time.monotonic() - hit[0] < 1800:
        return hit[1]
    from sqlalchemy import text
    ends = []
    for sid in src.scope_order:
        try:
            with src.scopes[sid]["engine"].connect() as conn:
                v = conn.execute(text("SELECT MAX(oph_created_date) FROM t_oph")).scalar()
            if v:
                ends.append(v if isinstance(v, date) else date.fromisoformat(str(v)[:10]))
        except Exception as exc:
            log.debug("[overview] latest date of %s unknown: %s", sid, exc)
    year = None
    if ends:
        end = min(ends)
        year = end.year if (end.month, end.day) == (12, 31) else end.year - 1
    _latest[src.id] = (time.monotonic(), year)
    return year


def default_period(src: "data_sources.DataSource") -> tuple[str, str]:
    """(phrase for the SQL writer, label for the answer) when the question names no period."""
    if src.entities:   # staging: business rule 10's default
        return " over the last 12 full months", "the last 12 full months"
    year = _last_full_year(src)
    if year:
        return f" in {year}", str(year)
    return "", "all the data held"


# ── planning ───────────────────────────────────────────────────────────────

def plan(message: str, src: "data_sources.DataSource", *, single_estate: bool) -> Optional[Plan]:
    """The measures and breakdown for a broad question, or None to answer it the plain way."""
    if len(src.measures) < 2:
        return None
    catalog = "\n".join(f"- {m.id}: {m.label} (breakdowns: {', '.join(m.grains)})" for m in src.measures)
    scope = "one estate" if single_estate else "several estates"
    got = llm_client.chat_json(
        [{"role": "system", "content": load_prompt("chat_overview_plan_system")},
         {"role": "user", "content": f"MEASURES:\n{catalog}\n\nSCOPE: {scope}\n\nQUESTION: {message}"}],
        name="plan_overview", schema=_schema(src),
        description="Record which measures answer the broad question, and how to break them down.",
        # Nova Pro: the planner chooses what the whole answer is about.
        tier="reasoning", max_tokens=300, task="chat_overview_plan",
    )
    if not got:
        return None
    by_id = {m.id: m for m in src.measures}
    ids = list(dict.fromkeys(got["measures"]))
    grain = got["grain"]
    if single_estate and grain == "estate":
        grain = "month"                  # one estate: a breakdown by estate is a single row
    chosen = [by_id[i] for i in ids if grain in by_id[i].grains]
    if len(chosen) < 2:
        # The measures picked do not share this breakdown: fall back to the one they all have.
        grain = "month" if single_estate else "estate"
        chosen = [by_id[i] for i in ids if grain in by_id[i].grains]
    if len(chosen) < 2:
        return None

    asked = (got.get("period") or "").strip(" .")
    if names_period(message):
        phrase = f" {asked}" if asked else f" (the period asked for in: \"{message}\")"
        label = asked or "the period asked for"
        if asked and not re.match(r"(?i)(in|for|during|over|since|from|last|this|the|pada|selama|bulan|tahun)\b",
                                  asked):
            phrase = f" in {asked}"
    else:
        phrase, label = default_period(src)
    return Plan(measures=chosen[:MAX_MEASURES], grain=grain, period=phrase, period_label=label)


_GRAIN_TEXT = {"estate": " per estate", "division": " per division", "block": " per block",
               "month": " per month"}


def sub_question(measure, p: Plan, src: "data_sources.DataSource") -> str:
    """The question the SQL writer gets for one measure of the plan."""
    # Live estates are separate databases: "per estate" is the fan-out itself,
    # so each database is asked for a single figure.
    grain = "" if (p.grain == "estate" and not src.entities) else _GRAIN_TEXT[p.grain]
    return measure.question.format(grain=grain, period=p.period)


# ── merging the parts ──────────────────────────────────────────────────────

_YEAR_COL = re.compile(r"year|tahun|^yr$", re.IGNORECASE)
_MONTH_COL = re.compile(r"month|bulan|^mon$|^mth$", re.IGNORECASE)
_PERIOD_TEXT = re.compile(r"^(\d{4})-(\d{1,2})")
_DIV_COL = re.compile(r"div", re.IGNORECASE)
_BLOCK_COL = re.compile(r"block|blok|^blk", re.IGNORECASE)
# Words a result column may use for each part of a measure id.
_WORDS = {
    "ripe": ["ripe", "masak"], "bunches": ["bunch", "janjang", "jjg", "tandan"], "total": ["total"],
    "rate": ["rate", "pct", "percent", "persen", "share", "ratio"], "ripeness": ["ripe", "pct", "rate"],
    "overripe": ["overripe", "lewat"], "pct": ["pct", "percent", "persen", "share", "rate"],
    "per": ["per"], "ha": ["_ha", "per_ha", "hect", "hektar"], "loose": ["loose", "brondolan"],
    "fruit": ["fruit", "brondolan"], "mandays": ["manday", "man_day", "hk", "hari_kerja"],
    "overtime": ["overtime", "lembur", "hour", "nhour", "duration"], "tonnes": ["ton"],
    "delivered": ["ton", "deliver"], "harvester": ["harvester", "pemanen"],
    "output": ["per_", "avg", "average", "rata"], "absence": ["mangkir", "absen", "absence", "person"],
}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _month_key(row: dict, cols: list[str]):
    year = next((row[c] for c in cols if _YEAR_COL.search(c) and _is_num(row.get(c))), None)
    month = next((row[c] for c in cols if _MONTH_COL.search(c) and _is_num(row.get(c))), None)
    if month is not None:
        return f"{int(year)}-{int(month):02d}" if year is not None else f"{int(month):02d}"
    for c in cols:
        m = _PERIOD_TEXT.match(str(row.get(c) or ""))
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def _key_columns(grain: str, cols: list[str], rows: list[dict], src) -> Optional[list[str]]:
    """The columns that make up the breakdown key in one part's result."""
    if grain == "month":
        used = [c for c in cols if (_YEAR_COL.search(c) or _MONTH_COL.search(c))]
        if not used:
            used = [c for c in cols if rows and _PERIOD_TEXT.match(str(rows[0].get(c) or ""))][:1]
        return used or None
    if grain == "estate":
        if "_source_estate" in cols:
            return ["_source_estate"]
        for c in cols:
            if rows and all(src.entity_code(r.get(c)) for r in rows):
                return [c]
        return None
    divs = [c for c in cols if _DIV_COL.search(c)]
    if grain == "division":
        return divs[:1] or None
    blocks = [c for c in cols if _BLOCK_COL.search(c)]
    # A block code is only unique inside its division, so the division goes with it.
    return (divs[:1] + blocks[:1]) if blocks else None


def _key(grain: str, row: dict, kcols: list[str], cols: list[str], src):
    if grain == "month":
        k = _month_key(row, cols)
    elif grain == "estate":
        v = row.get(kcols[0])
        k = v if kcols[0] == "_source_estate" else src.entity_code(v)
    else:
        k = " / ".join(str(row.get(c) or "").strip().upper() for c in kcols)
    if k is None:
        return None
    if "_source_estate" in row and grain != "estate":
        return (row["_source_estate"], k)      # live fan-out: the estate is part of the key
    return k


def _value_column(measure, cols: list[str], rows: list[dict], kcols: list[str]) -> Optional[str]:
    nums = [c for c in cols if c not in kcols and c != "_source_estate"
            and not (_YEAR_COL.search(c) or _MONTH_COL.search(c))
            and any(_is_num(r.get(c)) for r in rows)]
    if not nums:
        return None
    words = [w for part in measure.id.split("_") for w in _WORDS.get(part, [part])]

    def score(col: str) -> int:
        low = col.lower()
        return sum(1 for w in words if w in low)

    best = max(score(c) for c in nums)
    # Ties go to the later column: a ratio query returns its parts first and the ratio last.
    return [c for c in nums if score(c) == best][-1]


def _keyed(measure, cols: list[str], rows: list[dict], grain: str, src) -> Optional[dict]:
    """{key: value} for one part, or None when its rows cannot be keyed by the breakdown
    (no key column, no value column, or a key that repeats: the part is broken down finer)."""
    if not rows:
        return None
    kcols = _key_columns(grain, cols, rows, src)
    vcol = _value_column(measure, cols, rows, kcols or [])
    if not kcols or not vcol:
        return None
    values: dict = {}
    for r in rows:
        k = _key(grain, r, kcols, cols, src)
        if k is None or k in values:
            return None
        values[k] = r.get(vcol)
    return values


def merge(parts: list[tuple], grain: str, src) -> Optional[tuple[list[str], list[dict], list[str]]]:
    """One table from the parts' results, joined on the breakdown key.

    parts: [(measure, columns, rows)]. Returns (columns, rows, ids of the
    measures in the table), or None when fewer than two parts can be keyed or
    they share no key. A part that cannot be keyed is left out of the table
    (the caller shows it on its own) rather than sinking the others.
    """
    keyed = [(m, v) for m, c, r in parts if (v := _keyed(m, c, r, grain, src)) is not None]
    if len(keyed) < 2:
        return None
    common = set(keyed[0][1])
    for _, values in keyed[1:]:
        common &= set(values)
    if not common:
        return None

    order: list = []
    for _, values in keyed:
        order.extend(k for k in values if k not in order)
    live_split = any(isinstance(k, tuple) for k in order)
    grain_label = grain.capitalize()

    def shown(k):
        if grain == "estate" and src.entities:
            return src.entities[k]["label"]
        return k

    out_rows = []
    for k in order:
        row: dict = {}
        if live_split:
            row["Estate"], k_part = k
            row[grain_label] = k_part
        else:
            row[grain_label] = shown(k)
        for measure, values in keyed:
            row[measure.label] = values.get(k)
        out_rows.append(row)

    first = keyed[0][0].label
    if grain == "month":
        out_rows.sort(key=lambda r: (str(r.get("Estate", "")), str(r[grain_label])))
    else:
        out_rows.sort(key=lambda r: -(r[first] if _is_num(r.get(first)) else float("-inf")))
    columns = (["Estate"] if live_split else []) + [grain_label] + [m.label for m, _ in keyed]
    return columns, out_rows, [m.id for m, _ in keyed]
