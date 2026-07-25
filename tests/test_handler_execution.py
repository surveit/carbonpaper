"""The row driver and handler shapes: grain and order hold by construction —
the mapper never sees the frame, one result slot exists per input row, and
results are written back by input index (also under concurrency)."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from app.models import Stage
from app.models.stage import StageType
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import RunCancelled
from app.core.agent.usage import LlmUsage
from app.runtime.stages.execution import (
    ROW_DEFERRED_KEY,
    ROW_DROP_KEY,
    ROW_ERROR_KEY,
    ROW_USAGE_KEY,
    FrameHandler,
    RowMapHandler,
    SourceHandler,
    validate_registry_matches_model,
)
from app.core.stage_cache import StageCacheEntry
from conftest import contribution_of, make_run_context


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
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, make_run_context())
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
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": list(range(8))})}, make_run_context())
    assert list(out["x"]) == list(range(8))


def test_row_driver_parallel_branch_raises_run_cancelled_when_pre_requested():
    # Cancellation is requested BEFORE execute() even starts. The check runs
    # once per as_completed() wakeup, but a freed worker thread picks up its
    # next queued row independently of that check — so "how many rows race
    # ahead before the first check fires" is a genuine OS-scheduling race,
    # not something this test can pin to an exact count. Using few workers
    # (parallelism=2) against many more rows than could plausibly be
    # dispatched in that race window keeps `len(calls) < len(records)` true
    # without being timing-flaky.
    calls: list[int] = []

    def make_mapper(stage, ctx):
        def map_row(row):
            calls.append(row["x"])
            time.sleep(0.01)
            return {"x": row["x"]}
        return map_row

    handler = RowMapHandler(make_mapper=make_mapper, parallelism=2)
    ctx = make_run_context(
        identity=RunIdentity(project="p-parallel", run_id="r-parallel"),
        stage_cache=StageCacheEntry.read_write(),
    )
    request_cancel("p-parallel", "r-parallel")
    records = list(range(200))
    with pytest.raises(RunCancelled):
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": records})}, ctx)
    assert 0 < len(calls) < len(records)  # some rows started, nowhere near all


def test_row_driver_sequential_branch_raises_run_cancelled_when_pre_requested():
    calls: list[int] = []

    def make_mapper(stage, ctx):
        def map_row(row):
            calls.append(row["x"])
            return {"x": row["x"]}
        return map_row

    handler = RowMapHandler(make_mapper=make_mapper)  # parallelism=1 -> sequential branch
    ctx = make_run_context(
        identity=RunIdentity(project="p-seq", run_id="r-seq"),
        stage_cache=StageCacheEntry.read_write(),
    )
    request_cancel("p-seq", "r-seq")
    with pytest.raises(RunCancelled):
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, ctx)
    assert calls == []  # cancelled before the first row's mapper ever ran


def test_row_driver_ignores_cancellation_when_ctx_has_no_run_identity():
    # A subset/eval run's ctx carries no project/run_id (see
    # executor._subset_ctx) — cancellation must never apply to it, even if the
    # same run_id happens to be cancelled elsewhere.
    request_cancel("some-project", "some-run")
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
    assert len(out) == 2  # ran to completion, unaffected


def test_row_driver_is_one_to_one():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
    assert len(out) == 2


def test_row_driver_rejects_non_dict_result():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: 42)
    with pytest.raises(ValueError, match="one dict per row"):
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1]})}, make_run_context())


def test_row_driver_rejects_multiple_inputs():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    frames = {"a": pd.DataFrame({"x": [1]}), "b": pd.DataFrame({"x": [1]})}
    with pytest.raises(ValueError, match="exactly one input"):
        handler.execute(_two_input_stage(), frames, make_run_context())


def test_row_driver_empty_input():
    handler = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    out = handler.execute(_row_stage(),
                          {"src": pd.DataFrame({"x": pd.Series([], dtype="int64")})}, make_run_context())
    assert len(out) == 0


def test_row_driver_collects_row_errors_without_dropping_the_stage():
    def make_mapper(stage, ctx):
        def map_row(row):
            if row["x"] == 2:
                return {"x": row["x"], ROW_ERROR_KEY: "boom"}
            return {"x": row["x"], "y": row["x"] * 10}
        return map_row

    handler = RowMapHandler(make_mapper=make_mapper)
    ctx = make_run_context()
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, ctx)
    assert len(out) == 3                                    # stage completes, all rows kept
    assert contribution_of(out).row_errors == [{"row": 1, "message": "boom"}]


def test_row_driver_collects_multiple_row_errors_in_ascending_row_order():
    def make_mapper(stage, ctx):
        def map_row(row):
            if row["x"] in (10, 30):
                return {"x": row["x"], ROW_ERROR_KEY: f"boom-{row['x']}"}
            return {"x": row["x"], "y": row["x"] * 10}
        return map_row

    handler = RowMapHandler(make_mapper=make_mapper)
    ctx = make_run_context()
    # Rows at positions 0 and 2 of a 3-row input fail; position 1 succeeds.
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [10, 20, 30]})}, ctx)
    assert len(out) == 3
    assert contribution_of(out).row_errors == [
        {"row": 0, "message": "boom-10"},
        {"row": 2, "message": "boom-30"},
    ]


def test_row_driver_projects_to_declared_columns():
    schema = {"columns": [{"name": "x", "type": "int"}, {"name": "score", "type": "int"}]}
    handler = RowMapHandler(
        make_mapper=lambda stage, ctx: lambda row: {"x": row["x"], "score": 1, "extra": "drop me"},
        project_output_to_declared=True,
    )
    ctx = make_run_context()
    out = handler.execute(_row_stage(output_schema=schema),
                          {"src": pd.DataFrame({"x": [1]})}, ctx)
    assert list(out.columns) == ["x", "score"]
    assert contribution_of(out).dropped_columns == ["extra"]


def _marks_row_two_for_dropping(stage, ctx):
    def map_row(row):
        return {"x": row["x"], ROW_DROP_KEY: row["x"] == 2}
    return map_row


def test_row_driver_drops_rows_marked_by_a_dropping_handler():
    handler = RowMapHandler(make_mapper=_marks_row_two_for_dropping, drops_rows=True)
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, make_run_context())
    assert list(out["x"]) == [1, 3]          # the marked row is gone, the rest in input order
    assert list(out.index) == [0, 1]         # 0-based and contiguous for downstream row positions


def test_row_driver_rejects_a_drop_marker_from_a_handler_that_does_not_declare_dropping():
    handler = RowMapHandler(make_mapper=_marks_row_two_for_dropping)  # drops_rows defaults False
    with pytest.raises(ValueError) as excinfo:
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, make_run_context())
    assert "t" in str(excinfo.value) and ROW_DROP_KEY in str(excinfo.value)


def _marks_every_row_with_every_marker(stage, ctx):
    def map_row(row):
        return {
            "x": row["x"],
            ROW_ERROR_KEY: None,
            ROW_USAGE_KEY: LlmUsage(),
            ROW_DEFERRED_KEY: True,
            ROW_DROP_KEY: False,
        }
    return map_row


def test_row_driver_runs_the_handler_marker_collector_after_the_map():
    seen: list[pd.DataFrame] = []
    handler = RowMapHandler(
        make_mapper=_marks_every_row_with_every_marker,
        drops_rows=True,
        collect_row_markers=lambda stage, df, ctx, contribution: seen.append(df.copy()),
    )
    handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, make_run_context())
    [collected] = seen
    assert len(collected) == 3  # every mapped row
    assert {ROW_ERROR_KEY, ROW_USAGE_KEY, ROW_DEFERRED_KEY, ROW_DROP_KEY} <= set(collected.columns)


def test_row_driver_lets_a_marker_collector_raise_out_of_execute():
    def collect_row_markers(stage, df, ctx, contribution):
        raise RuntimeError("collector said stop")

    handler = RowMapHandler(
        make_mapper=lambda stage, ctx: lambda row: dict(row),
        collect_row_markers=collect_row_markers,
    )
    with pytest.raises(RuntimeError, match="collector said stop"):
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1]})}, make_run_context())


def test_internal_marker_columns_never_reach_output_even_without_an_output_schema():
    # No output_schema and no projection: the strip is the ONLY thing keeping
    # machinery columns out of stage output.
    handler = RowMapHandler(make_mapper=_marks_every_row_with_every_marker, drops_rows=True)
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
    assert list(out.columns) == ["x"]  # user column survives, every marker is gone


def test_marker_columns_are_not_reported_as_dropped_user_columns():
    def make_mapper(stage, ctx):
        def map_row(row):
            return {**_marks_every_row_with_every_marker(stage, ctx)(row), "extra": "drop me"}
        return map_row

    schema = {"columns": [{"name": "x", "type": "int"}]}
    handler = RowMapHandler(make_mapper=make_mapper, project_output_to_declared=True, drops_rows=True)
    ctx = make_run_context()
    out = handler.execute(_row_stage(output_schema=schema), {"src": pd.DataFrame({"x": [1]})}, ctx)
    assert list(out.columns) == ["x"]
    # the undeclared USER column only
    assert contribution_of(out).dropped_columns == ["extra"]


def test_dropping_handler_is_not_grain_and_order_preserving():
    mapping = RowMapHandler(make_mapper=lambda stage, ctx: lambda row: dict(row))
    dropping = RowMapHandler(make_mapper=_marks_row_two_for_dropping, drops_rows=True)
    assert dropping.preserves_grain_and_order is False
    assert mapping.preserves_grain_and_order is True
    assert SourceHandler(read=lambda stage, ctx: pd.DataFrame()).preserves_grain_and_order is True
    assert FrameHandler(apply=lambda stage, inputs, ctx: None).preserves_grain_and_order is False


def test_source_handler_reads_without_frames():
    handler = SourceHandler(read=lambda stage, ctx: pd.DataFrame({"k": ["a"]}))
    out = handler.execute(_row_stage(), {}, make_run_context())
    assert list(out["k"]) == ["a"]


def test_frame_handler_receives_frames():
    handler = FrameHandler(apply=lambda stage, inputs, ctx: inputs["src"].head(1))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
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
    validate_registry_matches_model(good)  # must not raise


def test_check_registry_rejects_shape_disagreeing_with_model():
    bad = _registry(FrameHandler(apply=lambda stage, inputs, ctx: pd.DataFrame()))
    with pytest.raises(RuntimeError, match="is registered as"):
        validate_registry_matches_model(bad)
