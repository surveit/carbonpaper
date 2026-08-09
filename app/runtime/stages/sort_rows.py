"""Handler for the sort_rows stage type: one permutation of the input's rows.

The permutation IS this stage's lineage, so it is handed to the runtime rather
than left to be inferred — nothing about an output row's position tells you
which input row it was.
"""
from __future__ import annotations

import numbers
from typing import Any, NamedTuple

import pandas as pd

from app.core.frames import list_rows
from app.models import Stage
from app.models.stages.sort_rows import SORT_KEY_FUNCTION_NAME, SortConfig, SortRowsStage

from ..context import RunContext
from ..lineage import attach_row_lineage, single_parent_lineage
from ..starlark_code import compile_starlark_function
from .execution import narrow_stage
from .starlark_marshal import marshal_row_for_starlark


class _SortVector(NamedTuple):
    """One key's values down the frame, with the two placements that order them."""

    values: pd.Series
    direction: str
    nulls: str


def handle_sort_rows(
    stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
) -> pd.DataFrame:
    """The input's rows in the authored order, carrying where each one came from."""
    input_id = stage.inputs[0].id
    src = inputs[input_id]
    order = _find_row_order(narrow_stage(stage, SortRowsStage).sort, src, stage.id)
    return attach_row_lineage(
        src.iloc[order].reset_index(drop=True),
        single_parent_lineage(input_id, order),
    )


def _find_row_order(cfg: SortConfig, src: pd.DataFrame, stage_id: str) -> list[int]:
    """`src`'s row positions, sorted — the whole ordering decision, in one place."""
    if len(src) == 0:
        return []
    scratch = pd.DataFrame(index=pd.RangeIndex(len(src)))
    by: list[str] = []
    ascending: list[bool] = []
    for position, vector in enumerate(_build_sort_vectors(cfg, src, stage_id)):
        # An is-null column ahead of each key is what makes `nulls` PER KEY:
        # pandas' own na_position is one setting for the whole sort.
        _refuse_uncomparable_key(vector.values, position, stage_id)
        missing, key = f"_null_{position}", f"_key_{position}"
        scratch[missing] = pd.isna(vector.values).to_numpy()
        scratch[key] = vector.values.to_numpy()
        by += [missing, key]
        ascending += [vector.nulls == "last", vector.direction == "ascending"]
    return _sorted_positions(scratch, by, ascending, stage_id)


def _refuse_uncomparable_key(values: pd.Series, position: int, stage_id: str) -> None:
    """Refuse a key mixing values nothing can order — pandas would invent an order instead."""
    if values.dtype != object:
        return
    # Sorting on SEVERAL columns (the is-null column plus the key) puts pandas
    # on its lexsort path, which factorizes an object column rather than
    # comparing it: `['x', 2]` comes back ordered, with the ordering resting on
    # nothing. A single-column sort raises instead; this restores that.
    kinds = {_comparison_kind(value) for value in values if not _is_null(value)}
    if len(kinds) > 1:
        raise ValueError(
            f"sort_rows stage {stage_id}: sort key {position} holds {sorted(kinds)} "
            "values, which cannot be compared with each other — sort on a key of one "
            "kind, or map it to one first"
        )


def _comparison_kind(value: Any) -> str:
    """What `value` can be ordered against: every number orders against every other."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, numbers.Number):
        return "number"
    if isinstance(value, str):
        return "text"
    return type(value).__name__


def _is_null(value: Any) -> bool:
    # pd.isna answers elementwise for a list/array, which is not a truth value.
    return value is None or (not isinstance(value, (list, tuple)) and bool(pd.isna(value)))


def _sorted_positions(
    scratch: pd.DataFrame, by: list[str], ascending: list[bool], stage_id: str
) -> list[int]:
    """Stable, so rows tying on every key keep their input order and a re-run repeats it."""
    try:
        ordered = scratch.sort_values(by=by, ascending=ascending, kind="stable")
    except TypeError as exc:
        raise ValueError(
            f"sort_rows stage {stage_id}: the sort keys hold values that cannot be "
            f"compared with each other ({exc}) — sort on a column of one type"
        ) from exc
    return [int(position) for position in ordered.index]


def _build_sort_vectors(
    cfg: SortConfig, src: pd.DataFrame, stage_id: str
) -> list[_SortVector]:
    if cfg.code is not None:
        return _compute_key_vectors(cfg, src, stage_id)
    return [
        _SortVector(_read_sort_column(src, key.column, stage_id), key.direction, key.nulls)
        for key in cfg.keys
    ]


def _read_sort_column(src: pd.DataFrame, column: str, stage_id: str) -> pd.Series:
    if column not in src.columns:
        raise ValueError(
            f"sort_rows stage {stage_id}: no column `{column}` to sort on — the "
            f"input carries {sorted(str(c) for c in src.columns)}"
        )
    return src[column]


def _compute_key_vectors(
    cfg: SortConfig, src: pd.DataFrame, stage_id: str
) -> list[_SortVector]:
    """The Starlark key run down the frame, then transposed into one vector per position."""
    handle = compile_starlark_function(
        cfg.code or "", cfg.function or SORT_KEY_FUNCTION_NAME, SORT_KEY_FUNCTION_NAME
    )
    if handle is None:
        raise ValueError(
            f"sort_rows stage {stage_id}: code does not define "
            f"`{cfg.function or SORT_KEY_FUNCTION_NAME}`"
        )
    keys = [
        _read_computed_key(handle(marshal_row_for_starlark(row)), index, stage_id)
        for index, row in enumerate(list_rows(src))
    ]
    return [
        _SortVector(
            pd.Series([key[position] for key in keys], dtype=object),
            cfg.direction,
            cfg.nulls,
        )
        for position in range(_find_key_width(keys, stage_id))
    ]


def _read_computed_key(result: Any, index: int, stage_id: str) -> list[Any]:
    if not isinstance(result, (list, tuple)):
        raise ValueError(
            f"sort_rows stage {stage_id}: {SORT_KEY_FUNCTION_NAME} must return a list "
            f"of values to order by, got {type(result).__name__} for row {index}"
        )
    return list(result)


def _find_key_width(keys: list[list[Any]], stage_id: str) -> int:
    """How many values every row's key holds; a key that is not one width is refused."""
    widths = {len(key) for key in keys}
    if len(widths) > 1:
        raise ValueError(
            f"sort_rows stage {stage_id}: {SORT_KEY_FUNCTION_NAME} returned keys of "
            f"{sorted(widths)} values — a ragged key orders rows on different things"
        )
    width = widths.pop()
    if width == 0:
        raise ValueError(
            f"sort_rows stage {stage_id}: {SORT_KEY_FUNCTION_NAME} returned an empty "
            "list — there is nothing to order the rows by"
        )
    return width
