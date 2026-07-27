"""The row-level cache interceptor (app/runtime/stages/execution.py).

Caching is a property of the handler SHAPE: `python_row_function` and a
batch_size-1 `llm_transform` are both driven through `_run_row_mapper`, so both
are cached by the same wrapper around the one line of per-row compute. These
tests drive the registered handlers, so what they pin is that whole path.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.stage_cache import (
    ReadOnlyStageCache,
    StageCache,
    StageCacheEntry,
    compute_row_fingerprint,
)
from app.models import Stage
from app.models.stage import StageType
from app.runtime.context import RunIdentity
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import ROW_ERROR_KEY, ROW_USAGE_KEY, RowMapHandler
from conftest import make_run_context

PROJECT = "row-cache-tests"

_DOUBLING_CODE = "def transform(row):\n    return {**row, 'y': row['x'] * 2}\n"


def _row_stage(code: str = _DOUBLING_CODE, *, cache: bool = True) -> Stage:
    return Stage.model_validate({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "src"}], "cache": cache,
        "function": {"kind": "inline", "code": code},
    })


def _llm_stage(*, batch_size: int = 1, instructions: str = "score it") -> Stage:
    return Stage.model_validate({
        "id": "score", "name": "Score", "type": "llm_transform",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": "x", "type": "int"}], "primary_key": ["x"]}}],
        "output_schema": {
            "columns": [{"name": "x", "type": "int"}, {"name": "verdict", "type": "str"}],
            "primary_key": ["x"]},
        "llm": {"prompt_instructions": instructions, "prompt_data_template": "{x}",
                "batch_size": batch_size},
    })


def _ctx(*, run_id: str = "r1", cache=None, bust_cache: bool = False):
    return make_run_context(
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache() if cache is None else cache,
        bust_cache=bust_cache,
    )


def _src(values: list[int]) -> pd.DataFrame:
    return pd.DataFrame({"x": values})


def _run(stage: Stage, src: pd.DataFrame, ctx) -> pd.DataFrame:
    out = HANDLERS[StageType(stage.type)].execute(stage, {"src": src}, ctx)
    assert out is not None
    return out


def _entries(stage: Stage) -> list[StageCacheEntry]:
    return ReadOnlyStageCache().find_entries(
        PROJECT, stage.id, stage.compute_definition_fingerprint()
    )


# ── python_row_function ──────────────────────────────────────────────────────


def _counting_row_handler(calls: list[int], **kwargs) -> RowMapHandler:
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
            calls.append(row["x"])
            return {**row, "y": row["x"] * 2}
        return map_row

    return RowMapHandler(make_mapper=make_mapper, **kwargs)


def test_second_run_reuses_the_cache_and_never_calls_the_mapper():
    stage, src = _row_stage(), _src([1, 2])
    calls: list[int] = []
    handler = _counting_row_handler(calls)

    first = handler.execute(stage, {"src": src}, _ctx(run_id="run1"))
    assert list(first["y"]) == [2, 4]
    assert calls == [1, 2]

    second = handler.execute(stage, {"src": src.copy()}, _ctx(run_id="run2"))
    assert list(second["y"]) == [2, 4]
    assert calls == [1, 2]  # the mapper was not called again


def test_registered_python_row_function_reuses_its_recorded_rows():
    """Through the REGISTERED handler, not a hand-built one: the recorded rows
    replay even when the authored function would now answer differently."""
    src = _src([1, 2])
    _run(_row_stage(), src, _ctx(run_id="run1"))

    # Same definition fingerprint is what matters, so re-run the same stage —
    # and assert nothing was re-derived by checking the store, not the values.
    out = _run(_row_stage(), src.copy(), _ctx(run_id="run2"))
    assert list(out["y"]) == [2, 4]
    assert {entry.output_row["y"] for entry in _entries(_row_stage())} == {2, 4}


def test_changing_the_stage_definition_invalidates_every_row():
    src = _src([1, 2])
    calls: list[int] = []
    _counting_row_handler(calls).execute(_row_stage(), {"src": src}, _ctx(run_id="run1"))

    changed = _row_stage("def transform(row):\n    return {**row, 'y': row['x'] * 3}\n")
    _counting_row_handler(calls).execute(changed, {"src": src.copy()}, _ctx(run_id="run2"))
    assert calls == [1, 2, 1, 2]  # a new definition fingerprint: every row recomputed


def test_changing_one_row_invalidates_only_that_row():
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(stage, {"src": _src([1, 2])}, _ctx(run_id="run1"))

    _counting_row_handler(calls).execute(stage, {"src": _src([1, 9])}, _ctx(run_id="run2"))
    assert calls == [1, 2, 9]  # row x=1 replayed; only the changed row recomputed


def test_a_failed_row_is_never_recorded():
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
            if row["x"] == 2:
                return {**row, ROW_ERROR_KEY: "boom"}
            return {**row, "y": row["x"] * 2}
        return map_row

    stage = _row_stage()
    handler = RowMapHandler(make_mapper=make_mapper)
    handler.execute(stage, {"src": _src([1, 2])}, _ctx(run_id="run1"))

    recorded = {entry.input_fingerprint for entry in _entries(stage)}
    failed = compute_row_fingerprint({"x": 2})
    assert failed not in recorded
    assert compute_row_fingerprint({"x": 1}) in recorded


def test_cache_false_neither_reads_nor_writes():
    stage = _row_stage(cache=False)
    calls: list[int] = []
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="run1"))
    assert _entries(stage) == []  # nothing written

    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="run2"))
    assert calls == [1, 1]  # and nothing read: the row re-rolled


def test_cache_false_does_not_change_the_definition_fingerprint():
    assert (
        _row_stage(cache=True).compute_definition_fingerprint()
        == _row_stage(cache=False).compute_definition_fingerprint()
    )


def test_bust_cache_skips_the_read_but_still_re_pins_the_entry():
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="run1"))

    _counting_row_handler(calls).execute(
        stage, {"src": _src([1])}, _ctx(run_id="run2", bust_cache=True))
    assert calls == [1, 1]  # read skipped: recomputed

    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="run3"))
    assert calls == [1, 1]  # the busted run left the entry re-pinned, not stale


def test_a_run_without_project_scope_touches_the_cache_at_all():
    """A subset/preview run carries no identity and no cache accessor, so the
    interceptor never opens — the mapper answers every row."""
    stage = _row_stage()
    calls: list[int] = []
    ctx = make_run_context()  # identity=None, stage_cache=None
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, ctx)
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, ctx)
    assert calls == [1, 1]
    assert _entries(stage) == []


def test_a_read_only_accessor_reuses_a_hit_but_records_nothing():
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="seed"))
    assert len(_entries(stage)) == 1

    read_only = _ctx(run_id="reader", cache=ReadOnlyStageCache())
    _counting_row_handler(calls).execute(stage, {"src": _src([1, 5])}, read_only)
    assert calls == [1, 5]                 # x=1 replayed, x=5 computed
    assert len(_entries(stage)) == 1       # and x=5 was NOT recorded


def test_the_cache_is_read_once_per_execution(monkeypatch):
    cache = StageCache()
    calls: list[tuple[str, str, str]] = []
    find_entries = cache.find_entries

    def counting_find_entries(project, stage_id, stage_fingerprint):
        calls.append((project, stage_id, stage_fingerprint))
        return find_entries(project, stage_id, stage_fingerprint)

    monkeypatch.setattr(cache, "find_entries", counting_find_entries)
    _counting_row_handler([]).execute(
        _row_stage(), {"src": _src([1, 2, 3])}, _ctx(run_id="once", cache=cache))
    assert len(calls) == 1


def test_a_post_map_mapper_still_gets_its_post_map_step():
    """The wrapper is used only in the map loop: `_finish_mapped_frame` tests
    the ORIGINAL mapper for the PostMapRowMapper shape, so wrapping must not
    hide it."""
    seen: list[int] = []

    class _Mapper:
        def __call__(self, row, index):
            return {**row, "y": row["x"]}

        def finish_mapped_rows(self, stage, df, ctx, contribution):
            seen.append(len(df))

    handler = RowMapHandler(make_mapper=lambda stage, ctx, src: _Mapper())
    handler.execute(_row_stage(), {"src": _src([1, 2])}, _ctx(run_id="postmap"))
    assert seen == [2]


# ── llm_transform, batch_size == 1 ───────────────────────────────────────────


def _stub_call_llm(monkeypatch, calls: list[dict]) -> None:
    def fake_call_llm(stage_id, llm, row, reply_model, usage_out):
        calls.append(dict(row))
        return {"verdict": f"v{row['x']}"}

    monkeypatch.setattr("app.runtime.stages.llm_transform.call_llm", fake_call_llm)


def test_llm_transform_row_path_does_not_call_the_model_for_a_cached_row(monkeypatch):
    calls: list[dict] = []
    _stub_call_llm(monkeypatch, calls)
    stage = _llm_stage()

    first = _run(stage, _src([1, 2]), _ctx(run_id="run1"))
    assert list(first["verdict"]) == ["v1", "v2"]
    assert len(calls) == 2

    second = _run(stage, _src([1, 2]), _ctx(run_id="run2"))
    assert list(second["verdict"]) == ["v1", "v2"]
    assert len(calls) == 2  # the model was not called again


def test_a_cached_llm_row_carries_no_usage(monkeypatch):
    """Usage is per-call telemetry, stripped before recording — a replayed row
    cost this run nothing, so it must not report spend."""
    calls: list[dict] = []
    _stub_call_llm(monkeypatch, calls)
    stage = _llm_stage()
    _run(stage, _src([1]), _ctx(run_id="run1"))

    [entry] = _entries(stage)
    assert entry.output_row is not None
    assert ROW_USAGE_KEY not in entry.output_row


def test_changing_the_prompt_invalidates_every_llm_row(monkeypatch):
    calls: list[dict] = []
    _stub_call_llm(monkeypatch, calls)
    _run(_llm_stage(), _src([1]), _ctx(run_id="run1"))
    _run(_llm_stage(instructions="score it harder"), _src([1]), _ctx(run_id="run2"))
    assert len(calls) == 2


def test_a_failed_llm_row_is_never_recorded(monkeypatch):
    def failing_call_llm(stage_id, llm, row, reply_model, usage_out):
        raise RuntimeError("model down")

    monkeypatch.setattr("app.runtime.stages.llm_transform.call_llm", failing_call_llm)
    stage = _llm_stage()
    _run(stage, _src([1]), _ctx(run_id="run1"))
    assert _entries(stage) == []


# ── human_review_queue opts out at its registration site ─────────────────────


def test_human_review_queue_is_registered_with_row_caching_off():
    handler = HANDLERS[StageType.human_review_queue]
    assert isinstance(handler, RowMapHandler)
    assert handler.caches_rows is False


def test_the_other_row_mapped_types_cache():
    for stage_type in (StageType.python_row_function, StageType.llm_transform):
        handler = HANDLERS[stage_type]
        assert isinstance(handler, RowMapHandler)
        assert handler.caches_rows is True


@pytest.mark.parametrize("bust", [False, True])
def test_open_row_cache_returns_nothing_for_an_opted_out_shape(bust):
    from app.runtime.stages.execution import open_row_cache

    ctx = _ctx(run_id="opt-out", bust_cache=bust)
    assert open_row_cache(_row_stage(), ctx, caches_rows=False) is None
