from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope
from app.models.citations import CitedValue
from app.models.claims import StageOutputRowCitation


class StageCitations(PersistedModel):
    """What one report stage cited, in the order it said so."""

    collection: ClassVar[str] = "run_citations"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    citations: list[CitedValue] = []
    cited_rows: list[StageOutputRowCitation] = []

    @staticmethod
    def compose_id(project_id: str, run_id: str, stage_id: str) -> str:
        return f"{project_id}/{run_id}/{stage_id}"
