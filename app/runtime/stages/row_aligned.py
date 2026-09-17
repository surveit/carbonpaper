"""The handler shape for a frame transform that leaves every base row where it stood."""
from __future__ import annotations

import pyarrow as pa

from app.models import WorkflowStage

from ..context import RunContext
from ..stage_output import StageOutput
from .execution import FrameTransformHandler


class RowAlignedFrameHandler(FrameTransformHandler):
    """Whole frames in, like its base — and output row i is base input row i, on every run."""

    def execute(
        self, workflow_stage: WorkflowStage, inputs: dict[str, pa.Table],
        ctx: RunContext,
    ) -> StageOutput | None:
        output = self.apply(workflow_stage, inputs, ctx)
        _refuse_a_row_out_of_its_base_place(workflow_stage, inputs, output)
        return output

    @property
    def preserves_grain_and_order(self) -> bool:
        return True


def _refuse_a_row_out_of_its_base_place(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], output: StageOutput | None
) -> None:
    """Held to the promise every run: a fan-out or a reorder is caught where it happens."""
    base_id = workflow_stage.inputs[0].id
    in_place = list(range(inputs[base_id].num_rows))
    recorded = _read_base_ordinals(base_id, output)
    if recorded != in_place:
        raise RuntimeError(
            f"stage {workflow_stage.stage.id}: this shape promises output row i is "
            f"{base_id!r} row i, and the run recorded {recorded[:8]} for "
            f"{len(in_place)} base row(s)"
        )


def _read_base_ordinals(base_id: str, output: StageOutput | None) -> list[int | None]:
    if output is None or output.lineage is None:
        return []
    return output.lineage.read_parent_ordinals(base_id)
