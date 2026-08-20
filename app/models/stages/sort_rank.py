"""sort_rank stage: the config block, plus column validation — every sort key and
the added rank column must resolve against the input, and the signature's
`produces` is that input's schema plus the rank column."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Optional, Sequence

from pydantic import Field, model_validator

from app.models.schema import Column, StageConfig, TableSchema, _Base
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput

RANK_COLUMN_TYPE = "int"


class SortKey(_Base):
    column: str
    descending: bool = Field(
        default=False, description="Largest (or latest, or Z-first) first."
    )
    order: Optional[list[str]] = Field(
        default=None,
        description=(
            "An explicit value order for a column whose ranking is not its sort order — "
            "severity tiers, priority bands, a scale from `critical` to `routine`. List "
            "the values first-to-last. A value the column holds but this list omits is "
            "refused at run time rather than sorted to an end and quietly ranked."
        ),
    )

    @model_validator(mode="after")
    def _order_is_not_also_reversed(self) -> "SortKey":
        if self.order is not None and self.descending:
            raise ValueError(
                f"sort key `{self.column}`: `order` already states the order first-to-last "
                f"— reverse the list rather than also setting descending"
            )
        if self.order is not None and len(set(self.order)) != len(self.order):
            raise ValueError(f"sort key `{self.column}`: `order` repeats a value")
        return self


class SortRankConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"keys", "rank_column"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    keys: list[SortKey] = Field(
        min_length=1, description="Sort keys in priority order; the first decides, the rest break ties."
    )
    rank_column: Optional[str] = Field(
        default=None,
        description=(
            "Name for a 1-based position column to add. Omit to sort without numbering. "
            "Ties are numbered by their sorted position, so two rows the keys cannot "
            "separate still get different numbers."
        ),
    )


class SortRankStage(AbstractStage):
    type: Literal[StageType.sort_rank]
    sort_rank: SortRankConfig
    # Exactly one input: ordering one frame's rows against each other.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"sort_rank": self.sort_rank}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_sort_rank_column_issues(self, inputs)

    def find_signature_schema_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_sort_rank_signature_issues(self, inputs)


def find_sort_rank_column_issues(
    stage: "SortRankStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    cols = resolve_input_columns(inputs, 0)
    config = stage.sort_rank
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field="sort_rank.keys", col=key.column, cols=sorted(cols))
        for key in config.keys
        if key.column not in cols
    ]
    if config.rank_column and config.rank_column in cols:
        issues.append(
            f"stage '{stage.id}': sort_rank.rank_column '{config.rank_column}' already "
            f"exists on its input — a rank is a new column, never an overwrite"
        )
    return issues


def find_sort_rank_signature_issues(
    stage: "SortRankStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    """Sorting rewrites no cell: produces is the input's schema, plus the rank column if named."""
    upstream = inputs[0].table_schema
    rank_column = stage.sort_rank.rank_column
    produced = {column.name: column for column in stage.signature.produces}
    issues = [
        f"stage '{stage.id}': signature produces vs input `{inputs[0].id}` — {reason}"
        for reason in TableSchema(
            columns=[c for c in stage.signature.produces if c.name != rank_column]
        ).find_unsatisfied_columns(upstream)
    ]
    if rank_column is None:
        return issues
    declared = produced.get(rank_column)
    if declared is None:
        issues.append(
            f"stage '{stage.id}': sort_rank adds column '{rank_column}', which signature "
            f"produces does not declare"
        )
    elif declared.type != RANK_COLUMN_TYPE:
        issues.append(
            f"stage '{stage.id}': signature produces rank column '{rank_column}' as "
            f"{declared.type!r} — a 1-based position is {RANK_COLUMN_TYPE!r}"
        )
    return issues


def find_rank_column(stage: "SortRankStage") -> Column | None:
    name = stage.sort_rank.rank_column
    if name is None:
        return None
    return next((c for c in stage.signature.produces if c.name == name), None)

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "sort_rank": StageTypeSpec(
        summary="Order rows by stated keys, optionally numbering them 1..n in a new column.",
        signature_form="replaces",
        blocks=["sort_rank"],
        requires_inputs=True,
        min_inputs=1,
        required=["keys"],
        optional=["rank_column"],
        notes=(
            "Takes exactly ONE input and changes no cell — it decides what order the rows "
            "are in, and may add one column holding each row's 1-based position. "
            "`produces` restates the input's columns, plus that column when `rank_column` "
            "is set.\n"
            "This stage ONLY orders. Working out the columns you rank on — a flag, a score, a "
            "band — is a starlark_row_function ahead of it. Two stages, because a reviewer "
            "checking the ordering rule should not have to read the scoring rule to find it.\n"
            "For a column whose ranking is not its alphabetical order, state the order: "
            "`{column: severity_tier, order: [T1, T2, T3]}`. A value outside that list stops "
            "the run — a tier the rule never anticipated must not be silently ranked last.\n"
            "The runtime records where each row moved from, so sorting does not cost the "
            "trace. A python_frame_function doing the same sort does."
        ),
    ),
}
