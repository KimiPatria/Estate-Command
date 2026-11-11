"""Charts for chat answers.

After a query has run, the model is asked which chart (if any) shows the result
best: it names a chart type and the columns to put on it, nothing else. What
it says is checked against the real columns, and the numbers on the chart are
taken from the rows by this module, never from the model, so a chart cannot
show a figure the database did not return. If the model is unavailable or
answers nonsense, a plain rule picks the chart instead.

The payload is renderer-neutral: labels plus one list of values per series.
The page draws it (chat_static/chat.js) and lets the user switch type.

Form rules follow the house data-viz guidance: time -> line, comparison -> bars,
rankings and long names -> horizontal bars, never two y-scales on one chart
(measures of very different size are not drawn together), a pie only when
someone asks for one, and at most 8 series.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("epms-chat")

CHART_TYPES = ("bar", "hbar", "line", "area", "pie", "scatter")

MAX_BAR_LABELS = 40
MAX_LINE_POINTS = 500
MAX_PIE_SLICES = 6
MAX_SERIES = 8
MAX_SCATTER = 500
SCALE_RATIO = 50          # measures further apart than this don't share an axis

_WANTS = re.compile(
    r"\b(?:charts?|graphs?|plot(?:s|ted|ting)?|visuali[sz]\w*|diagram\w*|draw"
    r"|grafik|bagan|gambarkan|visualisasi\w*|kurva|tren(?:d)? line)\b", re.IGNORECASE)
_TYPE_WORDS = [
    ("pie", re.compile(r"\b(?:pie|donut|doughnut|lingkaran)\b", re.IGNORECASE)),
    ("scatter", re.compile(r"\b(?:scatter|sebar)\b", re.IGNORECASE)),
    ("area", re.compile(r"\barea chart\b", re.IGNORECASE)),
    ("line", re.compile(r"\b(?:line|garis)\b", re.IGNORECASE)),
    ("hbar", re.compile(r"\bhorizontal\b", re.IGNORECASE)),
    ("bar", re.compile(r"\b(?:bar|column|batang|kolom)\b", re.IGNORECASE)),
]

_ISO_DATE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")
_YEAR_COL = re.compile(r"^(?:year|yr|tahun|thn)$|(?:^|_)(?:year|tahun)$", re.IGNORECASE)
_MONTH_COL = re.compile(r"^(?:month|mon|mo|bulan|bln)$|(?:^|_)(?:month|bulan)$", re.IGNORECASE)
_TIME_COL = re.compile(r"(?:year|tahun|month|bulan|week|minggu|quarter|kuartal|period|date|tanggal|day|hari)",
                       re.IGNORECASE)
_ID_COL = re.compile(r"(?:^id$|_id$|^no$|_no$|code$|kode$|nr$|number$|^werks$|^block$|^blok$)", re.IGNORECASE)
_NAME_COL = re.compile(r"(?:name|nama|label|estate|division|divisi|block|blok|activity|job)", re.IGNORECASE)


def wants_chart(message: str) -> bool:
    """Did the user ask to see a chart?"""
    return bool(_WANTS.search(message or ""))


def requested_type(message: str) -> Optional[str]:
    """A chart type the user named, if any ("as a pie chart")."""
    if not wants_chart(message):
        return None
    for name, pattern in _TYPE_WORDS:
        if pattern.search(message):
            return name
    return None


# ── reading the result ─────────────────────────────────────────────────────

def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def profile(columns: list[str], rows: list[dict]) -> tuple[list[str], list[dict], dict[str, str]]:
    """Column kinds ("number" | "date" | "category") and tidied rows.

    A separate year and month column are folded into one "period" column
    (2026-03), which is what month-by-month SQL usually returns and what a
    time axis needs.
    """
    columns = list(columns)
    rows = [dict(r) for r in rows]

    ycol = next((c for c in columns if _YEAR_COL.search(c)), None)
    mcol = next((c for c in columns if _MONTH_COL.search(c) and c != ycol), None)
    if ycol and mcol and "period" not in columns:
        ok = all(_is_number(r.get(ycol)) and _is_number(r.get(mcol)) for r in rows)
        if ok and rows:
            for r in rows:
                r["period"] = f"{int(r[ycol]):04d}-{int(r[mcol]):02d}"
            at = min(columns.index(ycol), columns.index(mcol))
            columns = [c for c in columns if c not in (ycol, mcol)]
            columns.insert(at, "period")

    kinds: dict[str, str] = {}
    for c in columns:
        vals = [r.get(c) for r in rows if r.get(c) is not None and r.get(c) != ""]
        if not vals:
            kinds[c] = "category"
        elif all(_is_number(v) for v in vals):
            # A year, a month number or a code is something to group by, not a measure.
            if _ID_COL.search(c):
                kinds[c] = "category"
            elif _TIME_COL.search(c) and _calendar_values(vals):
                kinds[c] = "date"
            else:
                kinds[c] = "number"
        elif all(isinstance(v, str) and _ISO_DATE.match(v) for v in vals):
            kinds[c] = "date"
        else:
            kinds[c] = "category"
    return columns, rows, kinds


def _calendar_values(vals: list) -> bool:
    """Whole numbers small enough to be a year, month, week or day number.
    "ripe_bunches_last_month" has "month" in its name and is still a measure."""
    return all(float(v).is_integer() and (1 <= v <= 366 or 1900 <= v <= 2100) for v in vals)


def _distinct(rows: list[dict], col: str) -> int:
    return len({str(r.get(col)) for r in rows})


# ── choosing the chart ─────────────────────────────────────────────────────

def heuristic(question: str, columns: list[str], kinds: dict[str, str],
              rows: list[dict], force: bool = False) -> Optional[dict]:
    """A sensible chart with no model involved, or None when a chart adds nothing."""
    nums = [c for c in columns if kinds[c] == "number"]
    dates = [c for c in columns if kinds[c] == "date"]
    cats = [c for c in columns if kinds[c] == "category"]
    if not nums or len(rows) < 2:
        return None

    asked = requested_type(question)
    spec: Optional[dict] = None
    if dates:
        x = dates[0]
        series = next((c for c in cats if 1 < _distinct(rows, c) <= MAX_SERIES
                       and _distinct(rows, x) < len(rows)), None)
        spec = {"type": "line", "x": x, "y": nums[:1] if series else nums[:3], "series": series}
    elif cats:
        full = [c for c in cats if _distinct(rows, c) == len(rows)]
        named = [c for c in (full or cats) if _NAME_COL.search(c)]
        x = (named or full or cats)[0]
        series = None
        if _distinct(rows, x) < len(rows):
            series = next((c for c in cats if c != x and 1 < _distinct(rows, c) <= MAX_SERIES), None)
        if len(rows) > 60 and not force:
            return None
        longest = max((len(str(r.get(x))) for r in rows), default=0)
        kind = "hbar" if (len(rows) > 8 or longest > 14) and not series else "bar"
        spec = {"type": kind, "x": x, "y": nums[:1] if series else nums[:3], "series": series}
    elif len(nums) >= 2:
        spec = {"type": "scatter", "x": nums[0], "y": [nums[1]], "series": None}
    if spec is None:
        return None
    if asked:
        spec["type"] = asked
    spec["title"] = ""
    return validate(spec, columns, kinds)


def validate(raw, columns: list[str], kinds: dict[str, str]) -> Optional[dict]:
    """A chart spec that only names real columns in roles they can play, or None."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or raw.get("chart") or "").lower().strip()
    if kind in ("column", "columns"):
        kind = "bar"
    if kind in ("donut", "doughnut"):
        kind = "pie"
    if kind not in CHART_TYPES:
        return None

    lookup = {c.lower(): c for c in columns}

    def col(name) -> Optional[str]:
        return lookup.get(str(name).lower().strip()) if name else None

    x = col(raw.get("x"))
    ys = raw.get("y")
    ys = [ys] if isinstance(ys, str) else (ys if isinstance(ys, list) else [])
    ys = [c for c in (col(y) for y in ys) if c and kinds[c] == "number"]
    ys = list(dict.fromkeys(ys))[:4]
    series = col(raw.get("series"))
    if not x or not ys or x in ys:
        return None
    if series in (x, *ys) or (series and kinds[series] == "number"):
        series = None
    if kind == "scatter":
        if kinds[x] != "number":
            return None
        ys, series = ys[:1], None
    elif kind in ("line", "area") and kinds[x] == "number":
        return None
    if kind == "pie":
        ys, series = ys[:1], None
    if series:
        ys = ys[:1]
    title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()[:90]
    return {"type": kind, "x": x, "y": ys, "series": series, "title": title}


def llm_plan(question: str, columns: list[str], kinds: dict[str, str],
             rows: list[dict]) -> Optional[dict] | bool:
    """Ask the model for a chart. Returns a spec, False for "no chart", None on failure."""
    import json

    import llm_client
    from gis.reasoning import parse_json
    from prompts import load_prompt

    cols = "\n".join(f"- {c} ({kinds[c]}, {_distinct(rows, c)} distinct)" for c in columns)
    sample = json.dumps([{c: r.get(c) for c in columns} for r in rows[:5]],
                        default=str, ensure_ascii=False)
    try:
        result = llm_client.chat(
            [{"role": "system", "content": load_prompt("chat_chart_system")},
             {"role": "user", "content": (f"QUESTION: {question}\n\nROWS RETURNED: {len(rows)}\n\n"
                                          f"COLUMNS:\n{cols}\n\nFIRST ROWS: {sample}")}],
            tier="routing", temperature=0, max_tokens=160, json_mode=True, task="chat_chart",
        )
    except Exception as exc:
        log.warning("[chat] chart planner failed: %s", exc)
        return None
    parsed = parse_json(result.text or "")
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("chart") or parsed.get("type") or "").lower() in ("none", "null", ""):
        return False
    return validate(parsed, columns, kinds)


# ── drawing data ───────────────────────────────────────────────────────────

def pretty(name: str) -> str:
    """ripe_bunches_last_month -> Ripe bunches last month"""
    text = re.sub(r"[_\s]+", " ", str(name)).strip()
    return text[:1].upper() + text[1:] if text.islower() or text.isupper() else text


def _label(v) -> str:
    if v is None or v == "":
        return "(blank)"
    s = str(v)
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}[ T]00:00:00", s) else s


def _num(v) -> Optional[float]:
    return float(v) if _is_number(v) else None


def _same_scale(rows: list[dict], ys: list[str]) -> list[str]:
    """Keep the measures that can share one axis with the first."""
    def peak(c):
        return max((abs(r[c]) for r in rows if _is_number(r.get(c))), default=0)
    base = peak(ys[0])
    keep = [ys[0]]
    for c in ys[1:]:
        p = peak(c)
        if base and p and max(base, p) / min(base, p) <= SCALE_RATIO:
            keep.append(c)
    return keep


def build(spec: dict, rows: list[dict], kinds: dict[str, str]) -> Optional[dict]:
    """The chart payload: every number comes from `rows`."""
    kind, x, ys, series = spec["type"], spec["x"], list(spec["y"]), spec.get("series")
    truncated = False

    if kind == "scatter":
        y = ys[0]
        pts = [[r[x], r[y]] for r in rows if _is_number(r.get(x)) and _is_number(r.get(y))]
        if len(pts) < 2:
            return None
        truncated = len(pts) > MAX_SCATTER
        return {"type": "scatter", "title": spec.get("title", ""), "x_label": pretty(x),
                "y_label": pretty(y), "labels": [], "truncated": truncated,
                "series": [{"name": pretty(y), "points": pts[:MAX_SCATTER]}]}

    rows = [r for r in rows if r.get(x) is not None]
    if kinds.get(x) == "date":
        rows = sorted(rows, key=lambda r: str(r.get(x)))
    ys = _same_scale(rows, ys)

    labels: list[str] = []
    seen: dict[str, int] = {}
    for r in rows:
        lab = _label(r.get(x))
        if lab not in seen:
            seen[lab] = len(labels)
            labels.append(lab)

    out: list[dict] = []
    if series:
        totals: dict[str, float] = {}
        grid: dict[str, list] = {}
        for r in rows:
            name = _label(r.get(series))
            v = _num(r.get(ys[0]))
            col = grid.setdefault(name, [None] * len(labels))
            i = seen[_label(r.get(x))]
            col[i] = v if col[i] is None else (col[i] + (v or 0))
            totals[name] = totals.get(name, 0) + abs(v or 0)
        names = sorted(grid, key=lambda n: -totals[n])
        truncated = len(names) > MAX_SERIES
        out = [{"name": n, "values": grid[n]} for n in names[:MAX_SERIES]]
    else:
        for y in ys:
            vals: list = [None] * len(labels)
            for r in rows:
                i = seen[_label(r.get(x))]
                v = _num(r.get(y))
                if v is not None:
                    vals[i] = v if vals[i] is None else vals[i] + v
            out.append({"name": pretty(y), "values": vals})

    if kind in ("hbar", "pie") and len(out) == 1:
        order = sorted(range(len(labels)), key=lambda i: -(out[0]["values"][i] or 0))
        labels = [labels[i] for i in order]
        out[0]["values"] = [out[0]["values"][i] for i in order]

    if kind == "pie":
        vals = out[0]["values"]
        if any((v or 0) < 0 for v in vals) or sum(v or 0 for v in vals) <= 0:
            kind = "hbar"                    # a share of a negative total means nothing
        elif len(labels) > MAX_PIE_SLICES:
            rest = sum(v or 0 for v in vals[MAX_PIE_SLICES - 1:])
            labels = labels[:MAX_PIE_SLICES - 1] + ["Other"]
            out[0]["values"] = vals[:MAX_PIE_SLICES - 1] + [rest]

    cap = MAX_LINE_POINTS if kind in ("line", "area") else MAX_BAR_LABELS
    if kind != "pie" and len(labels) > cap:
        truncated = True
        labels = labels[:cap]
        for s in out:
            s["values"] = s["values"][:cap]

    if len(labels) < 2 or not any(v is not None for s in out for v in s["values"]):
        return None
    return {
        "type": kind,
        "title": spec.get("title", ""),
        "x_label": pretty(x),
        "y_label": pretty(ys[0]) if len(out) == 1 or series else "",
        "x_kind": kinds.get(x, "category"),
        "labels": labels,
        "series": out,
        "truncated": truncated,
    }


def plan_chart(question: str, columns: list[str], rows: list[dict],
               use_llm: bool = True) -> Optional[dict]:
    """The chart for a result, or None. Never raises: a chart is optional."""
    try:
        if not columns or len(rows) < 2:
            return None
        columns, rows, kinds = profile(columns, rows)
        if not any(k == "number" for k in kinds.values()):
            return None
        force = wants_chart(question)
        spec = llm_plan(question, columns, kinds, rows) if use_llm else None
        if spec is False and not force:
            return None
        if not spec:
            spec = heuristic(question, columns, kinds, rows, force=force)
        if not spec:
            return None
        asked = requested_type(question)
        if asked and spec["type"] != asked:
            spec = validate({**spec, "type": asked}, columns, kinds) or spec
        if spec["type"] == "pie" and asked != "pie":
            spec["type"] = "hbar"            # a pie only when someone asks for one
        return build(spec, rows, kinds)
    except Exception as exc:
        log.warning("[chat] chart skipped: %s", exc)
        return None
