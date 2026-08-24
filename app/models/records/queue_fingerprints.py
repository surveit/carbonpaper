from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope


class QueueFingerprints(PersistedModel):
    """A halted queue stage's bookkeeping, stored as
    `queue_fingerprints/<project>/<run_id>/<stage_id>`."""

    collection: ClassVar[str] = "queue_fingerprints"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    # docs/run-manifest.md
    stage_fingerprint: str
    input_fingerprints: list[str]
    row_ordinals: list[int] | None = None

    @staticmethod
    def compose_id(project_id: str, run_id: str, stage_id: str) -> str:
        return f"{project_id}/{run_id}/{stage_id}"
