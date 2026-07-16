"""The row driver and handler shapes: grain and order hold by construction —
the mapper never sees the frame, one result slot exists per input row, and
results are written back by input index (also under concurrency)."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from app.core.models import Stage
from app.core.models.stage import StageType
from app.runtime.stages.execution import (
    FrameHandler,
    RowMapHandler,
    SourceHandler,
    check_registry_matches_model,
)


def _row_stage(output_schema=None):
    kw = {
        "id": "t", "name": "t", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    }
    if output_schema is not None:
        kw["output_schema"] = output_schema
    return Stage.model_validate(kw)


def _two_input_stage():
    return Stage.model_validate({
        "id": "t2", "name": "t2", "type": "python_frame_function",
        "inputs": [{"id": "a"}, {"id": "b"}],
        "function": {"kind": "inline", "code": "def transform(a, b):\n    return a\n"},
    })


def test_row_driver_maps_in_input_order():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: {"x": row["x"], "y": row["x"] * 10})
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, {})
    assert list(out["x"]) == [1, 2, 3]
    assert list(out["y"]) == [10, 20, 30]


def test_row_driver_preserves_order_under_parallelism():
    # Later rows finish FIRST (reverse sleeps); index write-back must still
    # reassemble in input order.
    def make_mapper(stage, ctx):
        def map_row(row):
            time.sleep(0.002 * (8 - row["x"]))
            return {"x": row["x"]}
        return map_row

    handler = RowMapHandler(make_mapper=make_mapper, parallelism=4)
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": list(range(8))})}, {})
    assert list(out["x"]) == list(range(8))


def test_row_driver_is_one_to_one():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, {})
    assert len(out) == 2


def test_row_driver_rejects_non_dict_result():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: 42)
    with pytest.raises(ValueError, match="one dict per row"):
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1]})}, {})


def test_row_driver_rejects_multiple_inputs():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    frames = {"a": pd.DataFrame({"x": [1]}), "b": pd.DataFrame({"x": [1]})}
    with pytest.raises(ValueError, match="exactly one input"):
        handler.execute(_two_input_stage(), frames, {})


def test_row_driver_empty_input():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    out = handler.execute(_row_stage(),
                          {"src": pd.DataFrame({"x": pd.Series([], dtype="int64")})}, {})
    assert len(out) == 0


def test_row_driver_collects_row_errors_without_dropping_the_stage():
    from app.runtime.stages.execution import ROW_ERROR_KEY

    def make_mapper(stage, ctx):
        def map_row(row):
            if row["x"] == 2:
                return {"x": row["x"], ROW_ERROR_KEY: "boom"}
            return {"x": row["x"], "y": row["x"] * 10}
        return map_row

    handler = RowMapHandler(make_mapper=make_mapper)
    ctx: dict = {}
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, ctx)
    assert len(out) == 3                                    # stage completes, all rows kept
    assert ctx["row_errors"]["t"] == [{"row": 1, "message": "boom"}]


def test_row_driver_collects_multiple_row_errors_in_ascending_row_order():
    from app.runtime.stages.execution import ROW_ERROR_KEY

    def make_mapper(stage, ctx):
        def map_row(row):
            if row["x"] in (10, 30):
                return {"x": row["x"], ROW_ERROR_KEY: f"boom-{row['x']}"}
            return {"x": row["x"], "y": row["x"] * 10}
        return map_row

    handler = RowMapHandler(make_mapper=make_mapper)
    ctx: dict = {}
    # Rows at positions 0 and 2 of a 3-row input fail; position 1 succeeds.
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [10, 20, 30]})}, ctx)
    assert len(out) == 3
    assert ctx["row_errors"]["t"] == [
        {"row": 0, "message": "boom-10"},
        {"row": 2, "message": "boom-30"},
    ]


def test_row_driver_projects_to_declared_columns():
    schema = {"columns": [{"name": "x", "type": "int"}, {"name": "score", "type": "int"}]}
    handler = RowMapHandler(
        make_mapper=lambda stage, ctx: lambda row: {"x": row["x"], "score": 1, "extra": "drop me"},
        project_output_to_declared=True,
    )
    ctx: dict = {}
    out = handler.execute(_row_stage(output_schema=schema),
                          {"src": pd.DataFrame({"x": [1]})}, ctx)
    assert list(out.columns) == ["x", "score"]
    assert ctx["dropped_columns"]["t"] == ["extra"]


def test_source_handler_reads_without_frames():
    handler = SourceHandler(read=lambda stage, ctx: pd.DataFrame({"k": ["a"]}))
    out = handler.execute(_row_stage(), {}, {})
    assert list(out["k"]) == ["a"]


def test_frame_handler_receives_frames():
    handler = FrameHandler(apply=lambda stage, inputs, ctx: inputs["src"].head(1))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, {})
    assert len(out) == 1


def _registry(llm_shape):
    frame = FrameHandler(apply=lambda stage, inputs, ctx: pd.DataFrame())
    return {
        StageType.input_data: SourceHandler(read=lambda stage, ctx: pd.DataFrame()),
        StageType.python_row_function: RowMapHandler(make_mapper=lambda s, c: lambda r: r),
        StageType.llm_transform: llm_shape,
        StageType.python_frame_function: frame,
        StageType.join_: frame,
        StageType.aggregate: frame,
        StageType.human_review_queue: frame,
        StageType.publish: frame,
    }


def test_check_registry_accepts_shapes_matching_the_model():
    good = _registry(RowMapHandler(make_mapper=lambda s, c: lambda r: r))
    check_registry_matches_model(good)  # must not raise


def test_check_registry_rejects_shape_disagreeing_with_model():
    bad = _registry(FrameHandler(apply=lambda stage, inputs, ctx: pd.DataFrame()))
    with pytest.raises(RuntimeError, match="is registered as"):
        check_registry_matches_model(bad)
