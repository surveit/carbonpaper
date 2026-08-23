from __future__ import annotations

from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope
from app.models.named_schemas import SchemaLibrary
from app.models.terms import Verb


class StoredTerms(PersistedModel):
    """The halves are stored apart; composing them is where a word meaning two things raises."""

    collection: ClassVar[str] = "terms"
    # The generators read it; only the authoring surface writes it.
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    nouns: SchemaLibrary
    verbs: list[Verb]
