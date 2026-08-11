"""Eval contract, as Pydantic models. Constructing a model validates it.
An eval measures the *real* workflow, not a copy; fan-out / fan-in are out of scope.
An EvalConfig can exist with no `table` yet; when a table is present its columns are
exactly `override_stage`'s output columns plus one expected-output column per check
(disambiguated on name conflict — see `app.evals.dataset_columns`).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, Field, field_validator, model_validator

from app.models.schema import _Base
from app.models.table import TableRef

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_slug(v: str) -> str:
    if not _SLUG_RE.match(v):
        raise ValueError(f"id {v!r} must be a slug (lowercase, digits, _ or -)")
    return v


SlugId = Annotated[str, AfterValidator(_validate_slug)]


# ── Overrides ────────────────────────────────────────────────────────────────
class StageOutputOverride(_Base):
    """Injecting `table` cuts `stage_id` and everything upstream of it out of the run."""
    stage_id: str
    table: TableRef


class ScoringMetric(str, Enum):
    exact = "exact"
    abs_tol = "abs_tol"
    sign = "sign"


# ── The comparison ───────────────────────────────────────────────────────────
class ExpectedOutput(_Base):
    output_column: str
    metric: ScoringMetric = ScoringMetric.exact
    tolerance: Optional[float] = None

    @model_validator(mode="after")
    def _tolerance_when_needed(self) -> "ExpectedOutput":
        if self.metric == ScoringMetric.abs_tol and self.tolerance is None:
            raise ValueError("metric=abs_tol needs a `tolerance`")
        return self


class CodeScorer(_Base):
    """The named function must be `function(actual_df, dataset_df) -> dict[str, Any]` of metrics."""
    module: str
    function: str


# ── The eval config ──────────────────────────────────────────────────────────
class EvalConfig(_Base):
    id: SlugId
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


# ── Scorability (computed per run) ───────────────────────────────────────────
class EvalRunSettings(_Base):
    """`frontier`: the target plus its non-overridden ancestors — the walk stops at an override."""
    can_score_declaratively: bool
    frontier: list[str]
    blocking_stages: list[str]


# ── The run result ───────────────────────────────────────────────────────────
class EvalRun(_Base):
    id: SlugId
    config: str
    project: str
    # Which workflow version was scored — the stale tripwire. If the target's key or
    # domain moved since the config was authored, it's stale; don't re-score.
    workflow_version: str
    status: Literal["scored", "vetoed", "error"]
    # How this run was scored (from app.evals.run_settings). `vetoed` = it
    # couldn't be scored declaratively and no code scorer was supplied.
    settings: EvalRunSettings
    # Score outputs — the scorer writes rollup metrics and a per-row result
    # table at `result_ref`. There is no overall pass/fail: an eval-dataset row
    # passes iff all its checks match, and whether the eval looks good is a human
    # review judgment, not a stored bool.
    metrics: dict[str, Any] = Field(default_factory=dict)
    result_ref: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    notes: list[str] = Field(default_factory=list)
