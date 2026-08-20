"""sort_rank stage: the config block, plus validation that every sort key resolves
against the input and that the signature writes nothing but the rank column."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Literal, Optional, Sequence

from pydantic import Field, model_validator

from app.models.schema import StageConfig, _Base
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput

RANK_COLUMN_TYPE = "int"


class NullPlacement(str, Enum):
    first = "first"
    last = "last"


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

    nulls: Optional[NullPlacement] = Field(
        default=None,
        description=(
            "Where rows holding no value in this column go. Leave it unset for a column "
            "that cannot be null. Setting it is how a nullable column becomes sortable: "
            "unset, a null stops the run rather than being placed somewhere the rule "
            "never chose."
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
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "the cache stores a table, not the lineage sidecar this type works out, so a hit would replay the rows without their provenance"
    )
    sort_rank: SortRankConfig
    # Exactly one input: ordering one frame's rows against each other.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"sort_rank": self.sort_rank}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_sort_rank_column_issues(self, inputs)

    def find_signature_config_issues(self) -> list[str]:
        return find_sort_rank_signature_issues(self)


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
    issues.extend(_find_unplaced_null_issues(stage, inputs))
    if config.rank_column and config.rank_column in cols:
        issues.append(
            f"stage '{stage.id}': sort_rank.rank_column '{config.rank_column}' already "
            f"exists on its input — a rank is a new column, never an overwrite"
        )
    return issues


def _find_unplaced_null_issues(
    stage: "SortRankStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    """Caught at authoring time, so a nullable key never reaches a run undecided."""
    nullable = {c.name for c in inputs[0].table_schema.columns if c.nullable}
    return [
        f"stage '{stage.id}': sort_rank orders by '{key.column}', which its input may "
        f"leave null — set `nulls` on that key to say whether those rows lead or trail"
        for key in stage.sort_rank.keys
        if key.column in nullable and key.nulls is None
    ]


def find_sort_rank_signature_issues(stage: "SortRankStage") -> list[str]:
    """Ordering rewrites no cell, so the only write it may declare is the rank column."""
    signature = stage.signature
    rank_column = stage.sort_rank.rank_column
    issues = [
        f"stage '{stage.id}': sort_rank changes no cell — its signature declares "
        f"reads and, when `rank_column` is set, that one add; never rewrites"
    ] if signature.rewrites else []
    added = {column.name: column for column in signature.adds}
    if rank_column is None:
        issues.extend(
            f"stage '{stage.id}': signature adds '{name}', but sort_rank adds a column "
            f"only when `rank_column` names one"
            for name in sorted(added)
        )
        return issues
    declared = added.get(rank_column)
    if declared is None:
        issues.append(
            f"stage '{stage.id}': sort_rank adds column '{rank_column}', which signature "
            f"adds does not declare"
        )
    elif declared.type != RANK_COLUMN_TYPE:
        issues.append(
            f"stage '{stage.id}': signature adds rank column '{rank_column}' as "
            f"{declared.type!r} — a 1-based position is {RANK_COLUMN_TYPE!r}"
        )
    issues.extend(
        f"stage '{stage.id}': signature adds '{name}', which sort_rank does not produce "
        f"— it orders rows and adds only '{rank_column}'"
        for name in sorted(set(added) - {rank_column})
    )
    return issues


# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "sort_rank": StageTypeSpec(
        summary="Order rows by stated keys, optionally numbering them 1..n in a new column.",
        signature_form="extends",
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
            "A key on a column its input may leave null must say where those rows go — `nulls: last`. Without it the stage is refused when it is saved, not when it runs.\n"
            "For a column whose ranking is not its alphabetical order, state the order: "
            "`{column: severity_tier, order: [T1, T2, T3]}`. A value outside that list stops "
            "the run — a tier the rule never anticipated must not be silently ranked last.\n"
            "The runtime records where each row moved from, so sorting does not cost "
            "the trace — sorting in authored code would."
        ),
    ),
}
