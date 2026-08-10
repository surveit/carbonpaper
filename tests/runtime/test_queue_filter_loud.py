import numpy as np
import pandas as pd
import pytest
from app.runtime.context import RunIdentity
from app.runtime.stages import HANDLERS
from app.models import parse_stage
from app.models.stage import StageType
from app.core.stage_cache import StageCacheEntry
from conftest import QUEUE_COLUMNS, make_run_context, queue_added_columns, reads_of


def test_bad_filter_raises_instead_of_skipping_review(tmp_path):
    columns = [
        {"name": "claim_id", "type": "str", "nullable": False},
        {"name": "score", "type": "int", "nullable": True},
        {"name": "nonexistent", "type": "bool", "nullable": True},
    ]
    stage = parse_stage({
        "id": "q", "type": "human_review_queue", "description": "q",
        # The edge DECLARES `nonexistent` — otherwise the filter's column
        # reference would be rejected when the stage is built, and this test is
        # about the frame that actually arrives not having the column.
        "inputs": [{"id": "a", "schema": {"columns": columns}}],
        "signature": {"form": "extends", "adds": queue_added_columns(),
                      "reads": reads_of("a", columns)},
        "queue": {**QUEUE_COLUMNS, "filter": "nonexistent == True"},
    })
    # `score` is here because QUEUE_COLUMNS reviews it: the stage refuses a frame
    # missing a declared source column before it ever evaluates the filter, and
    # this test is about the filter.
    inputs = {"a": pd.DataFrame({"claim_id": ["c1", "c2"], "score": [1, 2]})}
    ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project="queue-filter-loud", run_id="r1"),
        stage_cache=StageCacheEntry.read_write(),
    )
    with pytest.raises(ValueError, match="filter could not be evaluated"):
        HANDLERS[StageType.human_review_queue].execute(stage, inputs, ctx)


def test_a_cell_the_filter_cannot_answer_names_the_stage_and_the_filter(tmp_path):
    columns = [
        {"name": "claim_id", "type": "str", "nullable": False},
        {"name": "score", "type": "int", "nullable": True},
    ]
    stage = parse_stage({
        "id": "q", "type": "human_review_queue", "description": "q",
        "inputs": [{"id": "a", "schema": {"columns": columns}}],
        "signature": {"form": "extends", "adds": queue_added_columns(),
                      "reads": reads_of("a", columns)},
        "queue": {**QUEUE_COLUMNS, "filter": "score > 1"},
    })
    inputs = {"a": pd.DataFrame({
        "claim_id": ["c1"], "score": pd.Series([np.array([1, 2, 3])], dtype=object),
    })}
    ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project="queue-filter-loud", run_id="r2"),
        stage_cache=StageCacheEntry.read_write(),
    )
    with pytest.raises(ValueError) as excinfo:
        HANDLERS[StageType.human_review_queue].execute(stage, inputs, ctx)
    message = str(excinfo.value)
    assert "human_review_queue 'q'" in message
    assert "score > 1" in message
