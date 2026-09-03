"""Which section of _stage_executable.html shows a stage, keyed by stage type."""

from __future__ import annotations

from typing import assert_never

from app.models.stages.stage_base import AbstractStage, StageType


def name_transform_block(stage: AbstractStage) -> str:
    match stage.type:
        case StageType.input_data:
            return "connector"
        case StageType.llm_transform:
            return "llm"
        case StageType.union:
            return "union"
        case StageType.enrich | StageType.expand:
            return "join"
        case StageType.aggregate:
            return "aggregate"
        case StageType.human_review_queue:
            return "queue"
        case StageType.report:
            return "report"
        case StageType.dedupe:
            return "dedupe"
        case StageType.explode:
            return "explode"
        case StageType.sort_rank:
            return "sort_rank"
        case (StageType.python_row_function | StageType.python_frame_function
              | StageType.starlark_row_function | StageType.starlark_filter_rows
              | StageType.filter_rows | StageType.starlark_report):
            # No config section: the block itself draws these (_stage_code.html).
            return "code"
    assert_never(stage.type)
