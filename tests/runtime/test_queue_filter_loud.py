import pandas as pd
import pytest
from app.runtime.stages.human_review_queue import handle_human_review_queue
from app.models import Stage
from conftest import make_run_context


def test_bad_filter_raises_instead_of_skipping_review(tmp_path):
    stage = Stage.model_validate({
        "id": "q", "type": "human_review_queue", "name": "q", "inputs": ["a"],
        "output_schema": {"columns": [{"name": "claim_id", "type": "str", "nullable": False}],
                          "primary_key": ["claim_id"]},
        "queue": {"filter": "nonexistent == True", "hash_columns": ["claim_id"]},
    })
    inputs = {"a": pd.DataFrame({"claim_id": ["c1", "c2"]})}
    ctx = make_run_context(run_dir=tmp_path)
    with pytest.raises(ValueError, match="filter could not be evaluated"):
        handle_human_review_queue(stage, inputs, ctx)
