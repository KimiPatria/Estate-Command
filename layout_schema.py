from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Canonical visualisation catalogue. This is the single source of truth:
# prompts/layout_system.txt is fed CHART_TYPES from here, widget_resolver.py
# switches between these names, and dashboard_static/index.html has a renderer
# for each one. Adding a type means touching all three.
CHART_TYPES = (
    "big_number",
    "line",
    "multi_line",
    "bar",
    "horizontal_bar",
    "stacked_bar",
    "pie",
    "scatter",
    "bubble",
    "radar",
    "table",
)

ChartType = Literal[
    "big_number", "line", "multi_line", "bar", "horizontal_bar",
    "stacked_bar", "pie", "scatter", "bubble", "radar", "table",
]

# Near-misses seen from the layout LLM. Normalised rather than rejected: an
# unknown chart_type used to fail model_validate_json and take the *entire*
# dashboard down with a 422, which is a bad trade for a naming variant.
_CHART_ALIASES = {
    "column": "bar", "column_chart": "bar", "vertical_bar": "bar", "barchart": "bar",
    "hbar": "horizontal_bar", "bar_horizontal": "horizontal_bar", "horizontalbar": "horizontal_bar",
    "stacked": "stacked_bar", "stackedbar": "stacked_bar", "stacked_column": "stacked_bar",
    "grouped_bar": "stacked_bar", "stacked_area": "multi_line",
    "linechart": "line", "area": "line", "area_chart": "line", "timeseries": "line",
    "time_series": "line", "trend": "line",
    "multiline": "multi_line", "multi_series_line": "multi_line", "lines": "multi_line",
    "donut": "pie", "doughnut": "pie", "pie_chart": "pie",
    "scatterplot": "scatter", "scatter_plot": "scatter", "xy": "scatter",
    "kpi": "big_number", "metric": "big_number", "number": "big_number",
    "single_value": "big_number", "stat": "big_number", "bignumber": "big_number",
    "grid": "table", "data_table": "table", "list": "table",
    "gauge": "big_number", "heatmap": "table", "histogram": "bar", "funnel": "bar",
    "treemap": "horizontal_bar", "waterfall": "bar", "combo": "bar",
}


class WidgetConfig(BaseModel):
    id: str
    chart_type: ChartType
    title: str
    x_field: Optional[str] = None
    y_field: Optional[str] = None
    group_by: Optional[str] = None
    table: str
    sql_hint: Optional[str] = None   # SELECT skeleton; validated but not executed
    grid: dict = Field(default_factory=lambda: {"x": 0, "y": 0, "w": 6, "h": 4})
    # Set by widget_resolver after the query runs, when the LLM's chart_type or
    # field choices had to be corrected against the columns actually returned.
    resolver_note: Optional[str] = None

    @field_validator("chart_type", mode="before")
    @classmethod
    def _normalise_chart_type(cls, v):
        if not isinstance(v, str):
            return v
        key = v.strip().lower().replace("-", "_").replace(" ", "_")
        if key in CHART_TYPES:
            return key
        return _CHART_ALIASES.get(key, "table")

    @field_validator("x_field", "y_field", "group_by", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        # Nova/GPT-OSS both emit "null", "none" or "" instead of JSON null.
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"):
            return None
        return v.strip() if isinstance(v, str) else v


class DashboardLayout(BaseModel):
    title: str
    widgets: list[WidgetConfig]
    tables_used: list[str] = Field(default_factory=list)


class LayoutRequest(BaseModel):
    goal: str
    max_widgets: int = 6
