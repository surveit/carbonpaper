"""Which columns of a stage's output table are drawn first. Presentation only:
the frame on disk, the row values and the CSV download keep the order the stage
wrote them in — this reorders the header list a table is rendered from."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any, Protocol, TypeVar

from app.models import WorkflowStage
from app.models.stages.signature import (
    list_read_column_names,
    list_rewritten_column_names,
    list_written_column_names,
)
from app.web.diff_state import ColumnDiffState


class ColumnGroup(str, Enum):
    """What a stage did to a column. Named by the colour key the tables carry."""

    read = "read"
    changed = "changed"
    added = "added"
    dropped = "dropped"
    untouched = "untouched"


# What the stage worked FROM leads, then what it did, then the passengers.
_DRAWN_IN_ORDER = [ColumnGroup.read, ColumnGroup.changed, ColumnGroup.added,
                   ColumnGroup.dropped, ColumnGroup.untouched]


class HasColumnGroup(Protocol):
    @property
    def group(self) -> ColumnGroup: ...


Column = TypeVar("Column", bound=HasColumnGroup)


def find_column_group(state: str, *, read: bool, changed: bool) -> ColumnGroup:
    """`state` is a ColumnDiffState or a CellDiffState; both name the same three."""
    if state == ColumnGroup.dropped:
        return ColumnGroup.dropped
    if state == ColumnGroup.added:
        return ColumnGroup.added
    if changed:
        return ColumnGroup.changed
    return ColumnGroup.read if read else ColumnGroup.untouched


def order_columns_by_group(columns: Sequence[Column]) -> list[Column]:
    """A stable sort, so inside one group the frame's own order stands."""
    return sorted(columns, key=lambda column: _DRAWN_IN_ORDER.index(column.group))


def order_columns_by_signature(
    workflow_stage: WorkflowStage | None, names: Sequence[str]
) -> list[str]:
    """The same order as a diff table, for a view with no input frame to compare."""
    if workflow_stage is None:
        # No resolvable pinned version: nothing declares what this stage wrote,
        # so the frame's own order stands rather than an invented one.
        return list(names)
    read = list_read_column_names(workflow_stage.stage)
    rewritten = list_rewritten_column_names(workflow_stage.stage)
    introduced = set(list_written_column_names(workflow_stage.stage)) - rewritten

    def group_of(name: str) -> ColumnGroup:
        state = ColumnDiffState.added if name in introduced else ColumnDiffState.carried
        return find_column_group(state.value, read=name in read,
                                 changed=name in rewritten)

    return sorted(names, key=lambda name: _DRAWN_IN_ORDER.index(group_of(name)))


def order_preview_columns(
    table: dict[str, Any] | None, workflow_stage: WorkflowStage | None
) -> dict[str, Any] | None:
    """Takes the preview/table bundles app.web.loading builds; passes a failed load through."""
    if table is None or not table.get("columns"):
        return table
    return {
        **table,
        "columns": order_columns_by_signature(
            workflow_stage, [str(name) for name in table["columns"]]
        ),
    }
