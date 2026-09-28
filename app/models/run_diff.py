"""Two runs of one workflow version, compared stage by stage."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.ids import ID
from app.core.json_types import JsonDict
from app.core.run_status import StageStatus
from app.models.branch_analysis import RowOrdinal
from app.models.schema import StageId
from app.models.stages.stage_base import StageType

# Their rows are a model's or a reviewer's judgments, which two runs may make differently.
UNCOMPARED_STAGE_TYPES = frozenset({StageType.llm_transform, StageType.human_review_queue})


class RowDifference(BaseModel):
    """A row is None where that run's output ends before this position."""

    ordinal: RowOrdinal
    run_a_row: JsonDict | None
    run_b_row: JsonDict | None
    # In output column order; every shared column where one row is None.
    differing_columns: list[str]


class OutputComparison(BaseModel):
    run_a_row_count: int
    run_b_row_count: int
    columns_only_in_run_a: list[str]
    columns_only_in_run_b: list[str]
    # Positions whose rows differ on the shared columns, a position only one run reaches included.
    differing_row_count: int
    first_differing_rows: list[RowDifference]

    @property
    def is_identical(self) -> bool:
        return not (
            self.columns_only_in_run_a or self.columns_only_in_run_b or self.differing_row_count
        )


class StageComparison(BaseModel):
    stage_id: StageId
    type: StageType
    run_a_status: StageStatus
    run_b_status: StageStatus
    # None for an UNCOMPARED_STAGE_TYPES stage, and for one that did not finish in both runs.
    output_comparison: OutputComparison | None


class RunComparison(BaseModel):
    run_a_id: ID
    run_b_id: ID
    version_id: ID
    stages: list[StageComparison]
