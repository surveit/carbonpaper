"""explode stage: the config block, plus validation that `column` names a `list[X]`
column its input supplies and that the signature rewrites exactly that column, to X."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Sequence

from pydantic import Field

from app.models.schema import Column, StageConfig, find_list_element_type
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class ExplodeConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"column", "keep_empty"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    column: str = Field(
        description=(
            "The `list[X]` column to unpack: a real array at rest (a JSON list), never a "
            "delimited string. Each element becomes its own output row, carrying a copy of "
            "every other column from the row it came from."
        )
    )
    keep_empty: bool = Field(
        default=False,
        description=(
            "What becomes of a row whose list is empty or null. False drops it — the "
            "row had nothing to say. True keeps one output row carrying null in "
            "`column`, which is what you want when the absence itself is a finding "
            "(a document the model read and found no claims in)."
        ),
    )


class ExplodeStage(AbstractStage):
    type: Literal[StageType.explode]
    explode: ExplodeConfig
    # Exactly one input: unpacking a column of one frame. Combining two is a join.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"explode": self.explode}

    def find_signature_config_issues(self) -> list[str]:
        return find_explode_signature_issues(self)

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_explode_column_issues(self, inputs)


def find_explode_signature_issues(stage: "ExplodeStage") -> list[str]:
    """Every other column flows through, so the ONE write is `column`, narrowed to its element."""
    signature = stage.signature
    column = stage.explode.column
    issues = [
        f"stage '{stage.id}': explode adds no column — it unpacks '{column}' into rows "
        f"and copies the rest of the row onto each; signature adds must be empty"
    ] if signature.adds else []
    rewritten = {rewrite.name: rewrite for rewrite in signature.rewrites}
    if column not in rewritten:
        issues.append(
            f"stage '{stage.id}': explode narrows '{column}' from a list to one element "
            f"per row — signature rewrites must declare it, carrying the element type"
        )
    issues.extend(
        f"stage '{stage.id}': signature rewrites '{name}', which explode does not touch "
        f"— it changes '{column}' and copies every other column through"
        for name in sorted(set(rewritten) - {column})
    )
    return issues


def find_explode_column_issues(
    stage: "ExplodeStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    cols = resolve_input_columns(inputs, 0)
    name = stage.explode.column
    if name not in cols:
        return [COLUMN_ISSUE.format(
            sid=stage.id, field="explode.column", col=name, cols=sorted(cols)
        )]
    supplied = _input_column(inputs, name)
    element_type = find_list_element_type(supplied.type)
    if element_type is None:
        return [
            f"stage '{stage.id}': explode.column '{name}' is {supplied.type!r}, not a "
            f"list — only a `list[X]` column has elements to unpack into rows"
        ]
    return _find_element_type_issues(stage, name, element_type)


def _find_element_type_issues(
    stage: "ExplodeStage", name: str, element_type: str
) -> list[str]:
    declared = next((r for r in stage.signature.rewrites if r.name == name), None)
    if declared is None or declared.type == element_type:
        return []  # find_explode_signature_issues reports a missing rewrite
    return [
        f"stage '{stage.id}': signature rewrites '{name}' as {declared.type!r}, but "
        f"unpacking its input's {element_type!r} elements gives {element_type!r}"
    ]


def _input_column(inputs: Sequence["WorkflowStageInput"], name: str) -> Column:
    return next(c for c in inputs[0].table_schema.columns if c.name == name)

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "explode": StageTypeSpec(
        summary="Unpack one list column into one row per element, copying the rest of the row.",
        signature_form="extends",
        blocks=["explode"],
        requires_inputs=True,
        min_inputs=1,
        required=["column"],
        optional=["keep_empty"],
        notes=(
            "This is how many-things-on-one-row becomes many rows: a source column that "
            "already holds an array, a starlark step that split a field, or — most often — "
            "an llm_transform, which returns its many findings as ONE list column on the "
            "row it read. Whatever put it there, explode turns that column into a row each, "
            "so every element gets its own row to be reviewed, filtered and published on. "
            "Takes exactly ONE input.\n"
            "`column` must be DECLARED `list[X]`, and hold a real array at rest — a JSON "
            "list, not a delimited string. Explode does not split text: to unpack "
            "`\"TAX,ENERGY\"` give it a starlark_row_function that returns a list first.\n"
            "The signature READS that column and REWRITES it to `X` — the element type. It "
            "adds nothing: every other column is copied onto each output row unchanged, so "
            "none of them is declared.\n"
            "A row whose list is empty produces NO output row unless `keep_empty` is set — "
            "set it when a row that found nothing must still reach the output, carrying null.\n"
            "The runtime records which input row each output row came from, so a trace "
            "crosses this stage — which is the reason to unpack here rather than in "
            "authored code, where the trace would stop."
        ),
    ),
}
