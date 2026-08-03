"""The authored walkthrough of a workflow version: ordered steps, each narrating
the stages it names. Anything readable off the stages themselves (their names,
types, order, the columns they write) is deliberately absent — it is read off the
stages at render time instead of being frozen here.
"""
from __future__ import annotations

from pydantic import Field

from app.models.schema import _Base


class ReviewGuideStep(_Base):
    """One step of the walkthrough. `prose` may carry `backticked` column names."""

    title: str
    prose: str
    stage_ids: list[str]


class ReviewGuide(_Base):
    """`unnarrated` names the stages no step covers, so leaving one out is a decision."""

    steps: list[ReviewGuideStep]
    unnarrated: list[str] = Field(default_factory=list)

    def collect_step_stage_ids(self) -> list[str]:
        """Every stage id the steps name, in step order, repeats included."""
        return [stage_id for step in self.steps for stage_id in step.stage_ids]
