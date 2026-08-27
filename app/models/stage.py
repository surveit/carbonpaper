"""`Stage` — the discriminated union of the per-type stage models — plus
`StageDraft`, the flat permissive shape an authoring client submits.

Parse a stage dict with `parse_stage`; `Stage` itself is a type annotation, not
a class. The per-type models live in `app/models/stages/`.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional, Union, get_args

from pydantic import (
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from app.models.stages.stage_base import (  # noqa: F401  (re-exported: the stage vocabulary lives here)
    ReviewConfig,
    AbstractStage,
    AuthoredStageFields,
    StageInput,
    StageType,
    is_grain_and_order_preserving,
)
from app.models.schema import _Base
from app.models.stages.aggregate import AggregateConfig, AggregateStage
from app.models.stages.code import (
    PythonFrameFunctionStage,
    PythonFunction,
    PythonRowFunctionStage,
)
from app.models.stages.dedupe import DedupeConfig, DedupeStage
from app.models.stages.explode import ExplodeConfig, ExplodeStage
from app.models.stages.filter_rows import FilterConfig, FilterRowsStage
from app.models.stages.human_review_queue import HumanReviewQueueStage, QueueConfig
from app.models.stages.input_data import Connector, InputDataStage
from app.models.stages.join import EnrichStage, ExpandStage, JoinConfig
from app.models.stages.llm_transform import LLMConfig, LLMTransformStage
from app.models.stages.report import ReportConfig, ReportStage
from app.models.stages.signature import (  # noqa: F401  (re-exported: the stage vocabulary lives here)
    ExtendsSignature,
    InputReads,
    ReplacesSignature,
    TransformSignature,
)
from app.models.stages.sort_rank import SortRankConfig, SortRankStage
from app.models.stages.starlark import StarlarkFunction, StarlarkRowFunctionStage
from app.models.stages.starlark_filter import StarlarkFilter, StarlarkFilterRowsStage
from app.models.stages.union import UnionConfig, UnionStage
from app.core.utils import format_errors
from app.models.tool_schema_prompts import STAGE_DRAFT_DESCRIPTION, STAGE_EDIT_DESCRIPTION


# ── Stage ────────────────────────────────────────────────────────────────────
# `type` selects the model: each member declares the config blocks its type
# requires, so a missing block is a structured pydantic error against that
# member rather than a hand-written cross-field check.
Stage = Annotated[
    Union[
        InputDataStage,
        LLMTransformStage,
        PythonRowFunctionStage,
        PythonFrameFunctionStage,
        EnrichStage,
        ExpandStage,
        AggregateStage,
        HumanReviewQueueStage,
        ReportStage,
        UnionStage,
        FilterRowsStage,
        StarlarkRowFunctionStage,
        StarlarkFilterRowsStage,
        ExplodeStage,
        DedupeStage,
        SortRankStage,
    ],
    Field(discriminator="type"),
]

_STAGE_ADAPTER: TypeAdapter[Stage] = TypeAdapter(Stage)

_MODEL_BY_TYPE: dict[StageType, type[AbstractStage]] = {
    get_args(member.model_fields["type"].annotation)[0]: member
    for member in get_args(get_args(Stage)[0])
}


def find_cache_ignored_reason(stage_type: StageType) -> str | None:
    """Why this type ignores `cache`, in its own words. None where it honours one."""
    return _MODEL_BY_TYPE[stage_type].CACHE_IGNORED_BECAUSE


def max_declared_inputs(stage_type: StageType) -> int | None:
    """The `inputs` cap this type's model enforces; None where it takes any number."""
    caps = [
        cap
        for constraint in _MODEL_BY_TYPE[stage_type].model_fields["inputs"].metadata
        if (cap := getattr(constraint, "max_length", None)) is not None
    ]
    return caps[0] if caps else None


def parse_stage(spec: Any) -> Stage:
    return _STAGE_ADAPTER.validate_python(spec)


def validate_stage(spec: Any) -> list[str]:
    try:
        parse_stage(spec)
        return []
    except ValidationError as err:
        return format_errors(err)


# docs/models-and-storage.md
STAGE_SPEC_SCHEMA_VERSION = 8


def stage_to_spec_dict(stage: Stage) -> dict[str, Any]:
    return stage.model_dump(mode="json", by_alias=True, exclude_none=True)


def stage_to_json(stage: Stage) -> str:
    return stage.model_dump_json(indent=2, by_alias=True, exclude_none=True)


# ── StageDraft ───────────────────────────────────────────────────────────────
# Stage fields an authoring client never writes, so StageDraft does not declare
# them. A client that echoes back a stage it read from the server carries them
# anyway; trimming those is a tool-boundary accommodation, and lives there
# (`SubmittedStage`, app/tools/submitted_stage.py).
SERVER_OWNED_STAGE_FIELDS = ("tests", "eval", "review", "source")


# Add no cross-field validator: an invalid stage must parse here and be refused later.
# (Above the class deliberately — a docstring here would be copied into the tool schema
# and read by the authoring agent. See tests/arch/test_tool_schema_models_carry_no_docstring.py.)
class StageDraft(AuthoredStageFields):
    model_config = ConfigDict(json_schema_extra={"description": STAGE_DRAFT_DESCRIPTION})

    connector: Optional[Connector] = None
    llm: Optional[LLMConfig] = None
    function: Optional[PythonFunction] = None
    join: Optional[JoinConfig] = None
    aggregate: Optional[AggregateConfig] = None
    queue: Optional[QueueConfig] = None
    report: Optional[ReportConfig] = None
    union: Optional[UnionConfig] = None
    filter: Optional[FilterConfig] = None
    starlark: Optional[StarlarkFunction] = None
    starlark_filter: Optional[StarlarkFilter] = None
    explode: Optional[ExplodeConfig] = None
    dedupe: Optional[DedupeConfig] = None
    sort_rank: Optional[SortRankConfig] = None

    def to_stage_spec(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True, by_alias=True)


class StageEdit(_Base):
    model_config = ConfigDict(json_schema_extra={"description": STAGE_EDIT_DESCRIPTION})

    stage_id: str = Field(description="The id of the stage to change.")
    changes_json: str = Field(
        description="A JSON Merge Patch (as a string) of ONLY this stage's changed fields.",
    )
