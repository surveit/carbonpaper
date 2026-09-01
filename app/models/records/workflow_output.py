"""What one run published for a workflow output a stage declared."""
from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope
from app.models.claims import PublishedCitation


class WorkflowOutput(PersistedModel):
    collection: ClassVar[str] = "workflow_output"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    slug: str
    label: str
    primary: bool = False
    citation: PublishedCitation
