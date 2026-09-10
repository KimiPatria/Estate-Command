import logging

from layout_schema import CHART_TYPES
from prompts import load_prompt

log = logging.getLogger(__name__)


def build_layout_messages(ddl: str, goal: str, max_widgets: int) -> list[dict]:
    """Build chat messages for the dashboard layout suggester.

    Returns a two-element list [system, user] matching the same dict structure
    used by build_sql_messages() in prompt_builder.py.

    The chart enum is injected from layout_schema.CHART_TYPES rather than
    written out in the template, so the prompt cannot drift from the types the
    schema accepts and the browser can render.
    """
    system_content = load_prompt(
        "layout_system",
        max_widgets=max_widgets,
        chart_types=", ".join(CHART_TYPES),
    )

    user_content = (
        f"DDL:\n{ddl}\n\n"
        f"Dashboard goal: {goal}\n\n"
        f"Return a JSON layout with 3–{max_widgets} widgets."
    )

    log.info(
        "[layout_prompt] ddl_chars=%d goal_chars=%d max_widgets=%d",
        len(ddl), len(goal), max_widgets,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user",   "content": user_content},
    ]
