"""sort_rows stage: the config block, plus the output-side check — reordering its one
input's rows changes nothing about them, so its signature declares reads only."""
from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import Field, model_validator

from app.models.schema import StageConfig, _Base
from app.models.stages.code import (
    CORNER_CASES_DESCRIPTION,
    SUMMARY_DESCRIPTION,
    CornerCase,
)
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.signature import ExtendsSignature
from app.models.stages.stage_base import StageBase, StageInput, StageType
from app.models.stages.starlark import validate_starlark_function_code
from app.models.stages.warnings import CompilerWarning, warn

# The name a Starlark sort key falls back to, at write-time validation and at
# execution alike — one definition so the two layers cannot drift apart.
SORT_KEY_FUNCTION_NAME = "sort_key"

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
    "`filed_on` descending is one stage. This is the form to use; reach for `code` "
    "only when the order is genuinely not a function of any column."
)

_CODE_DESCRIPTION = (
    "Inline Starlark defining `sort_key(row)`, returning a LIST of values to order "
    "by, most significant first: `def sort_key(row): return [row[\"state\"], "
    "-len(row[\"filings\"])]`. Every row must return a list of the same length — a "
    "ragged key stops the stage rather than being padded. `direction` and `nulls` "
    "apply to the computed key. Prefer `keys`: a computed key is invisible to a "
    "reader of the workflow, and an order the data itself implies is better "
    "written as a starlark_row_function that ADDS the column, followed by a plain "
    "sort on it — then the number the order rests on is in the output, reviewable. "
    "`refuse(\"reason\")` stops the stage: a sort cannot drop the row it cannot key."
)

_FUNCTION_DESCRIPTION = (
    f"Name of the key function to call within `code`, defaulting to "
    f"`{SORT_KEY_FUNCTION_NAME}`. Set it only when the function is not called "
    f"`{SORT_KEY_FUNCTION_NAME}`."
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
    """sort_rows config: EITHER `keys` (declarative, preferred) OR `code` (a Starlark key)."""

    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"keys", "code", "function", "direction", "nulls"}
    )
    # `direction`/`nulls` belong to the computed key, so they change what this
    # stage computes; the two below describe it to a reader — see
    # StageBase.compute_definition_fingerprint.
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "corner_cases"})

    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    corner_cases: list[CornerCase] = Field(
        default_factory=list, description=CORNER_CASES_DESCRIPTION
    )
    keys: list[SortKey] = Field(default_factory=list, description=_KEYS_DESCRIPTION)
    code: Optional[str] = Field(default=None, description=_CODE_DESCRIPTION)
    function: Optional[str] = Field(default=None, description=_FUNCTION_DESCRIPTION)
    direction: Literal["ascending", "descending"] = Field(
        default="ascending",
        description=f"Direction of the `code` key. {_DIRECTION_DESCRIPTION}",
    )
    nulls: Literal["first", "last"] = Field(
        default="last",
        description=f"Nulls placement within the `code` key. {_NULLS_DESCRIPTION}",
    )

    @model_validator(mode="after")
    def _one_way_to_order(self) -> "SortConfig":
        if bool(self.keys) == bool(self.code):
            raise ValueError(
                "set exactly one of `keys` (columns to order by) or `code` (a Starlark "
                "key function) — an order stated twice is an order nobody can read"
            )
        return _validate_chosen_form(self)


def _validate_chosen_form(block: "SortConfig") -> "SortConfig":
    """Whichever of the two forms was chosen, held to its own rules."""
    if block.code is not None:
        validate_starlark_function_code(
            block.code, block.function, default_name=SORT_KEY_FUNCTION_NAME
        )
        return block
    if block.function is not None:
        raise ValueError("`function` names a function inside `code`, but no `code` is set")
    if {"direction", "nulls"} & block.model_fields_set:
        raise ValueError(
            "`direction` and `nulls` describe the `code` key — with `keys`, each entry "
            "carries its own, so a stage can sort one column up and the next one down"
        )
    _refuse_repeated_columns(block.keys)
    return block


def _refuse_repeated_columns(keys: list[SortKey]) -> None:
    seen: set[str] = set()
    for key in keys:
        if key.column in seen:
            raise ValueError(
                f"column `{key.column}` appears twice in `keys` — the second cannot "
                "break a tie the first already settled"
            )
        seen.add(key.column)


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

    def find_authored_code_block(self) -> Optional[SortConfig]:
        # A declarative sort IS its own account of itself.
        return self.sort if self.sort.code is not None else None

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        return find_sort_warnings(self)

    def describe_sort_order(self) -> str:
        """The order in one line, for a surface showing what this stage did to a row."""
        if self.sort.code is not None:
            return self.sort.code
        return ", ".join(
            f"{key.column} {key.direction} (nulls {key.nulls})" for key in self.sort.keys
        )


def find_sort_column_issues(stage: "SortRowsStage") -> list[str]:
    """Every `keys` entry naming a column the single input cannot supply."""
    cols = resolve_input_columns(stage, 0)
    # The `code` form names no column, so it resolves to nothing here — what a
    # key function reads is answered by the row it is handed at run time.
    return [
        COLUMN_ISSUE.format(
            sid=stage.id, field=f"sort.keys[{position}].column",
            col=key.column, cols=sorted(cols),
        )
        for position, key in enumerate(stage.sort.keys)
        if key.column not in cols
    ]


def find_sort_warnings(stage: "SortRowsStage") -> list[CompilerWarning]:
    """Warnings about `stage.sort` — raised here and only here, since this module owns it."""
    if stage.sort.code is not None and not (stage.sort.summary or "").strip():
        return [warn(stage, "undescribed",
                     "a computed sort key with no plain-language description — "
                     "reviewable only by reading its code")]
    return []


# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "sort_rows": NodeTypeSpec(
        summary="Reorder rows by one or more columns. Same rows, same columns, new order.",
        signature_form="extends",
        blocks=["sort"],
        requires_inputs=True,
        min_inputs=1,
        required=["keys"],
        optional=["code", "function", "direction", "nulls", "summary"],
        notes=(
            "Takes exactly ONE input and emits exactly the rows it was given, every "
            "column unchanged — only the order differs, so the signature never adds or "
            "rewrites. USE `keys`: a list ordered most-significant-first, each entry a "
            "`column` plus its own `direction` (ascending/descending) and `nulls` "
            "(first/last), which is how a stage sorts by several columns running "
            "different ways. Rows tying on every key keep their input order. "
            "AVOID `code`, the Starlark alternative: it is accepted, but an order "
            "computed inside the sort is invisible to whoever reads the workflow, and "
            "almost every case wanting it is better written as a starlark_row_function "
            "that adds the column to sort on, followed by a plain `keys` sort on that "
            "column — which leaves the number the order rests on in the output where a "
            "reviewer can check it. Reach for `code` only when the order genuinely "
            "cannot be a column. Sorting is not filtering and not grouping: use "
            "filter_rows to drop rows and aggregate to combine them."
        ),
    ),
}
