"""union stage: the config block, plus column validation — every declared input
must share an identical schema (prose aside), and a declared output_schema
must equal that shared schema."""
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from app.models.schema import StageConfig
from app.models.stage_base import StageBase, StageInput, StageType


class UnionConfig(StageConfig):
    """union config block. No fields: a union's behavior is fixed entirely by its
    (schema-identical) declared inputs, concatenated in declared order."""
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset()
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()


class UnionStage(StageBase):
    type: Literal[StageType.union]
    union: UnionConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=2)

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"union": self.union}

    def find_config_column_issues(self) -> list[str]:
        return find_union_column_issues(self)

    def find_output_schema_issues(self) -> list[str]:
        return find_union_output_issues(self)


def find_union_column_issues(stage: "UnionStage") -> list[str]:
    """One issue per input (after the first) whose declared schema disagrees
    with the first input's, naming the differing columns."""
    schemas = [(ref.id, ref.table_schema) for ref in stage.inputs]
    reference_id, reference = schemas[0]
    issues: list[str] = []
    for input_id, schema in schemas[1:]:
        differing = sorted(schema.differing_column_names(reference))
        if differing:
            issues.append(
                f"stage '{stage.id}': union input '{input_id}' disagrees with input "
                f"'{reference_id}' on column(s) {differing}"
            )
    return issues


def find_union_output_issues(stage: "UnionStage") -> list[str]:
    """Issue naming any column where the declared output_schema disagrees with
    the union's (already schema-identical) inputs."""
    assert stage.output_schema is not None  # StageBase._schemas_declared guarantees this
    reference = stage.inputs[0].table_schema
    differing = sorted(stage.output_schema.differing_column_names(reference))
    if not differing:
        return []
    return [
        f"stage '{stage.id}': output_schema disagrees with the union's shared "
        f"input schema on column(s) {differing}"
    ]
