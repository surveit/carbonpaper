import pandas as pd
import pytest
from app.runtime.context import RunIdentity
from app.runtime.stages.human_review_queue import handle_human_review_queue
from app.core.run_status import RunMode
from app.models import Stage
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
        stage_cache=StageCacheEntry.for_mode(RunMode.PRODUCTION),
    )
    with pytest.raises(ValueError, match="filter could not be evaluated"):
        handle_human_review_queue(stage, inputs, ctx)
