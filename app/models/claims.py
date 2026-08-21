"""What a project intends to claim, declared with the methodology before any stage exists."""
from __future__ import annotations

from enum import Enum
from string import Formatter
from typing import ClassVar
from uuid import uuid4

from pydantic import model_validator

from app.core.persistence import PersistedModel, PersistenceScope
from app.models.schema import TableSchema

# The one slot a sentence may fill from something other than a declared column.
ROW_COUNT_SLOT = "n"


class Salience(str, Enum):
    primary = "primary"
    secondary = "secondary"


class UniverseRequirement(str, Enum):
    none = "none"
    equal_coverage = "equal_coverage"
    closed = "closed"


class ClaimShape(PersistedModel):
    collection: ClassVar[str] = "claim_shape"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # Authored before any stage exists, so it declares no stage and no column.
    says: str
    table_schema: TableSchema
    requires: UniverseRequirement
    salience: Salience

    @staticmethod
    def compose_id(project_id: str) -> str:
        return f"{project_id}/{uuid4().hex}"

    def find_slot_names(self) -> list[str]:
        return find_slot_names(self.says)

    @model_validator(mode="after")
    def _refuse_an_unfillable_declaration(self) -> "ClaimShape":
        problems = find_declaration_problems(self.says, self.table_schema)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def find_declaration_problems(says: str, table_schema: TableSchema) -> list[str]:
    """Everything wrong with a shape, checkable with no run and no data."""
    if len(table_schema.columns) != 1:
        return [
            f"a claim shape declares exactly one column while tabular claims are withheld, "
            f"and this one declares {len(table_schema.columns)}"
        ]
    return _find_unfillable_slots(says, {column.name for column in table_schema.columns})


def find_slot_names(says: str) -> list[str]:
    try:
        return [name for _, name, _, _ in Formatter().parse(says) if name is not None]
    except ValueError as malformed:
        raise ValueError(f"{says!r} is not a fillable sentence: {malformed}") from malformed


def _find_unfillable_slots(says: str, declared: set[str]) -> list[str]:
    return [
        f"{says!r} fills {{{name}}}, which is neither {{{ROW_COUNT_SLOT}}} nor one of the "
        f"columns it declares ({sorted(declared)})"
        for name in find_slot_names(says)
        if name != ROW_COUNT_SLOT and name not in declared
    ]
