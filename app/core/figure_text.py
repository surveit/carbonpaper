"""One rendering of a numeric cell, mirrored in app/static/figure_text.js."""

from __future__ import annotations

# Four digits stay bare: a year and a row ordinal are not quantities.
GROUPS_FROM = 10_000


def render_figure(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if abs(value) < GROUPS_FROM:
        return str(value)
    return format(value, ",")
