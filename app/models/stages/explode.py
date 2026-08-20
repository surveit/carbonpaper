"""explode stage: the config block, plus column validation — `column` must name a
`list[X]` column its input supplies, and the signature's `produces` must be that
input's schema with `column` narrowed to X."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Sequence

from pydantic import Field

from app.models.schema import Column, StageConfig, find_list_element_type
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class ExplodeConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"column", "keep_empty"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    column: str = Field(
        description=(
            "The `list[X]` column to unpack. Each element becomes its own output row, "
            "carrying a copy of every other column from the row it came from."
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
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"explode": self.explode}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_explode_column_issues(self, inputs)

    def find_signature_schema_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_explode_signature_issues(self, inputs)


def find_explode_column_issues(
    stage: "ExplodeStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    cols = resolve_input_columns(inputs, 0)
    name = stage.explode.column
    if name not in cols:
        return [COLUMN_ISSUE.format(
            sid=stage.id, field="explode.column", col=name, cols=sorted(cols)
        )]
    if find_list_element_type(_input_column(inputs, name).type) is None:
        return [
            f"stage '{stage.id}': explode.column '{name}' is "
            f"{_input_column(inputs, name).type!r}, not a list — only a `list[X]` column "
            f"has elements to unpack into rows"
        ]
    return []


def find_explode_signature_issues(
    stage: "ExplodeStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    """Every input column survives; only the exploded one changes, to its element type."""
    upstream = inputs[0].table_schema
    exploded = stage.explode.column
    element_type = find_list_element_type(
        next((c.type for c in upstream.columns if c.name == exploded), "")
    )
    if element_type is None:
        return []  # find_explode_column_issues already reported it
    produced = {column.name: column for column in stage.signature.produces}
    issues = [
        f"stage '{stage.id}': explode carries every input column through — signature "
        f"produces must name exactly {sorted(c.name for c in upstream.columns)}, "
        f"missing {sorted(missing)}"
        for missing in [{c.name for c in upstream.columns} - set(produced)]
        if missing
    ]
    issues.extend(
        f"stage '{stage.id}': signature produces column '{name}' that its input "
        f"does not supply (it declares {sorted(c.name for c in upstream.columns)})"
        for name in sorted(set(produced) - {c.name for c in upstream.columns})
    )
    issues.extend(_find_column_spec_issues(stage, upstream.columns, produced, exploded, element_type))
    return issues


def _find_column_spec_issues(
    stage: "ExplodeStage",
    upstream_columns: Sequence[Column],
    produced: dict[str, Column],
    exploded: str,
    element_type: str,
) -> list[str]:
    issues: list[str] = []
    for column in upstream_columns:
        declared = produced.get(column.name)
        if declared is None:
            continue
        wanted = element_type if column.name == exploded else column.type
        if declared.type != wanted:
            issues.append(
                f"stage '{stage.id}': signature produces '{column.name}' as "
                f"{declared.type!r} but explode gives it {wanted!r}"
                + (f" — the element type of {column.type!r}" if column.name == exploded else "")
            )
    return issues


def _input_column(inputs: Sequence["WorkflowStageInput"], name: str) -> Column:
    return next(c for c in inputs[0].table_schema.columns if c.name == name)

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "explode": StageTypeSpec(
        summary="Unpack one list column into one row per element, copying the rest of the row.",
        signature_form="replaces",
        blocks=["explode"],
        requires_inputs=True,
        min_inputs=1,
        required=["column"],
        optional=["keep_empty"],
        notes=(
            "This is how a 1:N model transform becomes rows. An llm_transform returns its "
            "many findings as ONE list column on the row it read; explode turns that column "
            "into a row each, so every finding gets its own row to be reviewed, filtered and "
            "published on. Takes exactly ONE input.\n"
            "`column` must be a `list[X]` column. The signature RESTATES the input's schema "
            "with that one column narrowed from `list[X]` to `X`; every other column is "
            "copied onto each output row unchanged, so `produces` names all of them.\n"
            "A row whose list is empty produces NO output row unless `keep_empty` is set — "
            "set it when a row that found nothing must still reach the output, carrying null.\n"
            "The runtime records which input row each output row came from, so a trace "
            "crosses this stage. Doing the same unpacking in a python_frame_function "
            "does not — which is why this type exists."
        ),
    ),
}
