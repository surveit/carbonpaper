from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from app.core.run_status import StageStatus
from app.models import parse_stage
from app.models.run_manifest import StageProgress, StageRecord
from app.runtime.progress import StageProgressTracker
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import RowMapTransformHandler
from app.runtime.stages import llm_transform
from conftest import as_inputs, make_run_context, place_stage


def _row_stage():
    return parse_stage({
        "id": "map", "description": "Map", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "extends",
            "reads": [{
                "input": "src",
                "columns": [{"name": "x", "type": "int", "nullable": True}],
            }],
        },
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    })


def _frame_stage(code: str):
    columns = [{"name": "x", "type": "int", "nullable": True}]
    return parse_stage({
        "id": "shape", "description": "Shape", "type": "python_frame_function",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "src", "columns": columns}],
            "produces": columns,
        },
        "function": {"kind": "inline", "code": code},
    })


def _batched_stage():
    return parse_stage({
        "id": "classify", "description": "Classify", "type": "llm_transform",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "extends",
            "reads": [{
                "input": "src",
                "columns": [{"name": "x", "type": "int", "nullable": True}],
            }],
            "adds": [{"name": "label", "type": "str", "nullable": True}],
        },
        "llm": {
            "prompt_instructions": "Classify each item.",
            "prompt_data_template": "{x}",
            "batch_size": 2,
        },
    })


def _tracker(stage, flush=lambda: None, **kwargs):
    record = StageRecord.record_with_status(stage, StageStatus.RUNNING)
    return record, StageProgressTracker(record, flush, **kwargs)


def test_progress_refuses_a_completed_count_past_total():
    with pytest.raises(ValidationError, match="exceeds total"):
        StageProgress(completed=3, total=2, unit="rows", updated_at="now")


@pytest.mark.parametrize(
    "update",
    [
        {"completed": True, "total": 2, "unit": "rows"},
        {"completed": 1.0, "total": 2, "unit": "rows"},
        {"completed": 1, "total": 2.0, "unit": "rows"},
        {"completed": 1, "total": 2, "unit": " \t"},
    ],
)
def test_progress_callback_refuses_coerced_numbers_and_blank_units(update):
    _, tracker = _tracker(_row_stage())
    with pytest.raises(ValidationError):
        tracker(**update)


def test_progress_advance_refuses_a_completed_count_past_total():
    _, tracker = _tracker(_row_stage())
    tracker(completed=1, total=1, unit="rows")
    with pytest.raises(ValidationError, match="exceeds total"):
        tracker.advance()


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"completed": 1, "total": 2, "unit": "items"}, "unit changed"),
        ({"completed": 0, "total": 2, "unit": "rows"}, "regressed"),
        ({"completed": 1, "total": 3, "unit": "rows"}, "total changed"),
    ],
)
def test_progress_refuses_inconsistent_transitions(update, message):
    _, tracker = _tracker(_row_stage())
    tracker(completed=1, total=2, unit="rows")
    with pytest.raises(ValueError, match=message):
        tracker(**update)


def test_progress_coalesces_intermediate_writes_and_flushes_on_demand():
    now = [0.0]
    writes: list[int] = []
    record, tracker = _tracker(
        _row_stage(),
        lambda: writes.append(record.progress.completed if record.progress else -1),
        flush_interval_seconds=0.5,
        clock=lambda: now[0],
    )

    tracker(completed=0, total=3, unit="rows")
    now[0] = 0.1
    tracker.advance()
    now[0] = 0.6
    tracker.advance()
    tracker.advance()

    assert writes == [0, 2]
    tracker.flush()
    assert writes == [0, 2, 3]


def test_row_mapper_records_each_completed_row_automatically():
    stage = _row_stage()
    record, tracker = _tracker(stage)
    ctx = make_run_context().attach_stage_progress(tracker)
    handler = RowMapTransformHandler(
        make_mapper=lambda stage, ctx, src: lambda row, index: dict(row)
    )

    handler.execute(
        place_stage(stage),
        as_inputs({"src": pd.DataFrame({"x": [1, 2, 3]})}),
        ctx,
    )

    assert record.progress is not None
    assert record.progress.model_dump(exclude={"updated_at"}) == {
        "completed": 3, "total": 3, "unit": "rows",
    }


def test_batched_llm_driver_advances_after_each_completed_chunk(monkeypatch):
    stage = _batched_stage()
    record, tracker = _tracker(stage)
    tracker(completed=0, total=3, unit="rows")
    ctx = make_run_context().attach_stage_progress(tracker)

    def process_chunk(stage_id, llm, reply_schema, start, chunk):
        return [
            (start + index, {**row, "label": f"item-{row['x']}"})
            for index, row in enumerate(chunk)
        ]

    monkeypatch.setattr(llm_transform, "_process_chunk", process_chunk)
    rows = llm_transform.run_llm_batches(
        place_stage(stage),
        as_inputs({"src": pd.DataFrame({"x": [1, 2, 3]})}),
        ctx,
        1,
        [0, 1, 2],
    )

    assert len(rows) == 3
    assert record.progress is not None
    assert record.progress.completed == record.progress.total == 3


def test_frame_function_may_report_progress_through_a_keyword_only_callback():
    stage = _frame_stage(
        "def transform(df, *, progress):\n"
        "    progress(completed=1, total=2, unit='parts')\n"
        "    progress(completed=2, total=2, unit='parts')\n"
        "    return df\n"
    )
    record, tracker = _tracker(stage)
    ctx = make_run_context().attach_stage_progress(tracker)

    HANDLERS[stage.type].execute(
        place_stage(stage), as_inputs({"src": pd.DataFrame({"x": [1]})}), ctx
    )

    assert record.progress is not None
    assert record.progress.completed == record.progress.total == 2
    assert record.progress.unit == "parts"


def test_frame_progress_transition_failure_fails_the_function_call():
    stage = _frame_stage(
        "def transform(df, *, progress):\n"
        "    progress(completed=2, total=2, unit='parts')\n"
        "    progress(completed=1, total=2, unit='parts')\n"
        "    return df\n"
    )
    _, tracker = _tracker(stage)

    with pytest.raises(ValueError, match="regressed"):
        HANDLERS[stage.type].execute(
            place_stage(stage),
            as_inputs({"src": pd.DataFrame({"x": [1]})}),
            make_run_context().attach_stage_progress(tracker),
        )
