"""What a stage stops paying for once it is going to unwind.

Both paths submit EVERY unit of work to the pool up front, so the pool's own
shutdown drains the queue - a model call per row or chunk still waiting, made
and then dropped. These pin the two exits: a cancel, and any other raise.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.stage_cache import ReadOnlyStageCache
from app.models import Stage, parse_stage
from app.models.stage import StageType
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import RunCancelled
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import RowMapTransformHandler
from app.runtime.stages import llm_transform as lt
from conftest import as_inputs, make_run_context, place_stage

PROJECT = "cancelled-fan-out"
_ROWS = 8
_SRC = pd.DataFrame({"post_id": [f"p{i}" for i in range(_ROWS)],
                     "text": [f"t{i}" for i in range(_ROWS)]})


def _stage(batch_size: int = 1) -> Stage:
    return parse_stage({
        "id": "score", "description": "Score", "type": "llm_transform",
        "inputs": [{"id": "load"}], "cache": False,
        "signature": {"form": "extends",
                      "reads": [{"input": "load", "columns": [
                          {"name": "text", "type": "str", "nullable": True}]}],
                      "adds": [{"name": "label", "type": "str", "nullable": True}]},
        "llm": {"prompt_data_template": "score {text}", "batch_size": batch_size,
                "max_retries": 0}})


def _execute(batch_size: int, parallelism: int):
    handler = HANDLERS[StageType.llm_transform]
    assert isinstance(handler, RowMapTransformHandler)
    was, handler.parallelism = handler.parallelism, parallelism
    try:
        return handler.execute(
            place_stage(_stage(batch_size), load={"columns": [
                {"name": "post_id", "type": "str", "nullable": True},
                {"name": "text", "type": "str", "nullable": True}]}),
            as_inputs({"load": _SRC.copy()}),
            make_run_context(identity=RunIdentity(project=PROJECT, run_id="r1"),
                             stage_cache=ReadOnlyStageCache()))
    finally:
        handler.parallelism = was


@pytest.mark.parametrize("batch_size,seam", [(1, "call_llm"), (2, "call_llm_batch")],
                         ids=["per-row", "batched"])
def test_a_cancel_stops_the_work_still_queued(monkeypatch, batch_size, seam):
    calls = {"n": 0}

    def answer(*args, **kwargs):
        calls["n"] += 1
        request_cancel(PROJECT, "r1")            # cancel arrives during the first unit
        if "task" in kwargs:
            k = kwargs["task"].count("### item ")
            return {"results": [{"row_number": i, "label": f"L{i}"} for i in range(k)]}
        return {"label": "L"}

    monkeypatch.setattr(lt, seam, answer)
    # Fewer workers than units of work, so the pool actually holds a QUEUE. What
    # is already dispatched cannot be stopped — a blocking call has no interrupt —
    # so only what never started is what a cancel can save.
    with pytest.raises(RunCancelled):
        _execute(batch_size, parallelism=2)

    assert calls["n"] < _ROWS // batch_size      # not the whole stage


@pytest.mark.parametrize("batch_size,seam", [(1, "call_llm"), (2, "call_llm_batch")],
                         ids=["per-row", "batched"])
def test_an_aborting_unit_stops_the_work_still_queued(monkeypatch, batch_size, seam):
    """A raise is not a cancel, but it unwinds the same way — so it must stop paying too."""
    calls = {"n": 0}

    def answer(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        if "task" in kwargs:
            k = kwargs["task"].count("### item ")
            return {"results": [{"row_number": i, "label": f"L{i}"} for i in range(k)]}
        return {"label": "L"}

    monkeypatch.setattr(lt, seam, answer)
    with pytest.raises(KeyboardInterrupt):
        _execute(batch_size, parallelism=2)

    assert calls["n"] < _ROWS // batch_size
