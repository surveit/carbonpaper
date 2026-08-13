"""Which columns of a stage's output table are drawn first. Presentation only:
the frame on disk, the row values and the CSV download keep the order the stage
wrote them in — this reorders the header list a table is rendered from."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.models import WorkflowStage
from app.models.stages.signature import list_written_column_names


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
