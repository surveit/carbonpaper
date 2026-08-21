"""union stage: the config block, and the check that every input shares one schema."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Sequence

from pydantic import Field

from app.models.schema import StageConfig
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class UnionConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset()
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()


class UnionStage(AbstractStage):
    type: Literal[StageType.union]
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "concatenation costs less than hashing its own input would"
    )
    union: UnionConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=2)
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"union": self.union}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_union_column_issues(self, inputs)

    def find_signature_config_issues(self) -> list[str]:
        return find_union_signature_writes(self)


def find_union_signature_writes(stage: "UnionStage") -> list[str]:
    written = {
        "reads": stage.signature.reads,
        "adds": stage.signature.adds,
        "rewrites": stage.signature.rewrites,
    }
    named = sorted(field for field, entries in written.items() if entries)
    if not named:
        return []
    return [
        f"stage '{stage.id}': a union concatenates rows and touches no column, so its "
        f"signature declares nothing — {', '.join(named)} must be empty"
    ]


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

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "union": StageTypeSpec(
        summary="Concatenate two or more upstream dataframes with an identical schema.",
        signature_form="extends",
        blocks=["union"],
        requires_inputs=True,
        min_inputs=2,
        required=[],
        optional=[],
        notes=(
            "No configuration — pass `union: {}`. Every input must declare an IDENTICAL "
            "schema (same columns, same types); a mismatch is refused when the stage is "
            "saved, naming the differing columns. Concatenates the inputs in declared "
            "order. Return `[]` for `reads`, `adds` and `rewrites` — a union cannot "
            "change data."
        ),
    ),
}
