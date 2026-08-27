"""Transform signatures — the authored contract of what a stage reads and
writes, separate from the mechanism (code, prompt, join keys) that honours it.
Extends = the anchored family: output is the FIRST input extended by
rewrites/adds, every other anchor column flowing through untouched. Replaces =
the reshaping family: nothing flows, output is exactly `produces`."""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Sequence, Union

from pydantic import ConfigDict, Field, model_validator

from app.models.schema import Column, StageId, TableSchema, _Base
from app.models.tool_schema_prompts import (
    EXTENDS_SIGNATURE_DESCRIPTION,
    INPUT_READS_DESCRIPTION,
    REPLACES_SIGNATURE_DESCRIPTION,
)

if TYPE_CHECKING:
    # Only under TYPE_CHECKING, mirroring app.models.stages.shared:
    # app.models.stages.stage_base imports this module at runtime.
    from app.models.stages.stage_base import AbstractStage
    from app.models.workflow_stage import WorkflowStageInput


class InputReads(_Base):
    model_config = ConfigDict(json_schema_extra={"description": INPUT_READS_DESCRIPTION})

    input: StageId = Field(
        description="The upstream stage id, as declared in this stage's `inputs`."
    )
    columns: list[Column] = Field(
        min_length=1,
        description=(
            "What the transform consumes from that input, each with the spec it "
            "relies on. The input's producer must supply every entry "
            "(matching spec, compatible nullability)."
        ),
    )

    @model_validator(mode="after")
    def _no_duplicate_columns(self) -> "InputReads":
        _refuse_duplicate_names(self.columns, f"reads for input `{self.input}`")
        return self


class ExtendsSignature(_Base):
    model_config = ConfigDict(json_schema_extra={"description": EXTENDS_SIGNATURE_DESCRIPTION})

    form: Literal["extends"] = "extends"
    reads: list[InputReads] = Field(
        default_factory=list,
        description=(
            "What the transform consumes, per input. Columns it merely passes "
            "through are NOT reads — they flow without being consumed."
        ),
    )
    adds: list[Column] = Field(
        default_factory=list,
        description=(
            "Columns this stage introduces. Each name must be NEW to the anchor "
            "input — a collision is refused, never renamed."
        ),
    )
    rewrites: list[Column] = Field(
        default_factory=list,
        description=(
            "Anchor columns this stage revises in place, each carrying the spec "
            "it has AFTER this stage (the type may change). Every rewrite must "
            "also be read from the anchor input."
        ),
    )

    @model_validator(mode="after")
    def _writes_consistent(self) -> "ExtendsSignature":
        _refuse_duplicate_names([*self.adds, *self.rewrites], "adds + rewrites")
        return self


class ReplacesSignature(_Base):
    model_config = ConfigDict(json_schema_extra={"description": REPLACES_SIGNATURE_DESCRIPTION})

    form: Literal["replaces"] = "replaces"
    reads: list[InputReads] = Field(
        default_factory=list,
        description="What the transform consumes, per input.",
    )
    produces: list[Column] = Field(
        default_factory=list,
        description=(
            "Every output column, with its spec. Empty only for report, which "
            "emits files rather than a table."
        ),
    )

    @model_validator(mode="after")
    def _no_duplicate_produces(self) -> "ReplacesSignature":
        _refuse_duplicate_names(self.produces, "produces")
        return self


TransformSignature = Annotated[
    Union[ExtendsSignature, ReplacesSignature], Field(discriminator="form")
]


def _refuse_duplicate_names(columns: list[Column], where: str) -> None:
    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            raise ValueError(f"duplicate column {column.name!r} in {where}")
        seen.add(column.name)


# ── The stage-level rules ────────────────────────────────────────────────────
SIGNATURE_ISSUE = "stage '{sid}': signature {problem}"


def find_signature_issues(
    stage: "AbstractStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    signature = stage.signature
    issues = _find_read_issues(stage, inputs, signature.reads)
    if isinstance(signature, ExtendsSignature):
        issues.extend(_find_extends_issues(stage, inputs, signature))
    return issues


def _find_read_issues(
    stage: "AbstractStage",
    inputs: Sequence["WorkflowStageInput"],
    reads: list[InputReads],
) -> list[str]:
    supplied = {ref.id: ref.table_schema for ref in inputs}
    issues: list[str] = []
    seen: set[str] = set()
    for entry in reads:
        if entry.input in seen:
            issues.append(_issue(stage, f"declares reads for input `{entry.input}` twice"))
            continue
        seen.add(entry.input)
        upstream = supplied.get(entry.input)
        if upstream is None:
            issues.append(_issue(
                stage,
                f"reads from `{entry.input}`, which is not one of this stage's inputs "
                f"({sorted(supplied)})",
            ))
            continue
        issues.extend(
            _issue(stage, f"reads from `{entry.input}`: {reason}")
            for reason in TableSchema(columns=entry.columns).find_unsatisfied_columns(upstream)
        )
    return issues


def _find_extends_issues(
    stage: "AbstractStage",
    inputs: Sequence["WorkflowStageInput"],
    signature: ExtendsSignature,
) -> list[str]:
    anchor = inputs[0]
    issues: list[str] = []

    anchor_reads = {column.name for column in anchor_read_columns(stage)}
    issues.extend(
        _issue(stage, f"rewrites `{column.name}` without reading it from the anchor "
                      f"input `{anchor.id}`")
        for column in signature.rewrites
        if column.name not in anchor_reads
    )

    anchor_columns = {column.name for column in anchor.table_schema.columns}
    issues.extend(
        _issue(stage, f"adds `{column.name}`, which the anchor input `{anchor.id}` "
                      f"already supplies — a collision is refused, never renamed; "
                      f"declare a rewrite or use a different name")
        for column in signature.adds
        if column.name in anchor_columns
    )
    return issues


def promised_output_schema(
    stage: "AbstractStage", inputs: Sequence["WorkflowStageInput"]
) -> "TableSchema | None":
    signature = stage.signature
    if isinstance(signature, ExtendsSignature):
        if not inputs:
            raise ValueError(
                f"stage `{stage.id}`: extends-form output resolves off an anchor input, "
                f"and this stage declares none"
            )
        return inputs[0].table_schema.extend(signature.rewrites, signature.adds)
    if not signature.produces:
        return None
    return TableSchema(columns=signature.produces)


def list_written_column_names(stage: "AbstractStage") -> list[str]:
    """[] for a replaces form: nothing flows through it, so no subset is the stage's own work."""
    signature = stage.signature
    if not isinstance(signature, ExtendsSignature):
        return []
    return [column.name for column in [*signature.rewrites, *signature.adds]]


def list_read_column_names(stage: "AbstractStage") -> set[str]:
    """Consumed by the transform; a column that merely flows through is not one."""
    signature = stage.signature
    assert signature is not None, f"stage `{stage.id}`: no signature"
    return {column.name for entry in signature.reads for column in entry.columns}


def anchor_read_columns(stage: "AbstractStage") -> list[Column]:
    """What the transform consumes from its anchor input; [] unless the form flows the rest."""
    signature = stage.signature
    if not stage.inputs or not isinstance(signature, ExtendsSignature):
        return []
    anchor = stage.inputs[0].id
    return [
        column
        for entry in signature.reads
        if entry.input == anchor
        for column in entry.columns
    ]


def transform_input_schemas(stage: "AbstractStage") -> dict[StageId, TableSchema]:
    signature = stage.signature
    assert signature is not None, f"stage `{stage.id}`: no signature"
    reads = {entry.input: list(entry.columns) for entry in signature.reads}
    return {
        ref.id: TableSchema(columns=reads.get(ref.id, [])) for ref in stage.inputs
    }


def transform_output_schema(stage: "AbstractStage") -> TableSchema:
    signature = stage.signature
    assert signature is not None, f"stage `{stage.id}`: no signature"
    if isinstance(signature, ReplacesSignature):
        assert signature.produces, f"stage `{stage.id}` ({stage.type}) writes no table"
        return TableSchema(columns=signature.produces)
    return TableSchema(columns=[*signature.rewrites, *signature.adds])


def _issue(stage: "AbstractStage", problem: str) -> str:
    return SIGNATURE_ISSUE.format(sid=stage.id, problem=problem)


# Rendered once above the stage-type catalog; each type's line names only its form.
SIGNATURE_CONTRACT_NOTE = (
    "Every stage MUST declare `signature` — what its transform reads and "
    "writes, checked against its inputs and config at save. Form `extends`: "
    "output = the first input's rows plus `rewrites` (revised in place) and "
    "`adds` (new columns); every other column flows through untouched. Form "
    "`replaces`: nothing flows; output is exactly `produces`. `reads` lists "
    "what the transform consumes per input — a column that merely passes "
    "through is not a read."
)
