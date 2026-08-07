"""sort_rows stage: the config block, plus the output-side check — reordering its one
input's rows changes nothing about them, so its signature declares reads only."""
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from app.models.schema import StageConfig, _Base
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.signature import ExtendsSignature
from app.models.stages.stage_base import StageBase, StageInput, StageType

_DIRECTION_DESCRIPTION = (
    "`ascending` (smallest first) or `descending`. Rows that tie on every key keep "
    "their input order — the sort is stable, so an order is reproducible."
)

_NULLS_DESCRIPTION = (
    "Where missing values land: `last` (the default) or `first`. A null is not a "
    "value that sorts low — it is the absence of one, so its place is stated, "
    "never inferred from the direction."
)

_KEYS_DESCRIPTION = (
    "The sort keys, most significant FIRST: rows are ordered by the first key, ties "
    "broken by the second, and so on. Each entry names one column of the input and "
    "carries its own direction and nulls placement, so `state` ascending then "
    "`filed_on` descending is one stage."
)


class SortKey(_Base):
    """One key of the sort: which column, which way it runs, and where its nulls land."""

    column: str = Field(
        min_length=1,
        description="Column of the input to order by. A name it does not carry stops the stage.",
    )
    direction: Literal["ascending", "descending"] = Field(
        default="ascending", description=_DIRECTION_DESCRIPTION
    )
    nulls: Literal["first", "last"] = Field(
        default="last", description=_NULLS_DESCRIPTION
    )


class SortConfig(StageConfig):
    """sort_rows config: the ordered keys, and nothing else — a sort is its own account."""

    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"keys"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    keys: list[SortKey] = Field(min_length=1, description=_KEYS_DESCRIPTION)

    @model_validator(mode="after")
    def _no_repeated_column(self) -> "SortConfig":
        seen: set[str] = set()
        for key in self.keys:
            if key.column in seen:
                raise ValueError(
                    f"column `{key.column}` appears twice in `keys` — the second "
                    "cannot break a tie the first already settled"
                )
            seen.add(key.column)
        return self


class SortRowsStage(StageBase):
    type: Literal[StageType.sort_rows]
    sort: SortConfig
    # Exactly one input: an order is over one frame's rows, and two inputs is a
    # join or a union.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"sort": self.sort}

    def find_config_column_issues(self) -> list[str]:
        return find_sort_column_issues(self)

    def find_signature_config_issues(self) -> list[str]:
        signature = self.signature
        assert signature is not None  # find_signature_config_issues runs only with one
        if signature.adds or signature.rewrites:
            return [
                f"stage '{self.id}': sort_rows reorders rows and rewrites none of "
                f"them — its signature declares reads only, never adds or rewrites"
            ]
        return []

    def describe_sort_order(self) -> str:
        """The order in one line, for a surface showing what this stage did to a row."""
        return ", ".join(
            f"{key.column} {key.direction} (nulls {key.nulls})" for key in self.sort.keys
        )


def find_sort_column_issues(stage: "SortRowsStage") -> list[str]:
    """Every `keys` entry naming a column the single input cannot supply."""
    cols = resolve_input_columns(stage, 0)
    return [
        COLUMN_ISSUE.format(
            sid=stage.id, field=f"sort.keys[{position}].column",
            col=key.column, cols=sorted(cols),
        )
        for position, key in enumerate(stage.sort.keys)
        if key.column not in cols
    ]


# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "sort_rows": NodeTypeSpec(
        summary="Reorder rows by one or more columns. Same rows, same columns, new order.",
        signature_form="extends",
        blocks=["sort"],
        requires_inputs=True,
        min_inputs=1,
        required=["keys"],
        optional=[],
        notes=(
            "Takes exactly ONE input and emits exactly the rows it was given, every "
            "column unchanged — only the order differs, so the signature never adds or "
            "rewrites. `keys` is a list ordered most-significant-first, each entry a "
            "`column` plus its own `direction` (ascending/descending) and `nulls` "
            "(first/last), which is how a stage sorts by several columns running "
            "different ways. Rows tying on every key keep their input order. To order "
            "by something no column holds, add the column first — a "
            "starlark_row_function that computes it, then a sort on that column — so "
            "the value the order rests on is in the output where a reviewer can check "
            "it. Sorting is not filtering and not grouping: use filter_rows to drop "
            "rows and aggregate to combine them."
        ),
    ),
}
