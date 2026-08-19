"""What an llm_transform loses when it fails PART-WAY: the spend already made.
Each test faults the stage after K answered calls over 8 rows, then RE-RUNS it;
the second run's call count is the finding — the work the first could not hand
back, priced in model calls."""
from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from app.core.stage_cache import StageCache
from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import RunCancelled
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from conftest import as_inputs, contribution_of, make_run_context, place_stage, rows_of

PROJECT = "llm-failure-modes"

_SRC = pd.DataFrame({"post_id": [f"p{i}" for i in range(8)], "text": [f"t{i}" for i in range(8)]})


def _stage(*, batch_size: int = 1, max_retries: int = 0, cache: bool = True) -> Stage:
    return parse_stage({
        "id": "score", "description": "Score", "type": "llm_transform",
        "inputs": [{"id": "load"}], "cache": cache,
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [
                {"name": "text", "type": "str", "nullable": True}]}],
            "adds": [{"name": "label", "type": "str", "nullable": True}]},
        "llm": {"prompt_data_template": "score {text}", "batch_size": batch_size,
                "max_retries": max_retries},
    })


def _placed(stage: Stage):
    return place_stage(stage, load={"columns": [
        {"name": "post_id", "type": "str", "nullable": True},
        {"name": "text", "type": "str", "nullable": True}]})


def _ctx(run_id: str = "r1"):
    return make_run_context(
        identity=RunIdentity(project=PROJECT, run_id=run_id), stage_cache=StageCache())


@pytest.fixture(autouse=True)
def one_row_at_a_time():
    """Serial by default, so a fault lands on a known call number; a test may widen it."""
    handler = HANDLERS[StageType.llm_transform]
    was = handler.parallelism
    handler.parallelism = 1
    yield handler
    handler.parallelism = was


def _execute(stage: Stage, ctx, parallelism: int = 1):
    handler = HANDLERS[StageType.llm_transform]
    handler.parallelism = parallelism
    return handler.execute(_placed(stage), as_inputs({"load": _SRC.copy()}), ctx)


class _Calls:
    """One scripted model seam. `fault` fires on the (1-based) call named by `fail_on`."""

    def __init__(self, fail_on: int | None = None, fault: BaseException | None = None):
        self.n = 0
        self.fail_on = fail_on
        self.fault = fault

    def row(self, stage_id, llm, row, *, reply_model, usage_out=None, **kw) -> dict[str, Any]:
        self.n += 1
        self._maybe_fail()
        return {"label": f"L-{row['text']}"}

    def batch(self, stage_id, llm, *, instructions, task, reply_schema, usage_out=None, **kw):
        self.n += 1
        self._maybe_fail()
        k = task.count("### item ")
        return {"results": [{"row_number": i, "label": f"L{i}"} for i in range(k)]}

    def _maybe_fail(self) -> None:
        if self.fail_on is not None and self.n == self.fail_on and self.fault is not None:
            raise self.fault


def _second_run_calls(monkeypatch, stage: Stage) -> int:
    """Re-run the same stage clean, and report how many model calls it still had to make."""
    monkeypatch.undo()
    replay = _Calls()
    monkeypatch.setattr(lt, "call_llm", replay.row)
    monkeypatch.setattr(lt, "call_llm_batch", replay.batch)
    _execute(stage, _ctx(run_id="r2"))
    return replay.n


# ── per-row path (batch_size == 1) ───────────────────────────────────────────


def test_per_row_ordinary_exception_loses_only_the_failed_row(monkeypatch):
    stage = _stage()
    calls = _Calls(fail_on=6, fault=RuntimeError("model refused"))
    monkeypatch.setattr(lt, "call_llm", calls.row)

    out = _execute(stage, _ctx())

    assert calls.n == 8                                    # the map completed
    errors = contribution_of(out).row_errors
    assert [e["row"] for e in errors] == [5]
    assert _second_run_calls(monkeypatch, stage) == 1       # 7 of 8 recovered


@pytest.mark.parametrize("fault", [KeyboardInterrupt(), SystemExit(1)],
                         ids=["keyboard-interrupt", "sys-exit"])
def test_per_row_base_exception_aborts_the_stage_but_keeps_earlier_rows(monkeypatch, fault):
    """A BaseException is outside every `except Exception` on the path — it unwinds the run."""
    stage = _stage()
    calls = _Calls(fail_on=6, fault=fault)
    monkeypatch.setattr(lt, "call_llm", calls.row)

    with pytest.raises(type(fault)):
        _execute(stage, _ctx())

    assert calls.n == 6                                    # stopped where it broke
    assert _second_run_calls(monkeypatch, stage) == 3       # 5 recorded before the abort


def test_per_row_cancel_mid_stage_keeps_what_was_computed(monkeypatch):
    stage = _stage()
    calls = _Calls()

    def cancel_after_five(*args, **kwargs):
        if calls.n == 5:
            request_cancel(PROJECT, "r1")
        return calls.row(*args, **kwargs)

    monkeypatch.setattr(lt, "call_llm", cancel_after_five)
    with pytest.raises(RunCancelled):
        _execute(stage, _ctx())

    assert _second_run_calls(monkeypatch, stage) == 8 - calls.n


def test_per_row_uncached_stage_recovers_nothing(monkeypatch):
    """`cache: false` — what the docs recommend for a research stage — has no recovery at all."""
    stage = _stage(cache=False)
    calls = _Calls(fail_on=6, fault=KeyboardInterrupt())
    monkeypatch.setattr(lt, "call_llm", calls.row)

    with pytest.raises(KeyboardInterrupt):
        _execute(stage, _ctx())

    assert _second_run_calls(monkeypatch, stage) == 8       # every call re-spent


# ── batched path (batch_size > 1) ────────────────────────────────────────────


def test_batched_ordinary_exception_loses_only_its_own_chunk(monkeypatch):
    """`_process_chunk` catches it, so the other chunks still reach the recording loop."""
    stage = _stage(batch_size=2)
    calls = _Calls(fail_on=3, fault=RuntimeError("model refused"))
    monkeypatch.setattr(lt, "call_llm_batch", calls.batch)

    out = _execute(stage, _ctx())

    assert [e["row"] for e in contribution_of(out).row_errors] == [4, 5]
    assert _second_run_calls(monkeypatch, stage) == 1       # one chunk re-called


def test_batched_escaping_exception_discards_every_completed_chunk(monkeypatch):
    """The recording loop runs AFTER every chunk returns, so an escape wipes the spend."""
    stage = _stage(batch_size=2)
    calls = _Calls(fail_on=4, fault=KeyboardInterrupt())
    monkeypatch.setattr(lt, "call_llm_batch", calls.batch)

    with pytest.raises(KeyboardInterrupt):
        _execute(stage, _ctx())

    assert calls.n == 4                                    # 3 chunks answered
    assert _second_run_calls(monkeypatch, stage) == 4       # none of them kept


def test_batched_grain_failure_discards_every_completed_chunk(monkeypatch):
    """Not a crash: a driver-detected gap raises the same way, after the whole stage is paid for."""
    stage = _stage(batch_size=2)
    monkeypatch.setattr(lt, "call_llm_batch", _Calls().batch)
    monkeypatch.setattr(lt, "_emit_matched", lambda start, chunk, by_number, usages: [])

    with pytest.raises(RuntimeError, match="exactly one row per input"):
        _execute(stage, _ctx())

    assert _second_run_calls(monkeypatch, stage) == 4


def test_batched_stage_never_checks_for_cancellation(monkeypatch):
    """The between-chunk checkpoint the per-row driver has does not exist here."""
    stage = _stage(batch_size=2)
    calls = _Calls()

    def cancel_then_answer(*args, **kwargs):
        request_cancel(PROJECT, "r1")
        return calls.batch(*args, **kwargs)

    monkeypatch.setattr(lt, "call_llm_batch", cancel_then_answer)
    _execute(stage, _ctx())

    assert calls.n == 4                                    # every chunk ran despite the cancel


def test_batched_retry_multiplies_calls_per_chunk(monkeypatch):
    """A confused reply is re-asked per chunk; the row driver has no such loop."""
    stage = _stage(batch_size=2, max_retries=2)
    calls = _Calls()

    def confused(*args, **kwargs):
        calls.n += 1
        return {"results": [{"row_number": 0, "label": "L0"}]}   # one short, every time

    monkeypatch.setattr(lt, "call_llm_batch", confused)
    out = _execute(stage, _ctx())

    assert calls.n == 12                                   # 4 chunks x 3 attempts
    assert len(contribution_of(out).row_errors) == 8       # nothing usable for the spend
    assert _second_run_calls(monkeypatch, stage) == 4


# ── the fan-out (default parallelism 4) ──────────────────────────────────────


def test_an_aborting_row_does_not_stop_the_rows_already_queued(monkeypatch):
    """Every row is submitted up front, and the pool's exit drains what it holds."""
    stage = _stage()
    calls = _Calls(fail_on=2, fault=KeyboardInterrupt())
    monkeypatch.setattr(lt, "call_llm", calls.row)

    with pytest.raises(KeyboardInterrupt):
        _execute(stage, _ctx(), parallelism=4)

    assert calls.n == 8            # all 8 paid for, though the abort fired on the 2nd


def test_a_cancel_does_stop_the_rows_already_queued(monkeypatch):
    """The same fan-out, cancelled instead of raised: this path cancels its futures."""
    stage = _stage()
    calls = _Calls()

    def cancel_then_answer(*args, **kwargs):
        request_cancel(PROJECT, "r1")
        return calls.row(*args, **kwargs)

    monkeypatch.setattr(lt, "call_llm", cancel_then_answer)
    with pytest.raises(RunCancelled):
        _execute(stage, _ctx(), parallelism=4)

    assert calls.n < 8


def test_a_failing_cache_write_aborts_the_stage_after_the_call_was_paid_for(monkeypatch):
    """The recording step sits outside the per-row supervisor, so its failure is the stage's."""
    stage = _stage()
    calls = _Calls()
    monkeypatch.setattr(lt, "call_llm", calls.row)
    real_record = StageCache.record
    written = {"n": 0}

    def record(self, **kwargs):
        written["n"] += 1
        if written["n"] == 6:
            raise OSError("database or disk is full")
        real_record(self, **kwargs)

    monkeypatch.setattr(StageCache, "record", record)
    with pytest.raises(OSError):
        _execute(stage, _ctx())

    assert calls.n == 6                                    # the 6th answer was paid for
    assert _second_run_calls(monkeypatch, stage) == 3       # and lost, with the 2 behind it
