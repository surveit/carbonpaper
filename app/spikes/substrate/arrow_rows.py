"""Spike (issue #194): the `python_row_function` boundary over Arrow types.

`app.runtime.stages.execution._run_row_mapper` hands an authored function
`df.to_dict("records")` off a numpy-backed frame, and reassembles the results
with `pd.DataFrame(out_rows)`. Both ends leak numpy's type system into
author-written Python:

    in   nullable str  → float('nan')   (not None)
    in   list[str]     → numpy.ndarray  (not list)
    out  zero rows     → a frame with NO columns

Every generator guard PR #182 added to authored code — `pd.isna(x)`,
`list(x) if x is not None else []` — exists to undo one of these. Over an Arrow
table none of them are reachable: `Table.to_pylist()` produces Python `None`,
`list`, `dict`; and `Table.from_pylist(rows, schema=...)` produces a typed,
correctly-columned table from an empty list, because the schema is declared
rather than inferred from rows.

`run_row_function` is the Arrow-native driver. It keeps the production driver's
two structural guarantees — exactly one output slot per input row, assembled in
input order, so grain and order hold by construction — and adds a third the
production driver cannot offer: the output frame's columns and types come from
the stage's declared `output_schema`, so an empty stage output is still a
schema-shaped stage output.

The *host* question the issue raises ("Polars, or Arrow-backed pandas?") is
deliberately kept out of the driver: it runs on an Arrow table, and
`to_polars` / `to_arrow_pandas` are the two one-line adapters at the edge.
`tests/spikes/test_null_semantics.py` runs the same authored function through
both and finds no behavioural difference at the row boundary — the choice is an
ergonomics and rewrite-cost question for `python_frame_function`, not a
correctness one for `python_row_function`.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

import pandas as pd
import polars as pl
import pyarrow as pa

from app.models import TableSchema

from .arrow_types import arrow_schema_for

# One row handed to (and returned by) an authored transform: column → value.
# Deliberately the same shape as the production driver's `Row`, so an authored
# function is portable between the two without edits.
Row = dict[str, Any]


def rows_from_arrow(table: pa.Table) -> list[Row]:
    """The rows an authored function sees: Python `None` for a null, `list` for
    a list column, `dict` for a struct. `to_pylist` is the whole conversion —
    Arrow's null is out-of-band, so nothing has to be re-typed to make room for
    it the way a numpy float column does."""
    return table.to_pylist()


def rows_from_numpy_pandas(frame: pd.DataFrame) -> list[Row]:
    """What the production driver hands an authored function today, reproduced
    here so the two boundaries can be compared in one test rather than
    described. `str(k)` mirrors the production driver's column-label pinning."""
    return [{str(k): v for k, v in record.items()} for record in frame.to_dict("records")]


def frame_from_rows(rows: Sequence[Row], schema: TableSchema | None) -> pa.Table:
    """Assemble transform output into an Arrow table.

    With a declared `schema` the result carries exactly its columns and types —
    including when `rows` is empty, which is the case the numpy-pandas
    assembly cannot represent. Without one, types are inferred from the rows
    (and an empty list really does mean an empty table), so the schema is what
    buys the guarantee, not Arrow alone.
    """
    if schema is None:
        return pa.Table.from_pylist(list(rows))
    arrow_schema = arrow_schema_for(schema)
    declared = set(arrow_schema.names)
    projected = [{name: row.get(name) for name in arrow_schema.names} for row in rows]
    _reject_undeclared(rows, declared)
    return pa.Table.from_pylist(projected, schema=arrow_schema)


def run_row_function(
    function: Callable[[Row], Row],
    table: pa.Table,
    output_schema: TableSchema | None = None,
) -> pa.Table:
    """Map `function` over `table`'s rows, in input order, into an Arrow table.

    One result slot per input row, filled by index — the same construction that
    makes the production row driver grain-and-order preserving. A non-dict
    return fails loudly, naming the row, exactly as the production driver does.
    """
    rows = rows_from_arrow(table)
    results: list[Row] = []
    for index, row in enumerate(rows):
        result = function(row)
        if not isinstance(result, dict):
            raise ValueError(
                f"row mapper must return one dict per row, got "
                f"{type(result).__name__} for row {index}"
            )
        results.append(result)
    return frame_from_rows(results, output_schema)


def to_polars(table: pa.Table) -> pl.DataFrame:
    """Zero-copy view of an Arrow table as a Polars frame — the `python_frame_
    function` host under option A of the issue's table.

    The constructor rather than `pl.from_arrow`, whose return type is
    `DataFrame | Series` (it also accepts a bare Arrow array); a table always
    becomes a frame, and saying so here keeps the seam single-typed."""
    return pl.DataFrame(table)


def to_arrow_pandas(table: pa.Table) -> pd.DataFrame:
    """Arrow-backed pandas view (`pd.ArrowDtype` columns) — option B: the same
    pandas API authored code already uses, over Arrow storage and `pd.NA`."""
    return table.to_pandas(types_mapper=pd.ArrowDtype)


def from_arrow_pandas(frame: pd.DataFrame) -> pa.Table:
    """Back to Arrow from an Arrow-backed pandas frame."""
    return pa.Table.from_pandas(frame, preserve_index=False)


def _reject_undeclared(rows: Sequence[Row], declared: set[str]) -> None:
    """Fail loudly on a key no declared column covers.

    The production driver's `_project_onto_declared_columns` records dropped
    columns on the run context instead. That is the right production behaviour
    and this is not a claim against it — the spike has no context to record on,
    and a silent drop in a prototype would hide exactly the mismatches the
    prototype exists to find.
    """
    undeclared = sorted({key for row in rows for key in row if key not in declared})
    if undeclared:
        raise ValueError(
            f"row mapper produced column(s) no output_schema declares: {undeclared}"
        )
