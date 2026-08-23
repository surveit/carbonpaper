from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from app.core.record import PersistedModel, PersistenceScope
from app.models.review_guide import ReviewGuideStep


class ReviewGuide(PersistedModel):
    collection: ClassVar[str] = "review_guide"
    # Written by save_version_guide alone; a run may read it, never write one.
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # Found on these backpointers, never a composed id. Writing appends: the newest is live.
    project: str
    version_id: str
    # Prose only: a stage's name, type and columns are read off the version at render.
    steps: list[ReviewGuideStep]
    unnarrated: list[str] = Field(default_factory=list)

    def collect_step_stage_ids(self) -> list[str]:
        return [stage_id for step in self.steps for stage_id in step.stage_ids]
