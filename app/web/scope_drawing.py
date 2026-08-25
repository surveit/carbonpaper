"""Where every bar and ribbon of the scope map sits. See docs/scope-map.md."""

from __future__ import annotations


from pydantic import BaseModel

from app.models.branch_analysis import BranchId, RowOrdinal
from app.models.schema import StageId
from app.web.scope_layout import (
    BAND_GAP,
    BAR,
    COLUMN,
    CUT_LINE,
    HEAD,
    Column,
    Facts,
    Node,
    Ribbon,
    choose_columns,
    count_removal_lines,
    gather_ribbons,
    measure_height,
    order_nodes,
    place_bars,
    stack_ribbons,
)
from app.web.scope_payload import DrawnStage, ScopeMap


class DrawnBar(BaseModel):
    """One bar: a set of rows a stage told apart from the rest, and where it sits."""

    key: str
    label: str
    tip: str
    rows: int
    # The rows it holds, so a click can list them.
    on: list[RowOrdinal]
    x: float
    y: float
    height: float
    label_y: float
    # Where the label sits, and the bar's middle when a leader has to reach it.
    text_x: float
    leader_at: float | None = None
    # Every branch on it was read off the run rather than written as an `if`.
    implied: bool
    is_figure: bool = False
    # Set where the column stands for a merge drawn as one node, never a group of it.
    alias_of: StageId | None = None


class DrawnRibbon(BaseModel):
    """One run of rows between two bars, as the two edges a curve is drawn through."""

    from_key: str
    into_key: str
    rows: int
    x0: float
    y0: float
    h0: float
    x1: float
    y1: float
    h1: float


class DrawnRemoval(BaseModel):
    """Rows a stage took out of the workflow, named on its header line."""

    branch: BranchId
    label: str
    tip: str
    line: int


class DrawnColumn(BaseModel):
    """A stage as it is drawn: its header lines, its bars, and what it cut."""

    stage: DrawnStage
    x: float
    bars: list[DrawnBar]
    removals: list[DrawnRemoval]
    head_label: str
    head_note: str
    head_tip: str
    scale_label: str = ""
    scale_tip: str = ""
    merge_label: str = ""
    merge_wants: bool = False
    bottom: float


class ScopeDrawing(BaseModel):
    """The whole drawing, in pixels. Nothing below this decides where anything goes."""

    width: float
    height: float
    top: float
    # The one measurement the browser needs, so no constant is written down twice.
    bar_width: float = BAR
    columns: list[DrawnColumn]
    ribbons: list[DrawnRibbon]


def draw_the_scope(scope: ScopeMap, every_stage: bool) -> ScopeDrawing:
    facts = Facts(scope)
    columns = choose_columns(facts, every_stage)
    if not columns:
        return ScopeDrawing(width=0, height=0, top=HEAD, columns=[], ribbons=[])
    top = HEAD + BAND_GAP + count_removal_lines(columns) * CUT_LINE
    ribbons = gather_ribbons(facts, columns)
    order_nodes(columns, ribbons)
    place_bars(facts, columns, top, bool(ribbons))
    stack_ribbons(ribbons)
    return ScopeDrawing(
        width=len(columns) * COLUMN, top=top,
        height=measure_height(columns),
        columns=[_render_column(facts, column) for column in columns],
        ribbons=[_render_ribbon(columns, ribbon) for ribbon in ribbons])


# ─── the words on it ─────────────────────────────────────────────────────────


def _render_column(facts: Facts, column: Column) -> DrawnColumn:
    room = COLUMN - 14
    step = next((s for s in facts.scope.scale
                 if s.stage == column.stage.id and s.rows_count), None)
    about = f"{column.stage.id} — {column.stage.description or column.stage.type}"
    return DrawnColumn(
        stage=column.stage, x=column.x, bottom=column.bottom,
        head_label=_clip(column.stage.id, room // 7),
        head_note=_clip(column.stage.description or column.stage.type, room // 5.6),
        head_tip=about,
        bars=[_render_bar(facts, column, node) for node in column.nodes],
        removals=[_render_removal(branch, rows, line, room)
                  for line, (branch, rows) in enumerate(column.gone)],
        scale_label="" if step is None else _clip(
            f"{step.included_rows_count:,} of {step.rows_count:,}"
            f"{' row at ' if step.rows_count == 1 else ' rows at '}"
            f"{column.stage.glyph} {column.stage.id}", room // 5.6),
        scale_tip="" if step is None else
        f"{column.stage.id} holds {step.rows_count:,} rows; this figure descends "
        f"from {step.included_rows_count:,}",
        merge_label=_say_the_other_reading(column),
        merge_wants=column.alias is not None)


def _render_bar(facts: Facts, column: Column, node: Node) -> DrawnBar:
    label = _label_of(facts, node)
    return DrawnBar(
        key=node.key, rows=node.rows, on=sorted(node.on),
        label=_clip(label, (COLUMN - BAR - 14) // 6.1),
        tip=_say_what_the_bar_holds(node, label),
        x=column.x, y=node.y, height=node.height, label_y=node.label_y,
        text_x=column.x + BAR + 12, leader_at=_reach_for_the_label(node),
        implied=not any(facts.is_code(b) for b in node.branches),
        is_figure=node.is_figure,
        alias_of=None if node.alias_of is None else node.alias_of.stage_id)


# A bar drawn thin sits far from the label naming it, so a line goes and fetches it.
def _reach_for_the_label(node: Node) -> float | None:
    middle = node.y + node.height / 2
    return middle if abs(node.label_y - middle) > 2 else None


def _label_of(facts: Facts, node: Node) -> str:
    if node.alias_of is not None:
        by = ", ".join(node.alias_of.group_by) or "the whole frame"
        return (f"{node.alias_of.on_route_groups_count:,} of "
                f"{node.alias_of.groups_count:,} groups, by {by}")
    if node.is_figure:
        cited = facts.scope.citation
        named = f"{cited.column} = {_say_the_figure(cited.value)}"
        # A cell of the frame it was read into was merged from nothing.
        if facts.scope.covers.at_stage == cited.stage_id and node.rows == 1:
            return named
        return f"{named}, merged from {node.rows:,}"
    labels = [facts.scope.branches[b].label for b in node.branches]
    return " + ".join(labels) if labels else "—"


def _say_what_the_bar_holds(node: Node, label: str) -> str:
    if node.alias_of is None:
        return f"{label} — {node.rows:,} row{'' if node.rows == 1 else 's'}"
    return (f"{node.alias_of.stage_id} grouped {node.alias_of.rows_count:,} rows into "
            f"{node.alias_of.groups_count:,}. These {node.rows:,} came through "
            f"{node.alias_of.on_route_groups_count:,} of them. Click to list the groups.")


# Its count is true and its width is not, so it is text and never a ribbon.
def _render_removal(branch: BranchId, rows: int, line: int,
                    room: float) -> DrawnRemoval:
    return DrawnRemoval(
        branch=branch, line=line,
        label=_clip(f"{rows:,} row{'' if rows == 1 else 's'} filtered here", room - 2),
        tip=f"{rows:,} row{' was' if rows == 1 else 's were'} dropped from the "
            f"workflow at this stage. Click to draw them in a new tab: they are a "
            f"different set of rows, so the page around this one stops fitting.")


def _say_the_other_reading(column: Column) -> str:
    if column.alias is not None:
        return f"split into {column.alias.on_route_groups_count:,} groups"
    if column.expanded:
        held = len(column.nodes)
        return f"fold {held:,} group{' back' if held == 1 else 's back'}"
    return ""


def _say_the_figure(value: object) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:,.2f}".rstrip("0").rstrip(".") if isinstance(value, float) \
        else f"{value:,}"


def _clip(text: str, room: float) -> str:
    keep = int(room)
    return text if len(text) <= keep else text[:max(0, keep - 1)] + "…"


def _render_ribbon(columns: list[Column], ribbon: Ribbon) -> DrawnRibbon:
    return DrawnRibbon(
        from_key=ribbon.a.key, into_key=ribbon.b.key, rows=ribbon.rows,
        x0=columns[ribbon.ci].x + BAR, y0=ribbon.y0, h0=ribbon.h0,
        x1=columns[ribbon.cj].x, y1=ribbon.y1, h1=ribbon.h1)
