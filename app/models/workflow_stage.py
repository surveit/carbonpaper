"""A stage seen in its workflow: the authored `Stage` plus the schemas that are a
function of the whole graph rather than of the stage alone. In memory only —
`app.models.workflow.Workflow` is the one thing that builds these, and nothing
parses or dumps them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.models.schema import StageId, TableSchema

if TYPE_CHECKING:
    # Runtime-free: app.models.stage reaches the per-type modules, which annotate
    # against this one.
    from app.models.stage import Stage


@dataclass(frozen=True)
class WorkflowStageInput:
    id: StageId
    table_schema: TableSchema


@dataclass(frozen=True)
class WorkflowStage:
    stage: "Stage"
    # In the stage's own `inputs` order, so inputs[0] is the anchor/subject.
    inputs: list[WorkflowStageInput]
    # None only for report, which emits files rather than a table.
    output_schema: Optional[TableSchema]

    @property
    def id(self) -> StageId:
        return self.stage.id
