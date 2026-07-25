import numpy as np
import pandas as pd
import pytest
from app.runtime.context import RunIdentity
from app.runtime.stages import HANDLERS
from app.models import Stage
from app.models.stage import StageType
from app.core.stage_cache import StageCacheEntry
from conftest import make_run_context


def test_bad_filter_raises_instead_of_skipping_review(tmp_path):
    stage = Stage.model_validate({
        "id": "q", "type": "human_review_queue", "name": "q", "inputs": ["a"],
        "output_schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}],
                          "primary_key": ["claim_id"]},
        "queue": {"filter": "nonexistent == True"},
    })
    inputs = {"a": pd.DataFrame({"claim_id": ["c1", "c2"]})}
    ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project="queue-filter-loud", run_id="r1"),
        stage_cache=StageCacheEntry.read_write(),
    )
    with pytest.raises(ValueError, match="filter could not be evaluated"):
        HANDLERS[StageType.human_review_queue].execute(stage, inputs, ctx)


def test_a_cell_the_filter_cannot_answer_names_the_stage_and_the_filter(tmp_path):
    """A cell holding an array makes the comparison ambiguous rather than
    false. What the operator sees must still be this stage's own message —
    naming the stage id and the filter text — not the bare numpy error the
    comparison raises underneath."""
    stage = Stage.model_validate({
        "id": "q", "type": "human_review_queue", "name": "q", "inputs": ["a"],
        "output_schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}],
                          "primary_key": ["claim_id"]},
        "queue": {"filter": "score > 1"},
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
