"""Which columns of a stage's output table are drawn first. Presentation only:
the frame on disk, the row values and the CSV download keep the order the stage
wrote them in — this reorders the header list a table is rendered from."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from app.models import WorkflowStage
from app.models.stages.signature import ExtendsSignature, list_written_column_names
from app.web.diff_state import ColumnDiffState


class DrawnColumn(BaseModel):
    name: str
    # None where no pinned version resolves: nothing says what the stage did here.
    state: ColumnDiffState | None = None


def describe_frame_columns(
    workflow_stage: WorkflowStage | None, names: Sequence[str]
) -> list[DrawnColumn]:
    """What the stage wrote, then what it read, then what it carried untouched."""
    plain = [str(name) for name in names]
    if workflow_stage is None:
        return [DrawnColumn(name=name) for name in plain]
    states = _tell_what_the_stage_did(workflow_stage, plain)
    return [DrawnColumn(name=name, state=states[name])
            for name in sorted(plain, key=lambda name: _DRAWN_FIRST.index(states[name]))]


def order_written_columns_first(
    workflow_stage: WorkflowStage | None, names: Sequence[str]
) -> list[str]:
    if workflow_stage is None:
        # No resolvable pinned version: nothing declares what this stage wrote,
        # so the frame's own order stands rather than an invented one.
        return list(names)
    carried = set(names)
    written = [
        name for name in list_written_column_names(workflow_stage.stage) if name in carried
    ]
    promoted = set(written)
    return written + [name for name in names if name not in promoted]


def order_preview_columns(
    table: dict[str, Any] | None, workflow_stage: WorkflowStage | None
) -> dict[str, Any] | None:
    """Takes the preview/table bundles app.web.loading builds; passes a failed load through."""
    if table is None or not table.get("columns"):
        return table
    return {
        **table,
        "columns": order_written_columns_first(
            workflow_stage, [str(name) for name in table["columns"]]
        ),
    }


_DRAWN_FIRST = [
    ColumnDiffState.added,
    ColumnDiffState.rewritten,
    ColumnDiffState.read,
    ColumnDiffState.carried,
]


def _tell_what_the_stage_did(
    workflow_stage: WorkflowStage, names: Sequence[str]
) -> dict[str, ColumnDiffState]:
    signature = workflow_stage.stage.signature
    if not isinstance(signature, ExtendsSignature):
        # Nothing flows through a replaces form: the whole frame is its own work.
        return {name: ColumnDiffState.added for name in names}
    rewritten = {column.name for column in signature.rewrites}
    added = {column.name for column in signature.adds}
    read = {column.name for entry in signature.reads for column in entry.columns}
    return {name: _state_of(name, rewritten, added, read) for name in names}


def _state_of(
    name: str, rewritten: set[str], added: set[str], read: set[str]
) -> ColumnDiffState:
    if name in rewritten:
        return ColumnDiffState.rewritten
    if name in added:
        return ColumnDiffState.added
    if name in read:
        return ColumnDiffState.read
    return ColumnDiffState.carried
