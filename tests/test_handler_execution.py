"""The row driver and handler shapes: grain and order hold by construction —
the mapper never sees the frame, one result slot exists per input row, and
results are written back by input index (also under concurrency)."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from app.core.agent.usage import LlmUsage
from app.models import parse_stage
from app.models.stage import StageType
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import RunCancelled
from app.runtime.stages.execution import (
    ROW_DEFERRED_KEY,
    ROW_ERROR_KEY,
    ROW_USAGE_KEY,
    FrameHandler,
    RowMapHandler,
    SourceHandler,
    validate_registry_matches_model,
)
from app.core.stage_cache import StageCacheEntry
from conftest import contribution_of, make_run_context, reads_of


# The single `x` column of the frames these tests hand the driver. The declared
# schemas only have to be present and honest: these handlers are constructed
# directly, so a schema is read at all only where the handler is asked to
# project (`project_output_to_declared=True`), and those tests pass their own.
_X_COLUMN = [{"name": "x", "type": "int", "nullable": True}]


def _row_stage(output_schema=None, input_columns=_X_COLUMN):
    """A row function outputting `output_schema`; its adds are what that names beyond the edge."""
    flowing = {c["name"] for c in input_columns}
    added = [c for c in (output_schema or {}).get("columns", [])
             if c["name"] not in flowing]
    return parse_stage({
        "id": "t", "description": "t", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": {"columns": input_columns}}],
        "signature": {"form": "extends", "reads": reads_of("src", input_columns),
                      "adds": added},
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    })


def _two_input_stage():
    return parse_stage({
        "id": "t2", "description": "t2", "type": "python_frame_function",
        "inputs": [{"id": "a", "schema": {"columns": _X_COLUMN}},
                   {"id": "b", "schema": {"columns": _X_COLUMN}}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "a", "columns": _X_COLUMN}, {"input": "b", "columns": _X_COLUMN}],
            "produces": _X_COLUMN,
        },
        "function": {"kind": "inline", "code": "def transform(a, b):\n    return a\n"},
    })


def test_row_driver_maps_in_input_order():
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: {"x": row["x"], "y": row["x"] * 10})
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, make_run_context())
    assert list(out["x"]) == [1, 2, 3]
    assert list(out["y"]) == [10, 20, 30]


def test_row_driver_preserves_order_under_parallelism():
    # Later rows finish FIRST (reverse sleeps); index write-back must still
    # reassemble in input order.
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
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

    def make_mapper(stage, ctx, src):
        def map_row(row, index):
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

    def make_mapper(stage, ctx, src):
        def map_row(row, index):
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
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: dict(row))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
    assert len(out) == 2  # ran to completion, unaffected


def test_row_driver_is_one_to_one():
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: dict(row))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
    assert len(out) == 2


def test_row_driver_rejects_non_dict_result():
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: 42)
    with pytest.raises(ValueError, match="one dict per row"):
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1]})}, make_run_context())


def test_row_driver_rejects_multiple_inputs():
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: dict(row))
    frames = {"a": pd.DataFrame({"x": [1]}), "b": pd.DataFrame({"x": [1]})}
    with pytest.raises(ValueError, match="exactly one input"):
        handler.execute(_two_input_stage(), frames, make_run_context())


_EMPTY_SOURCE_COLUMNS = [{"name": "x", "type": "int", "nullable": True}, {"name": "id", "type": "str", "nullable": True}]


def _empty_source() -> pd.DataFrame:
    return pd.DataFrame({
        "x": pd.Series([], dtype="int64"), "id": pd.Series([], dtype="object")
    })


def test_row_driver_empty_input():
    # No row means no mapper result to name the output's columns, so the frame
    # takes the ones the signature promised — here an `extends` adding nothing,
    # so the input's. A 0x0 frame would make a downstream stage keyed on `id`
    # raise KeyError instead of producing an empty result.
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: dict(row))
    ctx = make_run_context()
    out = handler.execute(
        _row_stage(input_columns=_EMPTY_SOURCE_COLUMNS), {"src": _empty_source()}, ctx)
    assert len(out) == 0
    assert list(out.columns) == ["x", "id"]
    assert out["id"].tolist() == []      # the column is real, not just a label
    # the substituted frame still carries the stage's contribution
    assert contribution_of(out).dropped_columns == []


def test_row_driver_empty_input_still_emits_the_columns_the_signature_adds():
    added = {"columns": [*_EMPTY_SOURCE_COLUMNS, {"name": "y", "type": "str", "nullable": True}]}
    # The empty result is a RESULT, not an error: what downstream may read does
    # not depend on whether a row survived. An `extends` promises its `adds`, so
    # they are on the frame even though no mapper ran to produce them — without
    # this the stage fails its own output schema on a legitimately empty input.
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: dict(row))
    out = handler.execute(
        _row_stage(output_schema=added, input_columns=_EMPTY_SOURCE_COLUMNS),
        {"src": _empty_source()}, make_run_context())

    assert len(out) == 0
    assert list(out.columns) == ["x", "id", "y"]
    assert out["y"].tolist() == []
    assert out["x"].dtype == "int64"  # a column that flows keeps the input's dtype


def test_row_driver_empty_input_reports_no_dropped_columns_when_projecting():
    # Projection sees a frame with no columns at all, so it drops nothing —
    # an empty input must not be reported as having discarded `id`.
    schema = {"columns": [{"name": "x", "type": "int", "nullable": True}]}
    handler = RowMapHandler(
        make_mapper=lambda stage, ctx, src: lambda row, index: dict(row),
        project_output_to_declared=True,
    )
    ctx = make_run_context()
    out = handler.execute(
        _row_stage(output_schema=schema, input_columns=_EMPTY_SOURCE_COLUMNS),
        {"src": _empty_source()}, ctx)
    assert len(out) == 0
    assert list(out.columns) == ["x", "id"]
    assert contribution_of(out).dropped_columns == []


def test_row_driver_collects_row_errors_without_dropping_the_stage():
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
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
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
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
    schema = {"columns": [{"name": "x", "type": "int", "nullable": True}, {"name": "score", "type": "int", "nullable": True}]}
    handler = RowMapHandler(
        make_mapper=lambda stage, ctx, src: lambda row, index: {"x": row["x"], "score": 1, "extra": "drop me"},
        project_output_to_declared=True,
    )
    ctx = make_run_context()
    out = handler.execute(_row_stage(output_schema=schema),
                          {"src": pd.DataFrame({"x": [1]})}, ctx)
    assert list(out.columns) == ["x", "score"]
    assert contribution_of(out).dropped_columns == ["extra"]


def _mark_row_with_every_marker(row, index):
    return {
        "x": row["x"],
        ROW_ERROR_KEY: None,
        ROW_USAGE_KEY: LlmUsage(),
        ROW_DEFERRED_KEY: True,
    }


def _marks_every_row_with_every_marker(stage, ctx, src):
    return _mark_row_with_every_marker


class _MarksEveryRowAndKeepsTheFrame:
    """A mapper of the `PostMapRowMapper` shape: marks every row, and its own
    post-map step keeps the frame the driver hands back to it."""

    def __init__(self):
        self.seen: list[pd.DataFrame] = []

    def __call__(self, row, index):
        return _mark_row_with_every_marker(row, index)

    def finish_mapped_rows(self, stage, df, ctx, contribution):
        self.seen.append(df.copy())


class _MapperWhosePostMapStepRaises:
    def __call__(self, row, index):
        return dict(row)

    def finish_mapped_rows(self, stage, df, ctx, contribution):
        raise RuntimeError("post-map step said stop")


def test_row_driver_runs_the_mappers_own_post_map_step_after_the_map():
    mapper = _MarksEveryRowAndKeepsTheFrame()
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: mapper)
    handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2, 3]})}, make_run_context())
    [collected] = mapper.seen
    assert len(collected) == 3  # every mapped row
    assert {ROW_ERROR_KEY, ROW_USAGE_KEY, ROW_DEFERRED_KEY} <= set(collected.columns)


def test_row_driver_lets_a_mappers_post_map_step_raise_out_of_execute():
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: _MapperWhosePostMapStepRaises())
    with pytest.raises(RuntimeError, match="post-map step said stop"):
        handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1]})}, make_run_context())


def test_a_plain_closure_mapper_needs_no_post_map_step():
    """A mapper that is just a function — llm_transform's and
    python_row_function's shape — carries no `finish_mapped_rows`, and the
    driver runs it to completion without one."""
    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: dict(row))
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
    assert list(out["x"]) == [1, 2]


def test_internal_marker_columns_never_reach_output_even_without_an_output_schema():
    # No output_schema and no projection: the strip is the ONLY thing keeping
    # machinery columns out of stage output.
    handler = RowMapHandler(make_mapper=_marks_every_row_with_every_marker)
    out = handler.execute(_row_stage(), {"src": pd.DataFrame({"x": [1, 2]})}, make_run_context())
    assert list(out.columns) == ["x"]  # user column survives, every marker is gone


def test_marker_columns_are_not_reported_as_dropped_user_columns():
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
            return {**_mark_row_with_every_marker(row, index), "extra": "drop me"}
        return map_row

    schema = {"columns": [{"name": "x", "type": "int", "nullable": True}]}
    handler = RowMapHandler(make_mapper=make_mapper, project_output_to_declared=True)
    ctx = make_run_context()
    out = handler.execute(_row_stage(output_schema=schema), {"src": pd.DataFrame({"x": [1]})}, ctx)
    assert list(out.columns) == ["x"]
    # the undeclared USER column only
    assert contribution_of(out).dropped_columns == ["extra"]


def test_each_shape_reports_the_preservation_its_calling_convention_gives_it():
    mapping = RowMapHandler(make_mapper=lambda stage, ctx, src: lambda row, index: dict(row))
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
        StageType.python_row_function: RowMapHandler(make_mapper=lambda s, c, src: lambda r, i: r),
        StageType.llm_transform: llm_shape,
        StageType.python_frame_function: frame,
        StageType.enrich: frame,
        StageType.expand: frame,
        StageType.aggregate: frame,
        StageType.human_review_queue: RowMapHandler(make_mapper=lambda s, c, src: lambda r, i: r),
        StageType.publish: frame,
    }


def test_check_registry_accepts_shapes_matching_the_model():
    good = _registry(RowMapHandler(make_mapper=lambda s, c, src: lambda r, i: r))
    validate_registry_matches_model(good)  # must not raise


def test_check_registry_rejects_shape_disagreeing_with_model():
    bad = _registry(FrameHandler(apply=lambda stage, inputs, ctx: pd.DataFrame()))
    with pytest.raises(RuntimeError, match="is registered as"):
        validate_registry_matches_model(bad)
