"""Eval contract, as Pydantic models. Constructing a model validates it.
An eval measures the *real* workflow, not a copy; fan-out / fan-in are out of scope.
An EvalConfig can exist with no `table` yet; when a table is present its columns are
exactly `override_stage`'s output columns plus one expected-output column per check
(disambiguated on name conflict — see `app.evals.dataset_columns`).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Optional

from pydantic import AfterValidator, model_validator

from app.models.schema import _Base
from app.models.table import TableRef
from app.core.ids import ID

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_slug(v: str) -> str:
    if not _SLUG_RE.match(v):
        raise ValueError(f"id {v!r} must be a slug (lowercase, digits, _ or -)")
    return v


SlugId = Annotated[str, AfterValidator(_validate_slug)]


# ── Overrides ────────────────────────────────────────────────────────────────
class StageOutputOverride(_Base):
    """Injecting `table` cuts `stage_id` and everything upstream of it out of the run."""
    stage_id: ID
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



# ── Scorability (computed per run) ───────────────────────────────────────────
class EvalRunSettings(_Base):
    """`frontier`: the target plus its non-overridden ancestors — the walk stops at an override."""
    can_score_declaratively: bool
    frontier: list[str]
    blocking_stages: list[str]

