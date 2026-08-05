from __future__ import annotations

import pandas as pd
import pytest

from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.runtime.context import RunContext
from app.runtime.manifest import StageContribution
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import _project_onto_declared_columns
from conftest import queue_columns


def _rating_stage() -> Stage:
    """A row-mapped stage declaring three output columns. The id is distinct
    from every column name so a message can be checked for both."""
    return parse_stage({
        "id": "rate", "name": "Rate", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": [{"name": "id", "type": "str", "nullable": True}]}}],
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "id", "type": "str", "nullable": True}],
                },
            ],
            "adds": [
                {"name": "score", "type": "int", "nullable": True},
                {"name": "verdict", "type": "str", "nullable": True},
            ],
        },
        "function": {"kind": "inline", "code": "def transform(row): return row"},
    })


def test_projection_raises_naming_the_stage_and_every_missing_column():
    frame = pd.DataFrame({"id": ["r1"], "leftover": [1]})

    with pytest.raises(ValueError) as excinfo:
        _project_onto_declared_columns(frame, _rating_stage(), StageContribution())

    message = str(excinfo.value)
    assert "rate" in message
    assert "score" in message and "verdict" in message   # every missing one, not just the first


def test_projection_keeps_declared_order_and_reports_what_it_dropped():
    frame = pd.DataFrame({"verdict": ["yes"], "leftover": [1], "id": ["r1"], "score": [3]})
    contribution = StageContribution()

    projected = _project_onto_declared_columns(frame, _rating_stage(), contribution)

    assert list(projected.columns) == ["id", "score", "verdict"]
    assert contribution.dropped_columns == ["leftover"]


def test_human_review_queue_output_missing_a_declared_column_raises(tmp_path):
    """The queue handler projects through the same row driver, so a column its
    rows never carry fails there too — it is not quietly dropped from the frame
    a downstream stage then consumes."""
    stage = parse_stage({
        "id": "q", "name": "Review", "type": "human_review_queue",
        "inputs": [{"id": "load", "schema": {"columns": [
            {"name": "claim_id", "type": "str", "nullable": False},
            {"name": "score", "type": "int", "nullable": True}]}}],
        "signature": {
            "form": "extends",
            "adds": [
                {"name": "human_score", "type": "int", "nullable": True},
                {"name": "decision", "type": "str", "nullable": True},
                {"name": "reviewer_id", "type": "str", "nullable": True},
                {"name": "reviewed_at", "type": "str", "nullable": True},
                {"name": "reviewer_note", "type": "str", "nullable": True},
            ],
        },
        "queue": {**queue_columns(), "review_notes_column": None},
    })
    inputs = {"load": pd.DataFrame({"claim_id": ["c1"], "score": [1]})}
    ctx = RunContext.for_stages_outside_a_run(tmp_path, tmp_path, queue_auto_approve=True)

    with pytest.raises(ValueError) as excinfo:
        HANDLERS[StageType.human_review_queue].execute(stage, inputs, ctx)

    message = str(excinfo.value)
    assert "q" in message and "reviewer_note" in message
