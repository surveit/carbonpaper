"""The run's own workflow graph, read a second time as one figure's column walk."""

from __future__ import annotations

from enum import Enum

from app.models.schema import StageId
from app.web.diagrams import DiagramOverlay


class WalkState(str, Enum):
    """What this figure owes a stage: rows, nothing, or no part of it at all."""

    lit = "lit"
    dry = "dry"
    aside = "aside"


# A mermaid `style` line cannot spend a custom property, so these copy palette.css.
WALK_LIT_STROKE = "#1d539c"
WALK_QUIET_STROKE = "#d4d2ca"
WALK_ASIDE_FILL = "#eeede7"
WALK_ASIDE_INK = "#83878d"

_WALK_STYLE = {
    WalkState.lit: f"stroke:{WALK_LIT_STROKE},stroke-width:3px",
    WalkState.dry: f"stroke:{WALK_QUIET_STROKE},stroke-width:1px",
    WalkState.aside: (f"fill:{WALK_ASIDE_FILL},stroke:{WALK_QUIET_STROKE},"
                      f"color:{WALK_ASIDE_INK},stroke-width:1px"),
}


def read_walk_state(on_walk: bool, rows_behind: int) -> WalkState:
    if not on_walk:
        return WalkState.aside
    return WalkState.lit if rows_behind else WalkState.dry


def build_walk_overlay(
    states: dict[StageId, WalkState],
    rows_behind: dict[StageId, int],
    edges: dict[tuple[StageId, StageId], int | None],
) -> DiagramOverlay:
    return DiagramOverlay(
        notes={sid: _say_rows(state, rows_behind[sid]) for sid, state in states.items()},
        styles={sid: _WALK_STYLE[state] for sid, state in states.items()},
        unclickable={sid for sid, state in states.items() if state is WalkState.aside},
        edge_labels={pair: _say_edge_rows(rows)
                     for pair, rows in edges.items() if rows is not None},
    )


def _say_rows(state: WalkState, rows_behind: int) -> str:
    if state is WalkState.aside:
        return "not on the walk"
    return f"{rows_behind:,} row{'' if rows_behind == 1 else 's'} behind"


def _say_edge_rows(rows: int) -> str:
    return f"{rows:,}"
