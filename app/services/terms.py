"""Terms service: a project's nouns and its verbs, held as one stored document per
project. This module is the sole reader and writer of both halves — generation hands its
validated result here, and readers that need models, not raw dicts, load through here."""
from __future__ import annotations

from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope
from app.models import parse_schema_library
from app.models.named_schemas import SchemaLibrary
from app.models.terms import Terms, Verb
from app.services import workspace


class StoredTerms(PersistedModel):
    """The halves are stored apart; composing them is where a word meaning two things raises."""

    collection: ClassVar[str] = "terms"
    # Read by the review-guide and stage-test generators, written only by the
    # authoring surface — WorkflowVersion's and ReviewGuide's profile.
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    nouns: SchemaLibrary
    verbs: list[Verb]


def load_terms(project_id: str) -> Terms:
    """A project that stored none has none: empty Terms, never a stand-in word."""
    stored = StoredTerms.load_or_none(_document_id(project_id))
    if stored is None:
        return Terms(nouns=_read_pre_store_nouns(project_id), verbs=[])
    return Terms(nouns=stored.nouns, verbs=stored.verbs)


def count_nouns(project_id: str) -> int:
    """Counts what the project stored, a pre-store file too broken to parse included."""
    stored = StoredTerms.load_or_none(_document_id(project_id))
    if stored is not None:
        return len(stored.nouns.schemas)
    return len(workspace.load_schemas(workspace.resolve_project_dir(project_id)))


def write_terms(project_id: str, terms: Terms) -> None:
    """Replaces both halves — a word absent from `terms` is a word the project no longer uses."""
    StoredTerms(id=_document_id(project_id), nouns=terms.nouns, verbs=terms.verbs).save()


def write_nouns(project_id: str, nouns: SchemaLibrary) -> None:
    """Keeps the verbs already agreed: a generator answering with nouns alone retires none."""
    write_terms(project_id, Terms(nouns=nouns, verbs=load_terms(project_id).verbs))


def _document_id(project_id: str) -> str:
    # Composed: the store lists by id PREFIX, so a bare id would match a sibling's.
    return f"{project_id}/terms"


# ─── Nouns written before this collection existed ────────────────────────────
# One file per schema under `<project>/schemas/`, which every project authored up to
# now carries. Read, never written: the first write_terms moves that project into the
# store and the files stop being consulted.


def _read_pre_store_nouns(project_id: str) -> SchemaLibrary:
    schemas = workspace.load_schemas(workspace.resolve_project_dir(project_id))
    # The file loader stamps `_filename` on each; the model forbids what it does not declare.
    return parse_schema_library(
        [{k: v for k, v in schema.items() if not k.startswith("_")} for schema in schemas]
    )
