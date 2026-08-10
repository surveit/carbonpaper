"""Transform signatures — the authored contract of what a stage reads and
writes, separate from the mechanism (code, prompt, join keys) that honours it.
Extends = the anchored family: output is the FIRST input extended by
rewrites/adds, every other anchor column flowing through untouched. Replaces =
the reshaping family: nothing flows, output is exactly `produces`."""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Union

from pydantic import Field, model_validator

from app.models.schema import Column, StageId, TableSchema, _Base

if TYPE_CHECKING:
    # Only under TYPE_CHECKING, mirroring app.models.stages.shared:
    # app.models.stages.stage_base imports this module at runtime.
    from app.models.stages.stage_base import StageBase


class InputReads(_Base):
    """The columns a stage's transform consumes from ONE of its inputs."""

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
    """An anchored stage's contract: the first input's rows, rewritten and extended."""

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
    """The contract of a reshaping stage: nothing flows through, the output is
    exactly `produces`."""

    form: Literal["replaces"] = "replaces"
    reads: list[InputReads] = Field(
        default_factory=list,
        description="What the transform consumes, per input.",
    )
    produces: list[Column] = Field(
        default_factory=list,
        description=(
            "Every output column, with its spec. Empty only for publish, which "
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


def find_signature_issues(stage: "StageBase") -> list[str]:
    """Signature-vs-stage disagreements; edge-only, per stage, [] without a signature."""
    signature = stage.signature
    if signature is None:
        return []
    issues = _find_read_issues(stage, signature.reads)
    if isinstance(signature, ExtendsSignature):
        issues.extend(_find_extends_issues(stage, signature))
    return issues


def _find_read_issues(stage: "StageBase", reads: list[InputReads]) -> list[str]:
    """Reads must name declared inputs, once each, and be satisfied by their edges."""
    edges = {ref.id: ref.table_schema for ref in stage.inputs}
    issues: list[str] = []
    seen: set[str] = set()
    for entry in reads:
        if entry.input in seen:
            issues.append(_issue(stage, f"declares reads for input `{entry.input}` twice"))
            continue
        seen.add(entry.input)
        edge = edges.get(entry.input)
        if edge is None:
            issues.append(_issue(
                stage,
                f"reads from `{entry.input}`, which is not one of this stage's inputs "
                f"({sorted(edges)})",
            ))
            continue
        issues.extend(
            _issue(stage, f"reads from `{entry.input}`: {reason}")
            for reason in TableSchema(columns=entry.columns).find_unsatisfied_columns(edge)
        )
    return issues


def _find_extends_issues(stage: "StageBase", signature: ExtendsSignature) -> list[str]:
    if not stage.inputs:
        return [_issue(stage, "is extends-form, which needs an anchor: at least one input")]
    anchor = stage.inputs[0]
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


def promised_output_schema(stage: "StageBase") -> "TableSchema | None":
    """The output the signature promises; None without one (or empty produces)."""
    signature = stage.signature
    if signature is None:
        return None
    if isinstance(signature, ExtendsSignature):
        if not stage.inputs:
            return None
        return stage.inputs[0].table_schema.extend(signature.rewrites, signature.adds)
    if not signature.produces:
        return None
    return TableSchema(columns=signature.produces)


def anchor_read_columns(stage: "StageBase") -> list[Column]:
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


def input_read_schemas(stage: "StageBase") -> dict[StageId, TableSchema]:
    """Per declared input, what the transform consumes from it — empty where it reads none."""
    signature = stage.signature
    reads = {} if signature is None else {
        entry.input: list(entry.columns) for entry in signature.reads
    }
    return {
        ref.id: TableSchema(columns=reads.get(ref.id, [])) for ref in stage.inputs
    }


def output_schema_over_reads(stage: "StageBase") -> "TableSchema | None":
    """What comes out when the input carries ONLY the reads; None when that is no columns."""
    signature = stage.signature
    if signature is None:
        return None
    # Nothing flows under `replaces`, so narrowing the input cannot narrow the output.
    if isinstance(signature, ReplacesSignature):
        return TableSchema(columns=signature.produces) if signature.produces else None
    read = TableSchema(columns=anchor_read_columns(stage))
    extended = read.extend(signature.rewrites, signature.adds)
    return extended if extended.columns else None


def _issue(stage: "StageBase", problem: str) -> str:
    return SIGNATURE_ISSUE.format(sid=stage.id, problem=problem)


# Rendered once above the stage-type catalog; each type's line names only its form.
SIGNATURE_CONTRACT_NOTE = (
    "Every stage MUST declare `signature` — what its transform reads and "
    "writes, checked against its edges and config at save. Form `extends`: "
    "output = the first input's rows plus `rewrites` (revised in place) and "
    "`adds` (new columns); every other column flows through untouched. Form "
    "`replaces`: nothing flows; output is exactly `produces`. `reads` lists "
    "what the transform consumes per input — a column that merely passes "
    "through is not a read."
)
