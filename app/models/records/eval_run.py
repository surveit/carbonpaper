from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from pydantic import Field

from app.core.record import PersistedModel, PersistenceScope
from app.models.eval import EvalRunSettings, SlugId


class EvalRun(PersistedModel):
    """`id` is the composite `{project}/{run_id}`; `run_id` is the local id callers pass."""

    collection: ClassVar[str] = "eval_run"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    run_id: SlugId
    config: str
    project: str
    # The stale tripwire: a target whose key or domain moved since means don't re-score.
    workflow_version: str
    # The only non-final state; it learns nothing until the scorer replaces it.
    status: Literal["running", "scored", "vetoed", "error"]
    # From app.evals.run_settings. `vetoed` = not scorable declaratively, no code scorer.
    settings: EvalRunSettings
    # Rollup metrics; the per-row table lands at `result_ref`. No overall pass/fail.
    metrics: dict[str, Any] = Field(default_factory=dict)
    result_ref: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    notes: list[str] = Field(default_factory=list)

    @staticmethod
    def compose_id(project_id: str, run_id: str) -> str:
        return f"{project_id}/{run_id}"

    def is_running(self) -> bool:
        return self.status == "running"
