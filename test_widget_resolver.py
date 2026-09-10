"""
Regression tests for widget_resolver — the layer that reconciles a dashboard
widget's chart config with the columns its query actually returned.

Every shape below was captured from a real /api/generate response against the
BA + K3 databases while chasing "the dashboard renders but the charts are
empty". Run with:  pytest test_widget_resolver.py
"""

import pytest

from layout_schema import DashboardLayout, WidgetConfig
from widget_resolver import resolve_widget


def W(**kw) -> WidgetConfig:
    kw.setdefault("id", "w1")
    kw.setdefault("title", "t")
    kw.setdefault("table", "t_x")
    return WidgetConfig(**kw)


def resolved(widget: WidgetConfig, columns: list[str], rows: list[dict]) -> tuple:
    resolve_widget(widget, columns, rows)
    return widget.chart_type, widget.x_field, widget.y_field, widget.group_by


# ── the original bug ───────────────────────────────────────────────────────

def test_unaliased_aggregate_is_mapped_to_the_real_column():
    """The layout LLM names the source column; Postgres returns "sum". The
    browser then fell back to the last column — _source_estate — and charted
    toNum("BA") == 0 for every point."""
    assert resolved(
        W(chart_type="line", x_field="harvesting_plan_date", y_field="harvesting_plan_total_hk"),
        ["harvesting_plan_date", "sum", "_source_estate"],
        [{"harvesting_plan_date": "2024-07-02", "sum": 68, "_source_estate": "BA"},
         {"harvesting_plan_date": "2024-07-03", "sum": 71, "_source_estate": "BA"}],
    ) == ("line", "harvesting_plan_date", "sum", None)


def test_estate_tag_is_never_an_axis():
    widget = W(chart_type="bar", x_field="nope", y_field="also_nope")
    resolve_widget(widget, ["estate_code", "sum", "_source_estate"],
                   [{"estate_code": "BA", "sum": 2097, "_source_estate": "BA"}])
    assert "_source_estate" not in (widget.x_field, widget.y_field, widget.group_by)


# ── chart type vs. result shape ────────────────────────────────────────────

def test_big_number_over_a_grouped_query_becomes_a_comparison():
    assert resolved(
        W(chart_type="big_number", y_field="actual_ha"),
        ["division", "total_ha", "_source_estate"],
        [{"division": "1", "total_ha": 10.0, "_source_estate": "K3"},
         {"division": "2", "total_ha": 20.0, "_source_estate": "K3"},
         {"division": "3", "total_ha": 30.0, "_source_estate": "K3"}],
    ) == ("bar", "division", "total_ha", None)


def test_genuine_kpi_is_left_alone():
    """One row per estate is the head-office fan-out, not a grouped query."""
    assert resolved(
        W(chart_type="big_number", y_field="harvest_completion_actual_ha"),
        ["total_ha", "_source_estate"],
        [{"total_ha": 218717.0, "_source_estate": "K3"},
         {"total_ha": 9000.0, "_source_estate": "BA"}],
    ) == ("big_number", None, "total_ha", None)


def test_scatter_without_two_measures_falls_back_to_bar():
    assert resolved(
        W(chart_type="scatter", x_field="activity_code", y_field="qty"),
        ["activity_code", "total_qty", "_source_estate"],
        [{"activity_code": "HRV", "total_qty": 5, "_source_estate": "BA"}],
    ) == ("bar", "activity_code", "total_qty", None)


def test_real_scatter_is_preserved():
    assert resolved(
        W(chart_type="scatter", x_field="area_ha", y_field="tonnes"),
        ["block", "area_ha", "tonnes", "_source_estate"],
        [{"block": "A1", "area_ha": 12.5, "tonnes": 40.0, "_source_estate": "BA"}],
    ) == ("scatter", "area_ha", "tonnes", None)


def test_pie_with_too_many_slices_becomes_a_ranked_bar():
    assert resolved(
        W(chart_type="pie", x_field="block", y_field="total"),
        ["block", "total"],
        [{"block": f"B{i}", "total": i} for i in range(12)],
    ) == ("horizontal_bar", "block", "total", None)


def test_pie_over_time_becomes_a_line():
    assert resolved(
        W(chart_type="pie", x_field="month", y_field="total"),
        ["month", "total"],
        [{"month": "2024-01-01", "total": 5}, {"month": "2024-02-01", "total": 9}],
    ) == ("line", "month", "total", None)


@pytest.mark.parametrize("requested,expected", [
    ("multi_line", "line"), ("stacked_bar", "bar"), ("radar", "bar"),
])
def test_grouped_charts_degrade_when_the_group_column_is_absent(requested, expected):
    assert resolved(
        W(chart_type=requested, x_field="month", y_field="total", group_by="division_code"),
        ["month", "total"],
        [{"month": "2024-01-01", "total": 5}],
    ) == (expected, "month", "total", None)


def test_group_is_kept_when_the_column_is_present():
    assert resolved(
        W(chart_type="multi_line", x_field="month", y_field="total", group_by="division"),
        ["month", "division", "total", "_source_estate"],
        [{"month": "2024-01-01", "division": "1", "total": 5, "_source_estate": "K3"}],
    ) == ("multi_line", "month", "total", "division")


def test_bar_over_many_time_buckets_becomes_a_line():
    rows = [{"month": f"{y}-{m:02d}-01", "total": m} for y in (2023, 2024) for m in range(1, 13)]
    assert resolved(W(chart_type="bar", x_field="month", y_field="total"),
                    ["month", "total"], rows) == ("line", "month", "total", None)


def test_line_over_a_plain_category_becomes_a_bar():
    assert resolved(
        W(chart_type="line", x_field="division", y_field="total"),
        ["division", "total"],
        [{"division": "1", "total": 5}, {"division": "2", "total": 8}],
    ) == ("bar", "division", "total", None)


def test_chart_with_no_plottable_measure_becomes_a_table():
    assert resolved(
        W(chart_type="bar", x_field="division", y_field="qty"),
        ["division", "sum", "_source_estate"],
        [{"division": "1", "sum": None, "_source_estate": "BA"}],
    ) == ("table", None, None, None)


def test_null_kpi_stays_a_kpi():
    """The card renders "N/A", which is clearer than a one-cell empty table."""
    assert resolved(
        W(chart_type="big_number", y_field="qty"),
        ["sum", "_source_estate"],
        [{"sum": None, "_source_estate": "BA"}, {"sum": None, "_source_estate": "K3"}],
    ) == ("big_number", None, "sum", None)


def test_empty_result_is_left_untouched():
    assert resolved(W(chart_type="scatter", x_field="a", y_field="b"), ["a", "b"], []) \
        == ("scatter", "a", "b", None)


# ── column role detection ──────────────────────────────────────────────────

def test_varchar_quantities_still_count_as_a_measure():
    assert resolved(
        W(chart_type="bar", x_field="block", y_field="ha"),
        ["block", "ha"],
        [{"block": "A", "ha": "12.5"}, {"block": "B", "ha": "9.0"}],
    ) == ("bar", "block", "ha", None)


def test_varchar_codes_are_a_dimension_not_a_measure():
    """division_code arrives as "3" — a digit string, but an axis."""
    assert resolved(
        W(chart_type="bar"),
        ["harvest_completion_division_code", "sum", "_source_estate"],
        [{"harvest_completion_division_code": "3", "sum": 6663.0, "_source_estate": "K3"},
         {"harvest_completion_division_code": "2", "sum": 1200.0, "_source_estate": "K3"}],
    ) == ("bar", "harvest_completion_division_code", "sum", None)


def test_year_column_is_a_time_axis():
    assert resolved(
        W(chart_type="bar", x_field="year", y_field="total"),
        ["year", "total"],
        [{"year": y, "total": 5} for y in (2022, 2023, 2024)],
    ) == ("bar", "year", "total", None)


def test_date_trunc_timestamps_are_temporal():
    assert resolved(
        W(chart_type="pie", x_field="month", y_field="total"),
        ["month", "total"],
        [{"month": "2024-05-01 00:00:00+07:00", "total": 232},
         {"month": "2024-06-01 00:00:00+07:00", "total": 410}],
    ) == ("line", "month", "total", None)


# ── schema tolerance ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("donut", "pie"), ("KPI", "big_number"), ("horizontal-bar", "horizontal_bar"),
    ("area", "line"), ("column", "bar"), ("sunburst", "table"),
])
def test_chart_type_aliases_are_normalised(raw, expected):
    assert W(chart_type=raw).chart_type == expected


def test_null_like_strings_become_none():
    w = W(chart_type="bar", x_field="null", y_field=" total ", group_by="")
    assert (w.x_field, w.y_field, w.group_by) == (None, "total", None)


def test_unknown_chart_type_does_not_fail_the_whole_layout():
    """This used to raise ValidationError, 422 the request, and leave the user
    with an empty page instead of a dashboard."""
    layout = DashboardLayout.model_validate_json(
        '{"title":"T","widgets":[{"id":"w1","chart_type":"sankey","title":"x",'
        '"table":"t"}],"tables_used":["t"]}'
    )
    assert layout.widgets[0].chart_type == "table"
