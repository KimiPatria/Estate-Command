"""
Reconcile a dashboard widget's chart config against the data the query
actually returned.

The layout LLM picks chart_type/x_field/y_field from the DDL, before any SQL
runs. Two things go wrong with that:

  1. It names a *source* column ("harvesting_plan_total_hk") while the query
     returns an *alias* ("total_hk") — or worse, an unaliased aggregate that
     Postgres names "sum". The field then matches no column, the browser falls
     back to a positional guess, and the chart draws zeros. This was the main
     cause of "the dashboard renders but there's no data".
  2. It picks a chart the result shape cannot support — pie over 300 slices,
     scatter over a text column, big_number over a grouped query.

prompts/layout_system.txt asks the model to get both right; this module is the
deterministic backstop for when it doesn't. Every correction is recorded in
widget.resolver_note so the widget's info drawer can explain what changed.
"""

import logging
import re
from typing import Optional

from layout_schema import WidgetConfig

log = logging.getLogger(__name__)

# Added by dashboard_server._run_widget for head-office (multi-estate) fan-out.
# It is a provenance tag, never a chart axis — picking it as y_field is what
# produced all-zero charts, since toNum("BA") is 0.
ESTATE_COL = "_source_estate"

# Above this, a pie is unreadable; the same data reads fine as a ranked bar.
MAX_PIE_SLICES = 8
# A bar chart over this many time buckets should be a line.
MAX_BAR_TIME_POINTS = 15
# Below this a radar has too few axes to form a shape.
MIN_RADAR_AXES = 3

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]|$)")
_YEAR_MONTH = re.compile(r"^\d{4}[-/](0?[1-9]|1[0-2])$")
_NUMERIC_STR = re.compile(r"^-?\d+(\.\d+)?$")
_TIME_NAME = re.compile(r"(date|month|week|year|day|period|bucket|quarter|time)", re.I)
# Identifier-shaped names. Only consulted for columns whose values arrive as
# strings: this schema stores division/block/estate codes as varchar digits
# ("3", "007"), and charting one as the measure is as wrong as charting the
# estate tag. A real COUNT/SUM comes back as an int or float and never reaches
# this check.
_DIM_NAME = re.compile(
    r"(_(code|id|no|key|kode|nomor)$|^(id|code|no)$|"
    r"division|afdeling|estate|block|blok|company|section|department|"
    r"category|status|type|group|shift|gang)",
    re.I,
)


def _sample(rows: list[dict], col: str, limit: int = 50) -> list:
    """Non-null values for a column, capped — role detection does not need
    the whole result set."""
    out = []
    for r in rows:
        v = r.get(col)
        if v is not None and v != "":
            out.append(v)
            if len(out) >= limit:
                break
    return out


def _role(col: str, rows: list[dict]) -> str:
    """One of "temporal" | "numeric" | "categorical".

    Temporal is checked first: _coerce() in dashboard_server stringifies dates,
    so a DATE_TRUNC bucket arrives as "2024-01-01 00:00:00" and would otherwise
    just look like text. Numeric accepts digit-strings too — this schema stores
    many quantities as varchar, and the browser parses them with parseFloat.
    """
    vals = _sample(rows, col)
    if not vals:
        return "temporal" if _TIME_NAME.search(col) else "categorical"

    strs = [str(v) for v in vals]
    if all(_ISO_DATE.match(s) or _YEAR_MONTH.match(s) for s in strs):
        return "temporal"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        # A 4-digit int under a date-ish name is a year bucket, i.e. an axis.
        if _TIME_NAME.search(col) and all(1900 <= v <= 2200 for v in vals):
            return "temporal"
        return "numeric"
    if all(_NUMERIC_STR.match(s) for s in strs):
        # Digits arriving as text: either a varchar quantity the SQL forgot to
        # CAST, or a code. A decimal point or a leading zero settles it;
        # otherwise go by the column name.
        if any("." in s for s in strs):
            return "numeric"
        if _DIM_NAME.search(col) or any(len(s) > 1 and s[0] == "0" for s in strs):
            return "categorical"
        return "numeric"
    return "categorical"


def _distinct(rows: list[dict], col: Optional[str]) -> int:
    if not col:
        return 0
    return len({str(r.get(col)) for r in rows})


def _all_null(rows: list[dict], col: Optional[str]) -> bool:
    if not col:
        return True
    return all(r.get(col) is None or r.get(col) == "" for r in rows)


def resolve_widget(widget: WidgetConfig, columns: list[str], rows: list[dict]) -> None:
    """Patch widget.chart_type / x_field / y_field / group_by in place so they
    describe data that is actually present. No-op when the query returned
    nothing — there is no shape to reconcile against, and the LLM's config is
    still the most informative thing to show in the info drawer."""
    if not rows or not columns:
        return

    before = (widget.chart_type, widget.x_field, widget.y_field, widget.group_by)

    cands = [c for c in columns if c != ESTATE_COL]
    if not cands:
        return
    roles = {c: _role(c, rows) for c in cands}
    numerics    = [c for c in cands if roles[c] == "numeric"]
    temporals   = [c for c in cands if roles[c] == "temporal"]
    categoricals = [c for c in cands if roles[c] == "categorical"]

    def valid(name: Optional[str]) -> Optional[str]:
        return name if name in cands else None

    x_hint, y_hint, g_hint = valid(widget.x_field), valid(widget.y_field), valid(widget.group_by)
    ct = widget.chart_type

    # ── pick the measure ──────────────────────────────────────────────────
    # Aggregates trail the dimensions in a GROUP BY select list, so the last
    # numeric column is the better positional guess than the first.
    if ct in ("scatter", "bubble"):
        xy = [c for c in (x_hint, y_hint) if c in numerics]
        rest = [c for c in numerics if c not in xy]
        pair = (xy + rest)[:2]
        if len(pair) == 2:
            widget.x_field, widget.y_field = pair
            widget.group_by = g_hint if g_hint not in pair else None
            _finish(widget, before)
            return
        # Not enough measures for an XY plot — fall through and re-pick as a
        # categorical comparison below.
        ct = "bar" if (categoricals or temporals) and numerics else "table"
        x_hint = x_hint if x_hint in (categoricals + temporals) else None

    # A column that is null in every row has no detectable role, but it is
    # still the measure a SUM(...) widget was asking for — keep it as a
    # last-resort candidate so an empty KPI can say "N/A" rather than losing
    # its field entirely.
    measures = numerics or [c for c in cands if not _sample(rows, c, 1)]
    y = y_hint if y_hint in numerics else (measures[-1] if measures else None)

    # ── pick the dimension ────────────────────────────────────────────────
    dims = [c for c in (temporals + categoricals) if c != y]
    if x_hint and x_hint != y:
        x = x_hint
    elif dims:
        x = dims[0]
    else:
        x = next((c for c in cands if c != y), None)

    g = g_hint if g_hint and g_hint not in (x, y) else None

    # ── repair the chart type against the shape ───────────────────────────
    x_role = roles.get(x, "categorical")
    n_x = _distinct(rows, x)

    all_null = y is not None and _all_null(rows, y)

    if y is None or (all_null and ct != "big_number"):
        # Nothing plottable. A table at least shows the rows that came back
        # instead of an axis with no marks on it. A KPI is the exception: its
        # card already renders a clean "N/A", which reads better than a
        # one-cell table of nothing.
        ct = "table"
    elif ct == "big_number":
        # A grouped query behind a KPI widget: more distinct dimension values
        # than the estate fan-out can explain means this is a comparison.
        if x and n_x > 1 and not all_null:
            ct = "line" if x_role == "temporal" else "bar"
    elif ct in ("multi_line", "stacked_bar", "radar") and not g:
        ct = {"multi_line": "line", "stacked_bar": "bar", "radar": "bar"}[ct]
    elif ct == "pie":
        if x_role == "temporal":
            ct = "line"
        elif not x:
            ct = "table"
        elif n_x > MAX_PIE_SLICES:
            ct = "horizontal_bar"
    elif ct in ("line", "multi_line") and x_role == "categorical":
        ct = "bar" if ct == "line" else "stacked_bar"
    elif ct in ("bar", "horizontal_bar") and x_role == "temporal" and n_x > MAX_BAR_TIME_POINTS:
        ct = "multi_line" if g else "line"

    if ct == "radar" and n_x < MIN_RADAR_AXES:
        ct = "bar"

    if ct == "table":
        widget.chart_type = "table"
        widget.x_field = widget.y_field = widget.group_by = None
        _finish(widget, before)
        return

    widget.chart_type = ct
    if ct == "big_number":
        widget.x_field, widget.y_field, widget.group_by = None, y, None
    else:
        widget.x_field, widget.y_field, widget.group_by = x, y, g
    _finish(widget, before)


def _finish(widget: WidgetConfig, before: tuple) -> None:
    after = (widget.chart_type, widget.x_field, widget.y_field, widget.group_by)
    if after == before:
        return
    parts = []
    for label, old, new in zip(("chart_type", "x_field", "y_field", "group_by"), before, after):
        if old != new:
            parts.append(f"{label}: {old or '—'} → {new or '—'}")
    widget.resolver_note = "Adjusted to match the returned columns · " + ", ".join(parts)
    log.info("[resolver] %s %s", widget.id, "; ".join(parts))
