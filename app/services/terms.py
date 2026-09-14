"""A project's row types, tables and verbs: the sole reader and writer of the stored document."""
from __future__ import annotations

from typing import NamedTuple

from app.core.json_types import JsonDict
from app.models.terms import RowTypesAndSchemas, Terms
from app.models.records.terms import StoredTerms
from app.services import workspace


def load_terms(project_id: str) -> Terms:
    """A project that stored none has none: empty Terms, never a stand-in word."""
    stored = StoredTerms.load_or_none(_document_id(project_id))
    if stored is None:
        return _read_pre_store_terms(project_id)
    return Terms(row_types=stored.row_types, schemas=stored.schemas, verbs=stored.verbs)


def count_schemas(project_id: str) -> int:
    """Counts what the project stored, a pre-store file too broken to parse included."""
    stored = StoredTerms.load_or_none(_document_id(project_id))
    if stored is not None:
        return len(stored.schemas.schemas)
    return len(workspace.load_schemas(project_id))


def has_terms(project_id: str) -> bool:
    """Any part: a project may agree its words long before a table holds their rows."""
    stored = StoredTerms.load_or_none(_document_id(project_id))
    if stored is not None:
        return bool(stored.row_types or stored.schemas.schemas or stored.verbs)
    return bool(workspace.load_schemas(project_id))


def write_terms(project_id: str, terms: Terms) -> None:
    """Replaces all three — a word absent from `terms` is a word the project no longer uses."""
    StoredTerms(
        id=_document_id(project_id),
        row_types=terms.row_types,
        schemas=terms.schemas,
        verbs=terms.verbs,
    ).save()


def write_data_model(project_id: str, generated: RowTypesAndSchemas) -> None:
    """A generator that authors no verb retires none the project already agreed."""
    agreed = load_terms(project_id)
    write_terms(project_id, Terms(
        row_types=generated.row_types, schemas=generated.schemas, verbs=agreed.verbs
    ))


def _document_id(project_id: str) -> str:
    # Composed: the store lists by id PREFIX, so a bare id would match a sibling's.
    return f"{project_id}/terms"


# ─── Nouns authored before a row type was its own thing ──────────────────────

# `<project>/schemas/` files are read here and never written; alembic 0021 rewrites
# the stored ones.


# The pair RowTypesAndSchemas validates, before anything has validated it: alembic 0021
# reads it, so it must not depend on the models' current shape.
class UncheckedRowTypesAndSchemas(NamedTuple):
    row_types: list[JsonDict]
    schemas: list[JsonDict]


def split_pre_row_type_nouns(nouns: list[JsonDict]) -> UncheckedRowTypesAndSchemas:
    """Each noun mints the row type its rows are AND the table holding them, in its own order."""
    return UncheckedRowTypesAndSchemas(
        row_types=[_mint_row_type(noun) for noun in nouns],
        schemas=[_point_table_at_its_row_type(noun) for noun in nouns],
    )


def _mint_row_type(noun: JsonDict) -> JsonDict:
    name, title = noun.get("name"), noun.get("title")
    if not name or not title:
        raise ValueError(f"a noun carrying no name and title is no row type: {noun}")
    return {
        "id": name,
        "title": title,
        # An undefined word is still the word, and its title the only gloss authored.
        "definition": noun.get("description") or title,
        "also_written": noun.get("also_written") or [],
    }


def _point_table_at_its_row_type(noun: JsonDict) -> JsonDict:
    table = {key: value for key, value in noun.items() if key != "also_written"}
    return {**table, "row_type_id": noun["name"]}


def _read_pre_store_terms(project_id: str) -> Terms:
    # The file loader stamps `_filename` on each; the model forbids what it does not declare.
    nouns = [{key: value for key, value in schema.items() if not key.startswith("_")}
             for schema in workspace.load_schemas(project_id)]
    words = split_pre_row_type_nouns(nouns)
    return Terms.model_validate(
        {"row_types": words.row_types, "schemas": {"schemas": words.schemas}, "verbs": []}
    )
