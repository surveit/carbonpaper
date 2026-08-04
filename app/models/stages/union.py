"""union stage: the config block, plus column validation — every declared input
must share an identical schema (prose aside), and a declared output_schema
must equal that shared schema."""
from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import Field

from app.models.schema import StageConfig, TableSchema
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.signature import OverwritesSignature


class UnionConfig(StageConfig):
    """union config block. No fields: a union's behavior is fixed entirely by its
    (schema-identical) declared inputs, concatenated in declared order."""
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset()
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()


class UnionStage(StageBase):
    type: Literal[StageType.union]
    union: UnionConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=2)
    transform_signature: Optional[OverwritesSignature] = None

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"union": self.union}

    def find_unsupplied_reads(self) -> list[str]:
        return find_union_column_issues(self)

    def find_unaccounted_writes(self) -> list[str]:
        return find_union_output_issues(self)

    def find_signature_disagreements(self) -> list[str]:
        return find_union_signature_issues(self)


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


def find_union_signature_issues(stage: "UnionStage") -> list[str]:
    """A union reads no columns, and every input must supply `writes`."""
    signature = stage.transform_signature
    assert signature is not None  # find_signature_disagreements runs only with one
    issues = [
        f"stage '{stage.id}': a union concatenates without consuming any column; "
        f"transform_signature reads must be empty"
    ] if signature.reads else []
    produced = TableSchema(columns=signature.writes)
    issues.extend(
        f"stage '{stage.id}': transform_signature writes vs input `{ref.id}` — {reason}"
        for ref in stage.inputs
        for reason in produced.find_unsatisfied_columns(ref.table_schema)
    )
    return issues

# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "union": NodeTypeSpec(
        summary="Concatenate two or more upstream dataframes with an identical schema.",
        transform_signature_form="overwrites",
        blocks=["union"],
        requires_inputs=True,
        min_inputs=2,
        required=[],
        optional=[],
        notes=(
            "No configuration — pass `union: {}`. Every input must declare an IDENTICAL "
            "schema (same columns, same types); a mismatch is refused when the stage is "
            "saved, naming the differing columns. Concatenates the inputs in declared "
            "order; output_schema must equal that shared schema."
        ),
    ),
}
