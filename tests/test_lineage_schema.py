"""One schema on every sidecar, whatever its content."""
from __future__ import annotations

import pyarrow as pa
import pytest

from app.core.frames import read_frame_table, write_frame_table
from app.runtime.lineage import (
    explicit_lineage,
    LINEAGE_SCHEMA,
    EdgeKind,
    RowLineage,
    RowParent,
    lineage_sidecar_path,
)


def _written(lineage: RowLineage, tmp_path) -> pa.Table:
    (tmp_path / "outputs").mkdir(parents=True, exist_ok=True)
    path = lineage_sidecar_path(tmp_path, "a_stage")
    write_frame_table(lineage.to_table(), path)
    return read_frame_table(path)


def test_a_sidecar_with_no_rows_still_carries_every_typed_column(tmp_path) -> None:
    table = _written(RowLineage([]), tmp_path)
    assert table.num_rows == 0
    assert table.schema.equals(LINEAGE_SCHEMA)


def test_a_sidecar_whose_parents_name_no_columns_types_the_column_list(tmp_path) -> None:
    lineage = explicit_lineage([[RowParent("upstream", 3)], [RowParent("upstream", 7)]])
    table = _written(lineage, tmp_path)
    assert table.schema.equals(LINEAGE_SCHEMA)
    assert table.column("_trace_source_columns").to_pylist() == [[[]], [[]]]


def test_a_sidecar_naming_columns_has_the_same_schema_as_one_that_does_not(tmp_path) -> None:
    named = explicit_lineage([[RowParent("up", 0, EdgeKind.contribution.value, ("total",))]])
    bare = explicit_lineage([[RowParent("up", 0)]])
    assert _written(named, tmp_path).schema.equals(_written(bare, tmp_path).schema)


def test_a_row_ordinal_that_is_not_an_integer_is_refused_rather_than_written() -> None:
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError, TypeError)):
        explicit_lineage([[RowParent("up", "seven")]]).to_table()  # type: ignore[arg-type]
