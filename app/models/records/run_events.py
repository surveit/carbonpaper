from __future__ import annotations

from typing import ClassVar

from app.core.json_types import JsonDict
from app.core.record import PersistedModel, PersistenceScope


class RunEventChunk(PersistedModel):
    """One run's events `first_seq .. first_seq + len(events) - 1`."""

    collection: ClassVar[str] = "run_events"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    events: list[JsonDict] = []

    @staticmethod
    def compose_id(project_id: str, run_id: str, index: int) -> str:
        return f"{project_id}/{run_id}/{index:06d}"
