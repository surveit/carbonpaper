"""What one run published for a workflow output a stage declared."""
from __future__ import annotations

from typing import ClassVar, Optional

from app.core.ids import ID
from app.core.record import PersistedModel, PersistenceScope
from app.models.claims import PublishedCitation


class WorkflowOutput(PersistedModel):
    collection: ClassVar[str] = "workflow_output"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    slug: str
    label: str
    primary: bool = False
    # Copied off the rule, so minting reads the run and not the version behind it.
    shape_id: Optional[ID] = None
    citation: PublishedCitation
