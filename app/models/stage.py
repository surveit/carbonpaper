"""`Stage` — the discriminated union of the per-type stage models — plus
`StageDraft`, the flat permissive shape an authoring client submits.

Parse a stage dict with `parse_stage`; `Stage` itself is a type annotation, not
a class. The per-type models live in `app/models/stages/`.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional, Union

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from app.models.stages.stage_base import (  # noqa: F401  (re-exported: the stage vocabulary lives here)
    ReviewConfig,
    StageBase,
    StageCommon,
    StageInput,
    StageType,
    is_grain_and_order_preserving,
)
from app.models.stages.aggregate import AggregateConfig, AggregateStage
from app.models.stages.code import (
    PythonFrameFunctionStage,
    PythonFunction,
    PythonRowFunctionStage,
)
from app.models.stages.filter_rows import FilterConfig, FilterRowsStage
from app.models.stages.human_review_queue import HumanReviewQueueStage, QueueConfig
from app.models.stages.input_data import Connector, InputDataStage
from app.models.stages.join import EnrichStage, ExpandStage, JoinConfig
from app.models.stages.llm_transform import LLMConfig, LLMTransformStage
from app.models.stages.publish import PublishConfig, PublishStage
from app.models.stages.signature import (  # noqa: F401  (re-exported: the stage vocabulary lives here)
    ExtendsSignature,
    InputReads,
    ReplacesSignature,
    TransformSignature,
)
from app.models.stages.sort_rows import SortConfig, SortRowsStage
from app.models.stages.starlark import StarlarkFunction, StarlarkRowFunctionStage
from app.models.stages.union import UnionConfig, UnionStage
from app.core.utils import format_errors


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
        PublishStage,
        UnionStage,
        FilterRowsStage,
        SortRowsStage,
        StarlarkRowFunctionStage,
    ],
    Field(discriminator="type"),
]

_STAGE_ADAPTER: TypeAdapter[Stage] = TypeAdapter(Stage)


def parse_stage(spec: Any) -> Stage:
    """One stage dict as the per-type model its `type` selects. Raises
    ValidationError — this is where a stored stage's rules are enforced."""
    return _STAGE_ADAPTER.validate_python(spec)


def validate_stage(spec: Any) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        parse_stage(spec)
        return []
    except ValidationError as err:
        return format_errors(err)


# The version of the shape below: what a record embedding stages stamps into its
# schema_version column, and what an alembic revision rewrites a payload up to.
# v2: primary_key left the stage vocabulary (the data model keeps its own).
# v3: `name` became `description` — a stage has one name, its id.
STAGE_SPEC_SCHEMA_VERSION = 3


def stage_to_spec_dict(stage: Stage) -> dict[str, Any]:
    """Aliases restored (`schema`, not `table_schema`); None-valued keys dropped."""
    return stage.model_dump(mode="json", by_alias=True, exclude_none=True)


def stage_to_json(stage: Stage) -> str:
    return stage.model_dump_json(indent=2, by_alias=True, exclude_none=True)


# ── StageDraft ───────────────────────────────────────────────────────────────
# Stage fields an authoring client never writes, so StageDraft does not declare
# them. A client that echoes back a stage it read from the server carries them
# anyway; the draft drops those rather than refusing the whole stage.
SERVER_OWNED_STAGE_FIELDS = ("tests", "eval", "review", "source")


class StageDraft(StageCommon):
    """One stage as an authoring client submits it: every config block optional
    and no cross-field validator. A stage that breaks a rule must parse here and
    be refused by `parse_stage` in the handler, where the refusal reaches the
    client on the handler's own channel rather than as a parameter-binding
    error. Shares `StageCommon` with the stored models, so the fields both carry
    are declared once."""
    connector: Optional[Connector] = None
    llm: Optional[LLMConfig] = None
    function: Optional[PythonFunction] = None
    join: Optional[JoinConfig] = None
    aggregate: Optional[AggregateConfig] = None
    queue: Optional[QueueConfig] = None
    publish: Optional[PublishConfig] = None
    union: Optional[UnionConfig] = None
    filter: Optional[FilterConfig] = None
    sort: Optional[SortConfig] = None
    starlark: Optional[StarlarkFunction] = None

    # Which SERVER_OWNED_STAGE_FIELDS the submitted draft carried, for the caller
    # to warn about. Bookkeeping about one submission, not part of a stage: kept
    # out of the JSON schema a client is handed and out of every dump.
    dropped_server_owned_fields: SkipJsonSchema[list[str]] = Field(
        default_factory=list, exclude=True
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_server_owned_fields(cls, data: Any) -> Any:
        """Accept and discard the fields only the server writes, so a client can
        echo back a stage it read without tripping `extra="forbid"`. Cannot
        raise."""
        if not isinstance(data, dict):
            return data
        present = [name for name in SERVER_OWNED_STAGE_FIELDS if name in data]
        remaining = {k: v for k, v in data.items() if k not in SERVER_OWNED_STAGE_FIELDS}
        remaining["dropped_server_owned_fields"] = present
        return remaining

    def to_stage_spec(self) -> dict[str, Any]:
        """This draft as a dict `parse_stage` accepts — by alias, so
        `StageInput.table_schema` spells itself `schema:` the way a compiled stage
        does."""
        return self.model_dump(exclude_unset=True, by_alias=True)
