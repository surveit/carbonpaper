"""A project's data model — its named schemas — as one stored document.

Sole owner of the "data_model" collection: generation hands its validated result
here, and every reader that wants schemas comes through here rather than
composing a path.
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from app.core.persistence import PersistedModel, PersistenceScope
from app.models.named_schemas import NamedSchema, SchemaLibrary


class DataModel(PersistedModel):
    """One project's named schemas, `id`'d by project name."""

    collection: ClassVar[str] = "data_model"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # Each schema validates on its own; the LIBRARY rules — unique names,
    # resolvable references — are deliberately NOT applied here, so a data model
    # that has drifted into inconsistency still loads and the page can show the
    # reader what is wrong instead of failing to render. SchemaLibrary owns those
    # rules, and load_data_model below is where they are enforced.
    schemas: list[NamedSchema] = Field(default_factory=list)


def load_schemas(project: str) -> list[NamedSchema]:
    """The project's schemas, or [] when it has no data model yet."""
    record = DataModel.load_or_none(project)
    return list(record.schemas) if record is not None else []


def load_data_model(project: str) -> SchemaLibrary | None:
    """Strict: raises if the stored schemas no longer form a consistent library."""
    schemas = load_schemas(project)
    return SchemaLibrary(schemas=schemas) if schemas else None


def write_data_model(project: str, library: SchemaLibrary) -> None:
    """Whole-model write, so a shrinking re-generation leaves no stale schema."""
    _save(project, list(library.schemas))


def write_schema(project: str, schema: NamedSchema) -> None:
    """Revise the schema of this name in place; KeyError if there is none."""
    schemas = load_schemas(project)
    index = next((i for i, s in enumerate(schemas) if s.name == schema.name), None)
    if index is None:
        raise KeyError(schema.name)
    schemas[index] = schema
    _save(project, schemas)


def _save(project: str, schemas: list[NamedSchema]) -> None:
    record = DataModel.load_or_none(project) or DataModel(id=project)
    record.schemas = schemas
    record.save()
