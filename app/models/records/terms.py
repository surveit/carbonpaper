from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope
from app.models.named_schemas import SchemaLibrary
from app.models.row_types import RowType
from app.models.terms import Verb


class StoredTerms(PersistedModel):
    """Composing these into Terms is where a word meaning two things raises."""

    collection: ClassVar[str] = "terms"
    # The generators read it; only the authoring surface writes it.
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    SCHEMA_VERSION: ClassVar[int] = 2

    row_types: list[RowType]
    schemas: SchemaLibrary
    verbs: list[Verb]
