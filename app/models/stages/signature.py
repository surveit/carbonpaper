"""What a stage's transform reads and writes, apart from the mechanism (code,
prompt, join keys) that honours it. Extends = the output is the FIRST INPUT with
columns created and updated. Overwrites = the output is exactly `writes`. Both
forms expose `.reads` and `.writes`, so generic code never branches on form."""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Union

from pydantic import Field, model_validator

from app.models.schema import Column, StageId, TableSchema, _Base

if TYPE_CHECKING:
    # Only under TYPE_CHECKING, mirroring app.models.stages.shared:
    # app.models.stage_base imports this module at runtime.
    from app.models.stage_base import StageBase


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
    """The output is the first input's rows, with columns created and updated."""

    form: Literal["extends"] = "extends"
    reads: list[InputReads] = Field(
        default_factory=list,
        description=(
            "What the transform consumes, per input. A column the stage carries "
            "through untouched is not a read."
        ),
    )
    creates: list[Column] = Field(
        default_factory=list,
        description=(
            "Columns new to the first input. A name it already supplies is "
            "refused, never renamed — declare an update."
        ),
    )
    updates: list[Column] = Field(
        default_factory=list,
        description=(
            "First-input columns revised in place, with the spec they have AFTER "
            "(the type may change). Every update is also a read."
        ),
    )

    @property
    def writes(self) -> list[Column]:
        return [*self.creates, *self.updates]

    @model_validator(mode="after")
    def _writes_consistent(self) -> "ExtendsSignature":
        _refuse_duplicate_names(self.writes, "creates + updates")
        return self


class OverwritesSignature(_Base):
    """The output is exactly `writes`; the inputs' columns do not carry through."""

    form: Literal["overwrites"] = "overwrites"
    reads: list[InputReads] = Field(
        default_factory=list,
        description="What the transform consumes, per input.",
    )
    writes: list[Column] = Field(
        default_factory=list,
        description=(
            "The whole output, column by column. Empty only for publish, which "
            "emits files rather than a table."
        ),
    )

    @model_validator(mode="after")
    def _no_duplicate_writes(self) -> "OverwritesSignature":
        _refuse_duplicate_names(self.writes, "writes")
        return self


TransformSignature = Annotated[
    Union[ExtendsSignature, OverwritesSignature], Field(discriminator="form")
]


def _refuse_duplicate_names(columns: list[Column], where: str) -> None:
    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            raise ValueError(f"duplicate column {column.name!r} in {where}")
        seen.add(column.name)


# ── The stage-level rules ────────────────────────────────────────────────────
SIGNATURE_ISSUE = "stage '{sid}': transform_signature {problem}"


def find_signature_issues(stage: "StageBase") -> list[str]:
    """Edge-only, per stage; [] for a stage carrying no transform_signature."""
    signature = stage.transform_signature
    if signature is None:
        return []
    issues = _find_read_issues(stage, signature.reads)
    if isinstance(signature, ExtendsSignature):
        issues.extend(_find_extends_issues(stage, signature))
    else:
        issues.extend(_find_overwrites_issues(stage, signature))
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
        return [_issue(stage, "is extends-form, which needs a first input: this stage has none")]
    first_input = stage.inputs[0]
    issues: list[str] = []

    first_input_reads = {
        column.name
        for entry in signature.reads
        if entry.input == first_input.id
        for column in entry.columns
    }
    issues.extend(
        _issue(stage, f"updates `{column.name}` without reading it from the first "
                      f"input `{first_input.id}`")
        for column in signature.updates
        if column.name not in first_input_reads
    )

    first_input_columns = {column.name for column in first_input.table_schema.columns}
    colliding = [
        column.name for column in signature.creates
        if column.name in first_input_columns
    ]
    issues.extend(
        _issue(stage, f"creates `{name}`, which the first input `{first_input.id}` "
                      f"already supplies — a collision is refused, never renamed; "
                      f"declare an update or use a different name")
        for name in colliding
    )

    # A colliding create makes the promised output ill-defined, so the comparison
    # below only runs once the creates are genuinely new. The comparison exists
    # because output_schema is authored BESIDE the transform_signature — two
    # accounts of one output can drift; an output_schema computed from the
    # first-input edge and the transform_signature satisfies it by construction.
    if stage.output_schema is not None and not colliding:
        expected = first_input.table_schema.extend(signature.updates, signature.creates)
        differing = sorted(stage.output_schema.differing_column_names(expected))
        if differing:
            issues.append(_issue(
                stage,
                f"output_schema disagrees with the first input extended by this "
                f"transform_signature on column(s) {differing}",
            ))
    return issues


def _find_overwrites_issues(stage: "StageBase", signature: OverwritesSignature) -> list[str]:
    if stage.output_schema is None or not signature.writes:
        return []
    written = TableSchema(columns=signature.writes)
    differing = sorted(stage.output_schema.differing_column_names(written))
    if differing:
        return [_issue(
            stage, f"output_schema disagrees with `writes` on column(s) {differing}"
        )]
    return []


def promised_output_schema(stage: "StageBase") -> "TableSchema | None":
    """The output the transform_signature promises; None without one (or empty writes)."""
    signature = stage.transform_signature
    if signature is None:
        return None
    if isinstance(signature, ExtendsSignature):
        if not stage.inputs:
            return None
        return stage.inputs[0].table_schema.extend(signature.updates, signature.creates)
    if not signature.writes:
        return None
    return TableSchema(columns=signature.writes)


def _issue(stage: "StageBase", problem: str) -> str:
    return SIGNATURE_ISSUE.format(sid=stage.id, problem=problem)


# Rendered once above the stage-type catalog; each type's line names only its form.
SIGNATURE_CONTRACT_NOTE = (
    "Every stage MUST declare `transform_signature` — what its transform reads "
    "and writes, checked against its edges and config at save. Form `extends`: "
    "output = the first input's rows plus `updates` (revised in place) and "
    "`creates` (new columns); every other column carries through untouched. Form "
    "`overwrites`: nothing carries through; output is exactly `writes`. `reads` "
    "lists what the transform consumes per input — a column that merely passes "
    "through is not a read."
)
