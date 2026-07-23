"""Per-stage-type validation helpers that are too bulky to live inline on the
`Stage` model. Each module here holds the checks specific to one family of stage
types; `app.models.stage` imports them back into its model validators."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from app.models.schema import TableSchema
from app.models.stages.aggregate import (
    fill_aggregate_output_columns,
    find_aggregate_column_issues,
    find_aggregate_output_issues,
)
from app.models.stages.human_review_queue import find_queue_filter_column_issues
from app.models.stages.join import (
    fill_join_output_columns,
    find_join_column_issues,
    find_join_output_issues,
)
from app.models.stages.llm_transform import find_llm_prompt_column_issues
from app.models.stages.publish import find_publish_column_issues

if TYPE_CHECKING:
    from app.models.schema import Column
    from app.models.stage import Stage

# Dispatch keyed by the plain value string, not the StageType member — like
# stage.py's `_TYPE_SPEC`, this must survive `use_enum_values`: at runtime
# `stage.type` is a str, and a str-enum member hashes by *name*
# (StageType.join_ hashes as "join_", not "join"), so a member-keyed dict would
# silently miss the lookup.
_VALIDATORS: dict[str, Callable[["Stage"], list[str]]] = {
    "join": find_join_column_issues,
    "aggregate": find_aggregate_column_issues,
    "publish": find_publish_column_issues,
    "llm_transform": find_llm_prompt_column_issues,
    "human_review_queue": find_queue_filter_column_issues,
}


def find_config_column_issues(stage: "Stage") -> list[str]:
    """Every config-column issue for `stage`'s own type: [] for a type with
    no such check (e.g. input_data — a source has no upstream edge to resolve
    against) or one whose config's column references all resolve."""
    fn = _VALIDATORS.get(stage.type)
    return fn(stage) if fn else []


# Dispatch for the output-side check: only the stage types whose output is
# fixed entirely by config (as opposed to e.g. an authored python function)
# have a derivation to check a declared output_schema against.
_OUTPUT_VALIDATORS: dict[str, Callable[["Stage"], list[str]]] = {
    "aggregate": find_aggregate_output_issues,
    "join": find_join_output_issues,
}


def find_output_schema_issues(stage: "Stage") -> list[str]:
    """Every way `stage`'s declared output_schema is undeliverable by its own
    handle: [] for a type with no derivation (its internals, not its config,
    fix the output) or one whose declared columns are all producible."""
    fn = _OUTPUT_VALIDATORS.get(stage.type)
    return fn(stage) if fn else []


# Dispatch for the auto-fill seam: only the stage types whose output is fixed
# entirely by config have a derivation that can manufacture a missing
# output_schema outright, rather than merely check a declared one.
_FILL_DERIVATIONS: dict[str, Callable[["Stage"], "list[Column] | None"]] = {
    "aggregate": fill_aggregate_output_columns,
    "join": fill_join_output_columns,
}


def fill_output_schema(stage: "Stage") -> "Stage":
    """`stage` itself, unless its own type has a fill derivation, it declares
    no output_schema, and that derivation fully resolves one — in which case
    a copy of `stage` carrying the derived `TableSchema` (no primary_key: a
    join or aggregate can change or lose the input's grain, so the fill never
    guesses one). Never overwrites an authored output_schema, and never
    writes a partial fill: an underivable stage is returned untouched."""
    if stage.output_schema is not None:
        return stage
    fn = _FILL_DERIVATIONS.get(stage.type)
    if fn is None:
        return stage
    columns = fn(stage)
    if columns is None:
        return stage
    return stage.model_copy(update={"output_schema": TableSchema(columns=columns)})
