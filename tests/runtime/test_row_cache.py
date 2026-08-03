"""`python_row_function` and a batch_size-1 `llm_transform` share one
`_run_row_mapper` cache wrapper, so both are exercised here."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.stage_cache import (
    ReadOnlyStageCache,
    StageCache,
    StageCacheEntry,
    compute_row_fingerprint,
)
from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.runtime.context import RunIdentity
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import (
    ROW_ERROR_KEY,
    ROW_USAGE_KEY,
    RowMapHandler,
    _order_by_input_position,
)
from app.runtime.stages.llm_transform import run_llm_batches
from conftest import make_run_context

PROJECT = "row-cache-tests"

_DOUBLING_CODE = "def transform(row):\n    return {**row, 'y': row['x'] * 2}\n"


def _row_stage(code: str = _DOUBLING_CODE, *, cache: bool = True) -> Stage:
    return parse_stage({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": {"columns": [{"name": "x", "type": "int"}]}}],
        "cache": cache,
        "output_schema": {
            "columns": [{"name": "x", "type": "int"}, {"name": "y", "type": "int"}]},
        "function": {"code": code},
    })


def _llm_stage(*, batch_size: int = 1, instructions: str = "score it") -> Stage:
    return parse_stage({
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


def test_registered_python_row_function_replays_a_recorded_row_over_its_own_code():
    """Through the REGISTERED handler, not a hand-built one. The recorded row is
    seeded with an answer the authored function would never produce, so the value
    that comes back is itself the evidence of a replay — re-running the stage and
    reading the store back would only show that entries exist."""
    stage = _row_stage()
    StageCache().record(
        project=PROJECT, stage_id=stage.id,
        stage_fingerprint=stage.compute_definition_fingerprint(),
        input_fingerprint=compute_row_fingerprint({"x": 1}),
        input_row={"x": 1}, output_row={"x": 1, "y": 999},
    )

    out = _run(stage, _src([1]), _ctx(run_id="run1"))
    assert list(out["y"]) == [999]  # the authored `x * 2` would have said 2


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


def test_cache_false_writes_nothing():
    stage = _row_stage(cache=False)
    calls: list[int] = []
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="run1"))
    assert _entries(stage) == []

    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="run2"))
    assert calls == [1, 1]  # so the second run has nothing to replay


def test_cache_false_reads_nothing_that_is_already_pinned():
    """The read half of the opt-out, which the write test above cannot reach: a
    run that wrote nothing has nothing to replay, so re-rolling proves only that
    the store is empty. `cache` stays out of the definition fingerprint (see the
    test below), so the SAME stage cached once leaves an entry the uncached stage
    would find if it looked — and it must still re-roll."""
    calls: list[int] = []
    _counting_row_handler(calls).execute(
        _row_stage(cache=True), {"src": _src([1])}, _ctx(run_id="seed"))
    assert len(_entries(_row_stage())) == 1

    _counting_row_handler(calls).execute(
        _row_stage(cache=False), {"src": _src([1])}, _ctx(run_id="uncached"))
    assert calls == [1, 1]  # the pinned row was there to be had, and was not taken


def test_cache_false_does_not_change_the_definition_fingerprint():
    assert (
        _row_stage(cache=True).compute_definition_fingerprint()
        == _row_stage(cache=False).compute_definition_fingerprint()
    )


def test_bust_cache_skips_the_read_but_still_re_pins_the_entry():
    """Re-pinned, not merely stale. A busted run over a row that is ALREADY
    pinned proves only that the read was skipped: the run after it hits the first
    run's entry either way, so the recording half of the claim goes untested. The
    busted run is therefore also shown x=5, which nothing has ever pinned — the
    run after it can replay x=5 only if the busted run recorded what it
    recomputed."""
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="run1"))
    assert calls == [1]

    _counting_row_handler(calls).execute(
        stage, {"src": _src([1, 5])}, _ctx(run_id="run2", bust_cache=True))
    assert calls == [1, 1, 5]  # read skipped: x=1 recomputed despite its entry

    _counting_row_handler(calls).execute(
        stage, {"src": _src([1, 5])}, _ctx(run_id="run3"))
    # x=5 was computed by the busted run and by nothing else, so replaying it
    # here is the evidence that a busted run records.
    assert calls == [1, 1, 5]


def test_a_run_without_project_scope_touches_the_cache_at_all():
    """A subset/preview run carries no identity and no cache accessor, so the
    interceptor never opens — the mapper answers every row. The row is pinned by
    a scoped run FIRST, so the un-scoped run walks past an entry that was there
    to be had; without the seed, re-computing would prove only an empty store."""
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="seed"))
    assert len(_entries(stage)) == 1

    ctx = make_run_context()  # identity=None, stage_cache=None
    _counting_row_handler(calls).execute(stage, {"src": _src([1])}, ctx)
    assert calls == [1, 1]              # the pinned row was not read
    _counting_row_handler(calls).execute(stage, {"src": _src([5])}, ctx)
    assert calls == [1, 1, 5]
    assert len(_entries(stage)) == 1    # and x=5 was not written


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


# ── llm_transform, batch_size > 1 (the batched path bypasses the row driver) ─


def _stub_call_llm_batch(monkeypatch, batches: list[list[int]]) -> None:
    """Answer a batched call from the rendered task alone: each item's task text
    is the row's `x`, so the stub records which rows the model was shown."""
    def fake_call_llm_batch(stage_id, llm, instructions, task, reply_schema, usage_out):
        shown = [
            int(block.splitlines()[1])
            for block in task.split("### item ")[1:]
        ]
        batches.append(shown)
        return {"results": [
            {"row_number": number, "verdict": f"v{value}"}
            for number, value in enumerate(shown)
        ]}

    monkeypatch.setattr(
        "app.runtime.stages.llm_transform.call_llm_batch", fake_call_llm_batch)


def test_batched_path_caches_every_computed_row(monkeypatch):
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)

    out = _run(stage, _src([1, 2]), _ctx(run_id="run1"))
    assert list(out["verdict"]) == ["v1", "v2"]
    assert batches == [[1, 2]]
    assert len(_entries(stage)) == 2


def test_a_partial_batched_hit_calls_the_model_for_the_misses_only(monkeypatch):
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run(stage, _src([1, 2]), _ctx(run_id="run1"))
    batches.clear()

    out = _run(stage, _src([1, 2, 7, 8]), _ctx(run_id="run2"))
    assert batches == [[7, 8]]  # rows 1 and 2 replayed; only the misses batched
    assert list(out["x"]) == [1, 2, 7, 8]          # grain and order still hold
    assert list(out["verdict"]) == ["v1", "v2", "v7", "v8"]


def test_batched_misses_that_were_not_adjacent_rejoin_their_own_rows(monkeypatch):
    """A chunk is packed from the misses alone, so its rows need not be adjacent
    in the input — each reply must still land on the row that produced it."""
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run(stage, _src([2, 4]), _ctx(run_id="run1"))
    batches.clear()

    out = _run(stage, _src([1, 2, 3, 4, 5]), _ctx(run_id="run2"))
    assert batches == [[1, 3], [5]]
    assert list(out["x"]) == [1, 2, 3, 4, 5]
    assert list(out["verdict"]) == ["v1", "v2", "v3", "v4", "v5"]


def test_a_failed_batch_records_nothing(monkeypatch):
    def failing_call_llm_batch(stage_id, llm, instructions, task, reply_schema, usage_out):
        raise RuntimeError("model down")

    monkeypatch.setattr(
        "app.runtime.stages.llm_transform.call_llm_batch", failing_call_llm_batch)
    stage = _llm_stage(batch_size=2)
    out = _run(stage, _src([1, 2]), _ctx(run_id="run1"))
    assert len(out) == 2          # every row still emitted, carrying its failure
    assert _entries(stage) == []  # and nothing pinned


def test_batched_bust_cache_skips_the_read_but_re_pins(monkeypatch):
    """The batched path's version of the row-grain test above, and shaped the
    same way: the busted run is shown x=7, which nothing has ever pinned, so the
    run after it is what proves the busted run recorded rather than merely
    skipped its reads."""
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run(stage, _src([1, 2]), _ctx(run_id="run1"))
    batches.clear()

    _run(stage, _src([1, 2, 7]), _ctx(run_id="run2", bust_cache=True))
    assert batches == [[1, 2], [7]]  # read skipped: every row batched, pinned or not
    batches.clear()

    _run(stage, _src([1, 2, 7]), _ctx(run_id="run3"))
    # x=7 was computed by the busted run and by nothing else.
    assert batches == []


def test_batched_path_without_project_scope_calls_the_model_every_time(monkeypatch):
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run(stage, _src([1, 2]), make_run_context())
    _run(stage, _src([1, 2]), make_run_context())
    assert batches == [[1, 2], [1, 2]]


# ── the batched path's scatter back into input order ────────────────────────


def test_the_scatter_puts_every_row_back_in_its_own_input_position():
    """The shape assembles hits and computed rows by INPUT position, never by
    the order either arrived in: the computed rows come back in miss order and
    land on the positions the misses came from."""
    hits = {0: {"x": 0, "from": "cache"}, 3: {"x": 3, "from": "cache"}}
    computed = [{"x": 1, "from": "model"}, {"x": 2, "from": "model"}]

    rows = _order_by_input_position(_llm_stage(), hits, [1, 2], computed, 4)

    assert [row["x"] for row in rows] == [0, 1, 2, 3]
    assert [row["from"] for row in rows] == ["cache", "model", "model", "cache"]


def test_the_scatter_refuses_a_result_that_is_not_one_row_per_miss():
    """A missing computed row would silently mis-grain the stage, so the gap is
    raised instead of filled."""
    with pytest.raises(RuntimeError, match="batched execution returned 1 rows"):
        _order_by_input_position(_llm_stage(), {}, [0, 1], [{"x": 0}], 2)


def test_run_llm_batches_computes_every_row_it_is_given(monkeypatch):
    """The stage module resolves nothing: called directly with rows the cache
    could answer, it still asks the model about all of them. Which rows reach it
    is the shape's decision alone."""
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run(stage, _src([1, 2]), _ctx(run_id="seed"))
    batches.clear()

    rows = run_llm_batches(stage, {"src": _src([1, 2])}, _ctx(run_id="direct"), 1, [0, 1])

    assert batches == [[1, 2]]
    assert [row["verdict"] for row in rows] == ["v1", "v2"]


# ── every row-mapped registration caches: the shape has no opt-out ───────────


def test_every_row_mapped_stage_type_runs_under_the_interceptor():
    """No registration may exempt itself from the row cache — the interceptor is
    a property of the shape, so a type that wants different caching has to say so
    in `Stage.cache`, which is per-stage and visible to the author."""
    for stage_type in (
        StageType.python_row_function,
        StageType.llm_transform,
        StageType.human_review_queue,
    ):
        handler = HANDLERS[stage_type]
        assert isinstance(handler, RowMapHandler)
