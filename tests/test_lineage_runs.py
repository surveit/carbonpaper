"""A run-length sidecar: one entry per contiguous block, not per output row."""
from __future__ import annotations

import pyarrow as pa
import pytest

from app.runtime.lineage import (
    LINEAGE_SCHEMA,
    TRACE_RUN_LENGTH_KEY,
    LineageRun,
    RowLineage,
    RowParent,
    contiguous_parent_lineage,
    explicit_lineage,
)

_ROWS_PER_INPUT = 1_000


def _three_input_union() -> RowLineage:
    return RowLineage([
        LineageRun(_ROWS_PER_INPUT, [RowParent(f"month_{n}", 0)]) for n in range(3)
    ])


def test_a_union_sidecar_holds_one_entry_per_input_rather_than_per_row() -> None:
    lineage = _three_input_union()
    assert len(lineage) == 3 * _ROWS_PER_INPUT
    assert lineage.to_table().num_rows == 3


def test_each_row_of_a_run_names_the_input_row_that_sits_at_its_own_offset() -> None:
    lineage = _three_input_union()
    assert lineage.parents_of(0) == [RowParent("month_0", 0)]
    assert lineage.parents_of(_ROWS_PER_INPUT - 1) == [RowParent("month_0", 999)]
    # The row after the boundary restarts at the second input's row 0.
    assert lineage.parents_of(_ROWS_PER_INPUT) == [RowParent("month_1", 0)]
    assert lineage.parents_of(3 * _ROWS_PER_INPUT - 1) == [RowParent("month_2", 999)]


def test_iterating_a_run_gives_the_same_parents_as_asking_row_by_row() -> None:
    lineage = RowLineage([LineageRun(4, [RowParent("source", 10)])])
    assert list(lineage.iter_parents()) == [lineage.parents_of(r) for r in range(4)]
    assert lineage.parents == [[RowParent("source", 10 + k)] for k in range(4)]


def test_a_row_outside_the_lineage_is_refused_rather_than_answered() -> None:
    lineage = RowLineage([LineageRun(2, [RowParent("source", 0)])])
    with pytest.raises(IndexError):
        lineage.parents_of(2)


def test_a_run_covering_no_rows_is_refused_when_it_is_built() -> None:
    with pytest.raises(ValueError):
        LineageRun(0, [RowParent("source", 0)])


def test_a_list_of_parents_passed_where_runs_belong_fails_loudly() -> None:
    with pytest.raises(ValueError):
        RowLineage([[RowParent("source", 0)]])  # type: ignore[list-item]


def test_a_run_survives_the_round_trip_through_its_sidecar() -> None:
    lineage = _three_input_union()
    read_back = RowLineage.from_table(lineage.to_table())
    assert read_back.runs == lineage.runs
    assert read_back.parents_of(1_500) == [RowParent("month_1", 500)]


def test_shifting_moves_every_run_without_expanding_it() -> None:
    shifted = _three_input_union().shifted(7)
    assert len(shifted.runs) == 3
    assert shifted.parents_of(0) == [RowParent("month_0", 7)]
    assert shifted.parents_of(_ROWS_PER_INPUT) == [RowParent("month_1", 7)]


def test_a_windowed_stage_records_one_run_for_the_whole_window() -> None:
    lineage = contiguous_parent_lineage("upstream", 50, 20)
    assert len(lineage.runs) == 1
    assert len(lineage) == 20
    assert lineage.parents_of(19) == [RowParent("upstream", 69)]


def test_a_window_over_no_rows_records_nothing() -> None:
    assert contiguous_parent_lineage("upstream", 50, 0).runs == []


def test_a_sidecar_written_before_runs_existed_reads_one_row_per_entry() -> None:
    older = pa.table({
        "_trace_source_stage": [["source"], ["source"]],
        "_trace_source_row": [[4], [9]],
        "_trace_edge_kind": [["direct"], ["direct"]],
        "_trace_source_columns": [[[]], [[]]],
    })
    assert TRACE_RUN_LENGTH_KEY not in older.column_names
    lineage = RowLineage.from_table(older)
    assert len(lineage) == 2
    assert lineage.parents == [[RowParent("source", 4)], [RowParent("source", 9)]]


def test_one_run_per_row_still_writes_the_pinned_schema() -> None:
    table = explicit_lineage([[RowParent("source", 0)], [RowParent("source", 3)]]).to_table()
    assert table.schema.equals(LINEAGE_SCHEMA)
    assert table.column(TRACE_RUN_LENGTH_KEY).to_pylist() == [1, 1]


def test_a_sidecar_whose_run_length_is_null_is_refused_rather_than_read_as_one() -> None:
    torn = pa.table({
        "_trace_source_stage": [["source"]],
        "_trace_source_row": [[4]],
        "_trace_edge_kind": [["direct"]],
        "_trace_source_columns": [[[]]],
        TRACE_RUN_LENGTH_KEY: [None],
    }, schema=LINEAGE_SCHEMA)
    with pytest.raises(ValueError):
        RowLineage.from_table(torn)
