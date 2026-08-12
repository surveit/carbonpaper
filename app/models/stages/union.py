"""union stage: the config block, plus column validation — every declared input
must share an identical schema (prose aside), and the signature's `produces`
must be satisfied by every one of them."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Sequence

from pydantic import Field

from app.models.schema import StageConfig, TableSchema
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class UnionConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset()
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()


class UnionStage(AbstractStage):
    type: Literal[StageType.union]
    union: UnionConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=2)
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"union": self.union}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_union_column_issues(self, inputs)

    def find_signature_config_issues(self) -> list[str]:
        if not self.signature.reads:
            return []
        return [
            f"stage '{self.id}': a union concatenates without consuming any column; "
            f"signature reads must be empty"
        ]

    def find_signature_schema_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_union_signature_issues(self, inputs)


def find_union_column_issues(
    stage: "UnionStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    schemas = [(ref.id, ref.table_schema) for ref in inputs]
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


def find_union_signature_issues(
    stage: "UnionStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    produced = TableSchema(columns=stage.signature.produces)
    return [
        f"stage '{stage.id}': signature produces vs input `{ref.id}` — {reason}"
        for ref in inputs
        for reason in produced.find_unsatisfied_columns(ref.table_schema)
    ]

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "union": StageTypeSpec(
        summary="Concatenate two or more upstream dataframes with an identical schema.",
        signature_form="replaces",
        blocks=["union"],
        requires_inputs=True,
        min_inputs=2,
        required=[],
        optional=[],
        notes=(
            "No configuration — pass `union: {}`. Every input must declare an IDENTICAL "
            "schema (same columns, same types); a mismatch is refused when the stage is "
            "saved, naming the differing columns. Concatenates the inputs in declared "
            "order; the signature's `produces` restates that shared schema and reads nothing."
        ),
    ),
}
