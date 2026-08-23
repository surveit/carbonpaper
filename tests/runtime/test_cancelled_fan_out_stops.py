"""What a stage stops paying for once it is going to unwind.

Both paths submit EVERY unit of work to the pool up front, so the pool's own
shutdown drains the queue - a model call per row or chunk still waiting, made
and then dropped. These pin the two exits: a cancel, and any other raise.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd
import pytest

from app.core.stage_cache import ReadOnlyStageCache
from app.models import Stage, parse_stage
from app.models.stage import StageType
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import RunCancelled
from app.runtime.stages import HANDLERS
from app.runtime.stages import execution
from app.runtime.stages import llm_transform as lt
from conftest import as_inputs, make_run_context, place_stage

PROJECT = "cancelled-fan-out"
_ROWS = 8
_PARALLELISM = 2
_GATE_SECONDS = 30.0
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
    assert isinstance(handler, execution.RowMapTransformHandler)
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


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """Opens once the pool has dropped its queue, so a freed worker finds nothing left to take."""
    opened = threading.Event()

    class _PoolOpeningTheGateOnShutdown(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            super().shutdown(wait=False, cancel_futures=cancel_futures)
            opened.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(execution, "ThreadPoolExecutor", _PoolOpeningTheGateOnShutdown)
    yield opened
    opened.set()


def _hold_until_the_gate_opens(gate: threading.Event) -> None:
    if not gate.wait(timeout=_GATE_SECONDS):
        raise AssertionError("the pool never dropped its queue — the fan-out did not unwind")


def _reply_to(kwargs: dict[str, Any]) -> dict[str, Any]:
    if "task" in kwargs:
        k = kwargs["task"].count("### item ")
        return {"results": [{"row_number": i, "label": f"L{i}"} for i in range(k)]}
    return {"label": "L"}


@pytest.mark.parametrize("batch_size,seam", [(1, "call_llm"), (2, "call_llm_batch")],
                         ids=["per-row", "batched"])
def test_a_cancel_stops_the_work_still_queued(monkeypatch, gate, batch_size, seam):
    calls = {"n": 0}
    counting = threading.Lock()

    def answer(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with counting:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            request_cancel(PROJECT, "r1")        # cancel arrives during the first unit
        else:
            _hold_until_the_gate_opens(gate)
        return _reply_to(kwargs)

    monkeypatch.setattr(lt, seam, answer)
    # Fewer workers than units of work, so the pool actually holds a QUEUE. What
    # is already dispatched cannot be stopped — a blocking call has no interrupt —
    # so only what never started is what a cancel can save.
    with pytest.raises(RunCancelled):
        _execute(batch_size, parallelism=_PARALLELISM)

    # The unit that cancelled, plus the one each worker had already taken.
    assert 1 + _PARALLELISM < _ROWS // batch_size
    assert calls["n"] <= 1 + _PARALLELISM


@pytest.mark.parametrize("batch_size,seam", [(1, "call_llm"), (2, "call_llm_batch")],
                         ids=["per-row", "batched"])
def test_an_aborting_unit_stops_the_work_still_queued(monkeypatch, gate, batch_size, seam):
    """A raise is not a cancel, but it unwinds the same way — so it must stop paying too."""
    calls = {"n": 0}
    counting = threading.Lock()

    def answer(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with counting:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            raise KeyboardInterrupt
        _hold_until_the_gate_opens(gate)
        return _reply_to(kwargs)

    monkeypatch.setattr(lt, seam, answer)
    with pytest.raises(KeyboardInterrupt):
        _execute(batch_size, parallelism=_PARALLELISM)

    assert 1 + _PARALLELISM < _ROWS // batch_size
    assert calls["n"] <= 1 + _PARALLELISM
