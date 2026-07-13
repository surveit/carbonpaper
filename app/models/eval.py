"""Eval contract, as Pydantic models. Constructing a model validates it.

An eval measures the *real* workflow, not a copy of it. The v1 shape
(fan-out / fan-in are out of scope — those evals come later):

  - An **EvalConfig** is the authored spec. What defines it is the checks: each
    names a `target_stage` output column to grade (`ExpectedOutput.output_column`).
    An optional row-aligned eval-dataset `table` supplies the data for those
    checks; it's the data, not the definition, so a config can exist with no
    `table` yet (attach it later). When a table is present, its columns are
    exactly `override_stage`'s output columns (injected as that stage's whole
    output) plus one expected-output column per check, named after the check's
    target column (disambiguated on a name conflict — see
    `app.evals.dataset_columns`).
    (Not yet built: the scorer that runs this table through the workflow and
    grades it is expected to align each target output row back to the eval-
    dataset row that produced it by row-level lineage — an id stamped on each
    injected eval-dataset row and carried through to the target — rather than
    by a shared data column or row position; that alignment is only
    well-defined when the override→target path preserves grain, no fan-out /
    fan-in.) The config also carries any `reference_overrides` (extra data a
    row needs loaded) and how to score (`expected_outputs` comparisons,
    rollup `metrics`, or a `code` scorer for the escape hatch).
  - A **StageOutputOverride** injects a whole table as some stage's output.
  - An **EvalRun** is the result at a specific workflow version: its computed
    `settings` (can it be scored automatically, and if not why), and the
    scorer's `metrics` / per-row result table.

Storage: eval objects live under their own `eval_config/` and `eval_run/` object
types (see [[contract_pydantic_and_storage]]).
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Iterable, Literal, Optional

from pydantic import AfterValidator, Field, field_validator, model_validator

from app.models.workflow import Workflow
from app.models.schema import _Base
from app.models.table import TableRef

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_slug(v: str) -> str:
    """Object ids land on disk as `<object_type>/<object_id>.data`, so keep them
    filesystem-safe: lowercase, digits, underscore or hyphen."""
    if not _SLUG_RE.match(v):
        raise ValueError(f"id {v!r} must be a slug (lowercase, digits, _ or -)")
    return v


SlugId = Annotated[str, AfterValidator(_validate_slug)]


# ── Overrides ────────────────────────────────────────────────────────────────
class StageOutputOverride(_Base):
    """Inject `table` AS `stage_id`'s output, cutting that stage and everything
    upstream of it out of the run. `stage_id` may be ANY stage, not just an input
    — reference data, a fixture, whatever the eval needs held fixed."""
    stage_id: str
    table: TableRef


# ── The comparison ───────────────────────────────────────────────────────────
class ExpectedOutput(_Base):
    """One check: which `target_stage` output column to grade, and how. The
    eval-dataset file's expected-output column for this check is not authored
    here — it is named after `output_column` (the same name), unless
    `output_column` conflicts with one of the override stage's own output
    column names, in which case it is disambiguated (see
    `app.evals.dataset_columns`)."""
    output_column: str
    metric: Literal["exact", "abs_tol", "sign"] = "exact"
    tolerance: Optional[float] = None

    @model_validator(mode="after")
    def _tolerance_when_needed(self) -> "ExpectedOutput":
        if self.metric == "abs_tol" and self.tolerance is None:
            raise ValueError("metric=abs_tol needs a `tolerance`")
        return self


class CodeScorer(_Base):
    """Escape hatch: `function(actual_df, dataset_df) -> dict[str, Any]` of metrics.
    Needed when the path isn't grain-preserving (so declarative comparison can't
    align rows) or the comparison isn't column-by-column."""
    module: str
    function: str


# ── The eval config ──────────────────────────────────────────────────────────
class EvalConfig(_Base):
    """The authored eval: defined by its checks, plus how they plug into the
    workflow's stages and how they're scored.

    An optional eval-dataset `table` supplies the rows the checks run against:
    its columns are `override_stage`'s output columns (injected as that
    stage's whole output) plus one expected-output column per check, named
    after the check's target column (`ExpectedOutput.output_column`). Each
    check compares that expected-output column to the matching `target_stage`
    output column. Row alignment between the injected eval-dataset rows and
    the target's output is only well-defined when the override→target path is
    grain-preserving (see `resolve_eval_run_settings`). `reference_overrides`
    inject extra data at other stages; `code` overrides the per-column
    comparison when declarative scoring can't apply.
    """
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
    """How a given run will be scored, derived from the override→target path.

    `frontier` is the set of stages that actually execute to produce the target
    given the overrides (the target plus its non-overridden ancestors, not
    traversing above an override — its output is injected). `can_score_declaratively`
    is true iff every stage on the frontier preserves grain; otherwise the listed
    `blocking_stages` fan out, fan in, or reshape, and the run needs a code scorer.
    """
    can_score_declaratively: bool
    frontier: list[str]
    blocking_stages: list[str]


def resolve_eval_run_settings(
    workflow: Workflow,
    overrides: Iterable[str],
    target: str,
) -> EvalRunSettings:
    """Walk the executed frontier from `target` upward, stopping at overrides, and
    decide whether it can be scored automatically row-by-row (every frontier stage
    grain-preserving) — the v1 condition for a single-table, row-aligned eval.

    Whether an eval needs a code scorer is a property of the *path*, not the
    author's preference — this function is where that's decided. It raises
    (loudly) if `target` or any override names no stage, or if `target` is itself
    overridden — a misconfigured eval should fail at definition, not at score time.
    """
    by_id = {s.id: s for s in workflow.stages}
    if target not in by_id:
        raise ValueError(f"target {target!r} is not a stage in the workflow")
    ov = set(overrides)
    missing = ov - by_id.keys()
    if missing:
        raise ValueError(f"override(s) reference no stage: {sorted(missing)}")
    if target in ov:
        raise ValueError(f"target {target!r} cannot also be an override")

    frontier: list[str] = []
    seen: set[str] = set()
    stack = [target]
    while stack:
        node = stack.pop()
        if node in seen or node in ov:
            continue  # an overridden node is injected, not executed — and we
            # don't traverse above it; its upstream doesn't run either.
        seen.add(node)
        frontier.append(node)
        for upstream in by_id[node].input_ids:
            if upstream not in seen and upstream not in ov:
                stack.append(upstream)

    blocking = sorted(n for n in frontier if not by_id[n].is_grain_and_order_preserving)
    return EvalRunSettings(can_score_declaratively=not blocking,
                           frontier=sorted(frontier), blocking_stages=blocking)


# ── The run result ───────────────────────────────────────────────────────────
class EvalRun(_Base):
    """Result of running an EvalConfig against one workflow version."""
    id: SlugId
    config: str
    project: str
    # Which workflow version was scored — the stale tripwire. If the target's key or
    # domain moved since the config was authored, it's stale; don't re-score.
    workflow_version: str
    status: Literal["scored", "vetoed", "error"]
    # How this run was scored (from resolve_eval_run_settings). `vetoed` = it
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


__all__ = [
    "StageOutputOverride", "ExpectedOutput", "CodeScorer", "EvalConfig",
    "EvalRunSettings", "resolve_eval_run_settings", "EvalRun",
]
