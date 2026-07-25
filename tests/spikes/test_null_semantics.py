"""Spike (issue #194): the measurement — one authored transform, three substrates.

The issue names three symptoms of the numpy-backed-pandas tax. Each is
reproduced here against the *production* row boundary
(`app.runtime.stages.execution._run_row_mapper`'s `to_dict("records")` /
`pd.DataFrame(rows)`, mirrored by `arrow_rows.rows_from_numpy_pandas`) and then
shown absent from the Arrow boundary — with one transform function, written the
way an author (or a generator) would naturally write it, shared by both.

The round trip through parquet is not incidental: the runner persists every
stage output as `outputs/<stage>.parquet` and the next stage reads it back, so
that is where a `list[str]` column becomes `ndarray` in production.
"""
from __future__ import annotations

import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.models import TableSchema
from app.spikes.substrate.arrow_rows import (
    frame_from_rows,
    rows_from_arrow,
    rows_from_numpy_pandas,
    to_arrow_pandas,
    to_polars,
)
from app.spikes.substrate.arrow_types import arrow_schema_for


def _schema() -> TableSchema:
    """A registry-shaped schema: a nullable name, a list of tags, a nullable score."""
    return TableSchema.model_validate({
        "columns": [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "name", "type": "str"},
            {"name": "tags", "type": "list[str]"},
            {"name": "score", "type": "float"},
        ]
    })


def _rows() -> list[dict]:
    return [
        {"id": 1, "name": "acme ltd", "tags": ["sanctioned", "eu"], "score": 0.9},
        {"id": 2, "name": None, "tags": [], "score": None},
    ]


def _arrow_table(tmp_path) -> pa.Table:
    """The fixture as the runner would persist and re-read it."""
    path = tmp_path / "stage.parquet"
    pq.write_table(pa.Table.from_pylist(_rows(), schema=arrow_schema_for(_schema())), path)
    return pq.read_table(path)


def _numpy_pandas_frame(tmp_path) -> pd.DataFrame:
    path = tmp_path / "stage.parquet"
    pq.write_table(pa.Table.from_pylist(_rows(), schema=arrow_schema_for(_schema())), path)
    return pd.read_parquet(path)


def transform(row: dict) -> dict:
    """What a stage author writes when the schema says `name: str | null` and
    `tags: list[str]`. No `pd.isna`, no `list(...)` coercion — the guards
    PR #182 taught the generator to emit exist only to survive numpy."""
    name = row["name"]
    return {
        "id": row["id"],
        "shout": name.upper() if name is not None else None,
        "tags_json": json.dumps(row["tags"]),
    }


# ── symptom 1: a nullable str arrives as float('nan') ────────────────────────

def test_numpy_pandas_hands_a_float_nan_where_the_schema_says_nullable_str(tmp_path):
    row = rows_from_numpy_pandas(_numpy_pandas_frame(tmp_path))[1]
    assert isinstance(row["name"], float)
    assert row["name"] != row["name"]  # NaN
    assert row["name"] is not None     # so `if x is None` never fires


def test_arrow_hands_none(tmp_path):
    assert rows_from_arrow(_arrow_table(tmp_path))[1]["name"] is None


# ── symptom 2: a list[str] arrives as ndarray ────────────────────────────────

def test_numpy_pandas_hands_an_ndarray_where_the_schema_says_list_str(tmp_path):
    tags = rows_from_numpy_pandas(_numpy_pandas_frame(tmp_path))[0]["tags"]
    assert not isinstance(tags, list)
    assert type(tags).__name__ == "ndarray"


def test_arrow_hands_a_list(tmp_path):
    assert rows_from_arrow(_arrow_table(tmp_path))[0]["tags"] == ["sanctioned", "eu"]


# ── the two symptoms, as an author meets them ────────────────────────────────

def test_the_same_transform_raises_on_numpy_pandas_and_runs_on_arrow(tmp_path):
    """The headline measurement. On numpy-pandas the transform raises twice —
    once per symptom — and neither failure is about the data being wrong."""
    numpy_rows = rows_from_numpy_pandas(_numpy_pandas_frame(tmp_path))

    with pytest.raises(TypeError, match="not JSON serializable"):
        transform(numpy_rows[0])            # symptom 2: ndarray in json.dumps
    with pytest.raises(AttributeError, match="upper"):
        transform(numpy_rows[1])            # symptom 1: float('nan').upper()

    assert [transform(row) for row in rows_from_arrow(_arrow_table(tmp_path))] == [
        {"id": 1, "shout": "ACME LTD", "tags_json": '["sanctioned", "eu"]'},
        {"id": 2, "shout": None, "tags_json": "[]"},
    ]


# ── symptom 3: an empty result has no columns ────────────────────────────────

def test_an_empty_result_loses_its_columns_on_pandas_and_keeps_them_on_arrow():
    out_schema = TableSchema.model_validate({
        "columns": [
            {"name": "id", "type": "int"},
            {"name": "shout", "type": "str"},
            {"name": "tags_json", "type": "str"},
        ]
    })
    assert list(pd.DataFrame([]).columns) == []

    empty = frame_from_rows([], out_schema)
    assert empty.num_rows == 0
    assert empty.schema.names == ["id", "shout", "tags_json"]


# ── the host question: Polars vs Arrow-backed pandas ─────────────────────────

def test_both_hosts_agree_with_arrow_at_the_row_boundary(tmp_path):
    """Issue #194 asks which in-memory frame to hand Python transforms. At the
    *row* grain the answer does not matter — both hosts round-trip Arrow's
    nulls and lists identically — so `python_row_function` can move to Arrow
    without deciding it. The choice only bites `python_frame_function`, where
    authored code touches the frame API itself."""
    table = _arrow_table(tmp_path)
    expected = rows_from_arrow(table)

    assert to_polars(table).rows(named=True) == expected

    arrow_pandas_rows = to_arrow_pandas(table).to_dict("records")
    assert [row["name"] for row in arrow_pandas_rows] == ["acme ltd", None]
    assert [list(row["tags"]) for row in arrow_pandas_rows] == [["sanctioned", "eu"], []]


# ── the migration hazard the spike found ─────────────────────────────────────

def test_flipping_the_substrate_changes_row_fingerprints_for_list_columns(tmp_path):
    """`compute_row_fingerprint` already collapses every pandas null form to
    JSON null, so a scalar column's identity survives the flip. A *list* column
    does not: an ndarray reaches `json.dumps(default=str)` as its numpy repr,
    a list serialises as a JSON array. Any stage whose input carries a list
    column would re-queue every cached human decision on the day the substrate
    changes — the one migration step that needs a plan, not just a flag."""
    from app.services.stage_cache import compute_row_fingerprint

    numpy_rows = rows_from_numpy_pandas(_numpy_pandas_frame(tmp_path))
    arrow_rows = rows_from_arrow(_arrow_table(tmp_path))

    scalars = ["id", "name", "score"]
    assert compute_row_fingerprint({k: numpy_rows[1][k] for k in scalars}) == \
        compute_row_fingerprint({k: arrow_rows[1][k] for k in scalars})

    assert compute_row_fingerprint(numpy_rows[0]) != compute_row_fingerprint(arrow_rows[0])


def test_arrow_backed_pandas_keeps_the_pandas_api_authors_already_use(tmp_path):
    """The migration-cost datum for `python_frame_function`: with
    `dtype_backend="pyarrow"`, existing pandas frame code keeps working while
    nulls stop being NaN. A Polars rewrite has no such property."""
    frame = to_arrow_pandas(_arrow_table(tmp_path))
    grouped = frame.assign(has_name=frame["name"].notna()).groupby("has_name").size()
    assert grouped.to_dict() == {False: 1, True: 1}
    assert str(frame["score"].dtype) == "double[pyarrow]"
    assert frame["score"].isna().tolist() == [False, True]
