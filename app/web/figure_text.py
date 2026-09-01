"""One rendering of a numeric cell, mirrored in static/figure_text.js."""

from __future__ import annotations

from typing import Any

# Four digits stay bare: a year and a row ordinal are not quantities.
GROUPS_FROM = 10_000


def render_figure(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if abs(value) < GROUPS_FROM:
        return str(value)
    return format(value, ",")
