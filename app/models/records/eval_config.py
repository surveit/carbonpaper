from __future__ import annotations

from typing import ClassVar, Optional

from pydantic import Field, field_validator, model_validator

from app.core.record import PersistedModel, PersistenceScope
from app.models.eval import CodeScorer, ExpectedOutput, SlugId, StageOutputOverride
from app.models.table import TableRef


class EvalConfig(PersistedModel):
    """`id` is the composite `{project}/{eval_id}`; `eval_id` is the local id callers pass."""

    collection: ClassVar[str] = "eval"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    DUMP_OPTS: ClassVar[dict] = {"exclude_none": True}

    eval_id: SlugId
    project: str
    name: str
    description: Optional[str] = None
    # data + wiring
    override_stage: str
    target_stage: str
    table: Optional[TableRef] = None
    expected_outputs: list[ExpectedOutput]
    # context + scoring
    reference_overrides: list[StageOutputOverride] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    code: Optional[CodeScorer] = None

    @staticmethod
    def compose_id(project_id: str, eval_id: str) -> str:
        return f"{project_id}/{eval_id}"

    @field_validator("expected_outputs")
    @classmethod
    def _nonempty(cls, v: list) -> list:
        if not v:
            raise ValueError("must be non-empty")
        return v

    @model_validator(mode="after")
    def _distinct_stages(self) -> "EvalConfig":
        if self.override_stage == self.target_stage:
            raise ValueError("override_stage and target_stage must differ")
        return self

    @model_validator(mode="after")
    def _unique_reference_stages(self) -> "EvalConfig":
        seen: set[str] = set()
        for ov in self.reference_overrides:
            if ov.stage_id in seen:
                raise ValueError(f"duplicate reference_override for stage {ov.stage_id!r}")
            seen.add(ov.stage_id)
        return self
