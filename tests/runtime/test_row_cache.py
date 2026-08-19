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
from app.runtime.stage_output import StageOutput
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import (
    ROW_CACHED_KEY,
    ROW_DEFERRED_KEY,
    ROW_ERROR_KEY,
    ROW_USAGE_KEY,
    Row,
    RowMapTransformHandler,
    _order_by_input_position,
)
from app.runtime.stages.llm_transform import _accept_completed_chunk, run_llm_batches
from conftest import as_inputs, make_run_context, place_stage, rows_of

PROJECT = "row-cache-tests"

_DOUBLING_CODE = "def transform(row):\n    return {**row, 'y': row['x'] * 2}\n"


def _row_stage(code: str = _DOUBLING_CODE, *, cache: bool = True) -> Stage:
    return parse_stage({
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "cache": cache,
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "src",
                    "columns": [{"name": "x", "type": "int", "nullable": True}],
                },
            ],
            "adds": [{"name": "y", "type": "int", "nullable": True}],
        },
        "function": {"kind": "inline", "code": code},
    })


def _llm_stage(*, batch_size: int = 1, instructions: str = "score it") -> Stage:
    return parse_stage({
        "id": "score", "description": "Score", "type": "llm_transform",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "src",
                       "columns": [{"name": "x", "type": "int", "nullable": True}]}],
            "adds": [{"name": "verdict", "type": "str", "nullable": True}]},
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


def _run(stage: Stage, src: pd.DataFrame, ctx) -> StageOutput:
    out = HANDLERS[StageType(stage.type)].execute(place_stage(stage), as_inputs({"src": src}), ctx)
    assert out is not None
    return out


def _entries(stage: Stage) -> list[StageCacheEntry]:
    return ReadOnlyStageCache().find_entries(
        PROJECT, stage.id, stage.compute_definition_fingerprint()
    )


# ── python_row_function ──────────────────────────────────────────────────────


def _counting_row_handler(calls: list[int], **kwargs) -> RowMapTransformHandler:
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
            calls.append(row["x"])
            return {**row, "y": row["x"] * 2}
        return map_row

    return RowMapTransformHandler(make_mapper=make_mapper, **kwargs)


def test_second_run_reuses_the_cache_and_never_calls_the_mapper():
    stage, src = _row_stage(), _src([1, 2])
    calls: list[int] = []
    handler = _counting_row_handler(calls)

    first = handler.execute(place_stage(stage), as_inputs({"src": src}), _ctx(run_id="run1"))
    assert list(rows_of(first)["y"]) == [2, 4]
    assert calls == [1, 2]

    second = handler.execute(place_stage(stage), as_inputs({"src": src.copy()}), _ctx(run_id="run2"))
    assert list(rows_of(second)["y"]) == [2, 4]
    assert calls == [1, 2]  # the mapper was not called again


def test_registered_python_row_function_replays_a_recorded_row_over_its_own_code():
    stage = _row_stage()
    StageCache().record(
        project_id=PROJECT, stage_id=stage.id,
        stage_fingerprint=stage.compute_definition_fingerprint(),
        input_fingerprint=compute_row_fingerprint({"x": 1}),
        input_row={"x": 1}, output_row={"x": 1, "y": 999},
    )

    out = _run(stage, _src([1]), _ctx(run_id="run1"))
    assert list(rows_of(out)["y"]) == [999]  # the authored `x * 2` would have said 2


def test_changing_the_stage_definition_invalidates_every_row():
    src = _src([1, 2])
    calls: list[int] = []
    _counting_row_handler(calls).execute(place_stage(_row_stage()), as_inputs({"src": src}), _ctx(run_id="run1"))

    changed = _row_stage("def transform(row):\n    return {**row, 'y': row['x'] * 3}\n")
    _counting_row_handler(calls).execute(place_stage(changed), as_inputs({"src": src.copy()}), _ctx(run_id="run2"))
    assert calls == [1, 2, 1, 2]  # a new definition fingerprint: every row recomputed


def test_changing_one_row_invalidates_only_that_row():
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1, 2])}), _ctx(run_id="run1"))

    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1, 9])}), _ctx(run_id="run2"))
    assert calls == [1, 2, 9]  # row x=1 replayed; only the changed row recomputed


def test_a_failed_row_is_never_recorded():
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
            if row["x"] == 2:
                return {**row, ROW_ERROR_KEY: "boom"}
            return {**row, "y": row["x"] * 2}
        return map_row

    stage = _row_stage()
    handler = RowMapTransformHandler(make_mapper=make_mapper)
    handler.execute(place_stage(stage), as_inputs({"src": _src([1, 2])}), _ctx(run_id="run1"))

    recorded = {entry.input_fingerprint for entry in _entries(stage)}
    failed = compute_row_fingerprint({"x": 2})
    assert failed not in recorded
    assert compute_row_fingerprint({"x": 1}) in recorded


def test_cache_false_writes_nothing():
    stage = _row_stage(cache=False)
    calls: list[int] = []
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1])}), _ctx(run_id="run1"))
    assert _entries(stage) == []

    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1])}), _ctx(run_id="run2"))
    assert calls == [1, 1]  # so the second run has nothing to replay


def test_cache_false_reads_nothing_that_is_already_pinned():
    calls: list[int] = []
    _counting_row_handler(calls).execute(
        place_stage(_row_stage(cache=True)), as_inputs({"src": _src([1])}), _ctx(run_id="seed"))
    assert len(_entries(_row_stage())) == 1

    _counting_row_handler(calls).execute(
        place_stage(_row_stage(cache=False)), as_inputs({"src": _src([1])}), _ctx(run_id="uncached"))
    assert calls == [1, 1]  # the pinned row was there to be had, and was not taken


def test_cache_false_does_not_change_the_definition_fingerprint():
    assert (
        _row_stage(cache=True).compute_definition_fingerprint()
        == _row_stage(cache=False).compute_definition_fingerprint()
    )


def test_bust_cache_skips_the_read_but_still_re_pins_the_entry():
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1])}), _ctx(run_id="run1"))
    assert calls == [1]

    _counting_row_handler(calls).execute(
        place_stage(stage), as_inputs({"src": _src([1, 5])}), _ctx(run_id="run2", bust_cache=True))
    assert calls == [1, 1, 5]  # read skipped: x=1 recomputed despite its entry

    _counting_row_handler(calls).execute(
        place_stage(stage), as_inputs({"src": _src([1, 5])}), _ctx(run_id="run3"))
    # x=5 was computed by the busted run and by nothing else, so replaying it
    # here is the evidence that a busted run records.
    assert calls == [1, 1, 5]


def test_a_run_without_project_scope_neither_reads_nor_writes_the_cache():
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1])}), _ctx(run_id="seed"))
    assert len(_entries(stage)) == 1

    ctx = make_run_context()  # identity=None, stage_cache=None
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1])}), ctx)
    assert calls == [1, 1]              # the pinned row was not read
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([5])}), ctx)
    assert calls == [1, 1, 5]
    assert len(_entries(stage)) == 1    # and x=5 was not written


def test_a_read_only_accessor_reuses_a_hit_but_records_nothing():
    stage = _row_stage()
    calls: list[int] = []
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1])}), _ctx(run_id="seed"))
    assert len(_entries(stage)) == 1

    read_only = _ctx(run_id="reader", cache=ReadOnlyStageCache())
    _counting_row_handler(calls).execute(place_stage(stage), as_inputs({"src": _src([1, 5])}), read_only)
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
        place_stage(_row_stage()), as_inputs({"src": _src([1, 2, 3])}), _ctx(run_id="once", cache=cache))
    assert len(calls) == 1


def test_a_post_map_mapper_still_gets_its_post_map_step():
    """The cache wrapper must not hide the original mapper's post-map protocol from the driver."""
    seen: list[int] = []

    class _Mapper:
        def __call__(self, row, index):
            return {**row, "y": row["x"]}

        def finish_mapped_rows(self, stage, df, ctx, contribution):
            seen.append(len(df))

    handler = RowMapTransformHandler(make_mapper=lambda stage, ctx, src: _Mapper())
    handler.execute(place_stage(_row_stage()), as_inputs({"src": _src([1, 2])}), _ctx(run_id="postmap"))
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
    assert list(rows_of(first)["verdict"]) == ["v1", "v2"]
    assert len(calls) == 2

    second = _run(stage, _src([1, 2]), _ctx(run_id="run2"))
    assert list(rows_of(second)["verdict"]) == ["v1", "v2"]
    assert len(calls) == 2  # the model was not called again


def test_a_cached_llm_row_carries_no_usage(monkeypatch):
    """A replayed row cost this run nothing, so keeping usage would report spend never paid."""
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
    assert list(rows_of(out)["verdict"]) == ["v1", "v2"]
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
    assert list(rows_of(out)["x"]) == [1, 2, 7, 8]          # grain and order still hold
    assert list(rows_of(out)["verdict"]) == ["v1", "v2", "v7", "v8"]


def test_batched_misses_that_were_not_adjacent_rejoin_their_own_rows(monkeypatch):
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run(stage, _src([2, 4]), _ctx(run_id="run1"))
    batches.clear()

    out = _run(stage, _src([1, 2, 3, 4, 5]), _ctx(run_id="run2"))
    assert batches == [[1, 3], [5]]
    assert list(rows_of(out)["x"]) == [1, 2, 3, 4, 5]
    assert list(rows_of(out)["verdict"]) == ["v1", "v2", "v3", "v4", "v5"]


def test_a_failed_batch_records_nothing(monkeypatch):
    def failing_call_llm_batch(stage_id, llm, instructions, task, reply_schema, usage_out):
        raise RuntimeError("model down")

    monkeypatch.setattr(
        "app.runtime.stages.llm_transform.call_llm_batch", failing_call_llm_batch)
    stage = _llm_stage(batch_size=2)
    out = _run(stage, _src([1, 2]), _ctx(run_id="run1"))
    assert len(rows_of(out)) == 2          # every row still emitted, carrying its failure
    assert _entries(stage) == []  # and nothing pinned


def test_completed_chunk_is_cached_before_a_later_chunk_crashes(monkeypatch):
    handler = HANDLERS[StageType.llm_transform]

    def crash_after_completed_chunk(
        stage, inputs, ctx, parallelism, positions, on_chunk_completed,
    ):
        source_rows = inputs[stage.inputs[0].id].to_pylist()
        completed = [
            (0, {**source_rows[0], "verdict": "kept"}),
            (1, {**source_rows[1], ROW_ERROR_KEY: "failed"}),
            (2, {**source_rows[2], ROW_DEFERRED_KEY: "waiting"}),
        ]
        on_chunk_completed(completed)
        raise RuntimeError("later chunk crashed")

    monkeypatch.setattr(handler, "run_batches", crash_after_completed_chunk)
    stage = _llm_stage(batch_size=3)

    with pytest.raises(RuntimeError, match="later chunk crashed"):
        _run(stage, _src([1, 2, 3, 4]), _ctx(run_id="run1"))

    entries = _entries(stage)
    assert [entry.frozen_input["x"] for entry in entries] == [1]
    assert entries[0].output_row["verdict"] == "kept"


def test_invalid_completed_chunk_reaches_no_cache_callback():
    accepted: list[tuple[int, Row]] = []

    with pytest.raises(RuntimeError, match=r"expected \[0, 1\]"):
        _accept_completed_chunk(
            [(0, {"x": 1}), (0, {"x": 2})], 0, 2, accepted.extend
        )

    assert accepted == []


def test_batched_bust_cache_skips_the_read_but_re_pins(monkeypatch):
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
    hits = {0: {"x": 0, "from": "cache"}, 3: {"x": 3, "from": "cache"}}
    computed = [{"x": 1, "from": "model"}, {"x": 2, "from": "model"}]

    rows = _order_by_input_position(_llm_stage(), hits, [1, 2], computed, 4)

    assert [row["x"] for row in rows] == [0, 1, 2, 3]
    assert [row["from"] for row in rows] == ["cache", "model", "model", "cache"]


def test_the_scatter_refuses_a_result_that_is_not_one_row_per_miss():
    with pytest.raises(RuntimeError, match="batched execution returned 1 rows"):
        _order_by_input_position(_llm_stage(), {}, [0, 1], [{"x": 0}], 2)


def test_run_llm_batches_computes_every_row_it_is_given(monkeypatch):
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run(stage, _src([1, 2]), _ctx(run_id="seed"))
    batches.clear()

    rows = run_llm_batches(
        place_stage(stage),
        as_inputs({"src": _src([1, 2])}),
        _ctx(run_id="direct"),
        1,
        [0, 1],
        lambda rows: None,
    )

    assert batches == [[1, 2]]
    assert [row["verdict"] for row in rows] == ["v1", "v2"]


# ── the replayed-row count the run page reads ────────────────────────────────


def _replayed(out: StageOutput) -> int | None:
    return out.contribution.cached_rows


def test_a_computing_run_reports_no_replay_count_at_all(monkeypatch):
    """None, not zero — a zero would read as a measurement that was taken."""
    _stub_call_llm(monkeypatch, [])
    assert _replayed(_run(_llm_stage(), _src([1, 2]), _ctx(run_id="run1"))) is None


def test_a_fully_replayed_llm_stage_counts_every_row(monkeypatch):
    stage = _llm_stage()
    _stub_call_llm(monkeypatch, [])
    _run(stage, _src([1, 2]), _ctx(run_id="run1"))

    assert _replayed(_run(stage, _src([1, 2]), _ctx(run_id="run2"))) == 2


def test_a_partly_replayed_batched_stage_counts_only_the_hits(monkeypatch):
    stage = _llm_stage(batch_size=2)
    _stub_call_llm_batch(monkeypatch, [])
    _run(stage, _src([1, 2]), _ctx(run_id="run1"))

    out = _run(stage, _src([1, 2, 7, 8]), _ctx(run_id="run2"))
    assert _replayed(out) == 2


def test_the_replay_marker_never_reaches_stage_output(monkeypatch):
    stage = _llm_stage()
    _stub_call_llm(monkeypatch, [])
    computed = _run(stage, _src([1]), _ctx(run_id="run1"))

    replayed = _run(stage, _src([1]), _ctx(run_id="run2"))
    assert list(rows_of(replayed).columns) == list(rows_of(computed).columns)
    assert ROW_CACHED_KEY not in rows_of(replayed).columns


def test_a_replayed_row_is_never_re_recorded_carrying_the_marker(monkeypatch):
    stage = _llm_stage()
    _stub_call_llm(monkeypatch, [])
    _run(stage, _src([1]), _ctx(run_id="run1"))
    _run(stage, _src([1]), _ctx(run_id="run2"))

    [entry] = _entries(stage)
    assert entry.output_row is not None
    assert ROW_CACHED_KEY not in entry.output_row


# ── every row-mapped registration caches: the shape has no opt-out ───────────


def test_every_row_mapped_stage_type_runs_under_the_interceptor():
    for stage_type in (
        StageType.python_row_function,
        StageType.llm_transform,
        StageType.human_review_queue,
    ):
        handler = HANDLERS[stage_type]
        assert isinstance(handler, RowMapTransformHandler)


# ── narrowing to the signature's declared reads ──────────────────────────────
# Registered on llm_transform, whose reads the model pins to the prompt's
# placeholders. Exercised here through a hand-built handler so the driver's own
# behaviour is what is under test, not an LLM backend.

_NOISE_COLUMN = {"name": "noise", "type": "str", "nullable": True}


_READS_X = [{"input": "src", "columns": [{"name": "x", "type": "int", "nullable": True}]}]


def _two_column_stage(*, reads=None, code: str = _DOUBLING_CODE, cache: bool = False) -> Stage:
    return parse_stage({
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "cache": cache,
        "signature": {
            "form": "extends",
            "reads": _READS_X if reads is None else reads,
            "adds": [{"name": "y", "type": "int", "nullable": True}]},
        "function": {"kind": "inline", "code": code},
    })


def _seen_rows_handler(seen: list[Row], **kwargs) -> RowMapTransformHandler:
    def make_mapper(stage, ctx, src):
        def map_row(row, index):
            seen.append(dict(row))
            return {**row, "y": row["x"] * 2}
        return map_row

    return RowMapTransformHandler(make_mapper=make_mapper, **kwargs)


def _noisy_src(noise: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"x": [1, 2], "noise": noise})


def test_the_mapper_is_handed_only_the_columns_the_signature_reads():
    seen: list[Row] = []
    handler = _seen_rows_handler(seen)

    handler.execute(place_stage(_two_column_stage()), as_inputs({"src": _noisy_src(["a", "b"])}), _ctx())

    assert seen == [{"x": 1}, {"x": 2}]  # `noise` never reached it


def test_an_unread_column_still_flows_to_the_output():
    handler = _seen_rows_handler([])

    out = handler.execute(place_stage(_two_column_stage()), as_inputs({"src": _noisy_src(["a", "b"])}), _ctx())

    assert list(rows_of(out)["noise"]) == ["a", "b"]
    assert list(rows_of(out)["y"]) == [2, 4]


def test_a_column_the_stage_never_reads_stops_invalidating_its_cache():
    stage = _two_column_stage(cache=True)
    calls: list[Row] = []
    handler = _seen_rows_handler(calls)

    handler.execute(place_stage(stage), as_inputs({"src": _noisy_src(["a", "b"])}), _ctx(run_id="run1"))
    assert len(calls) == 2

    # Same `x` values, different `noise` — every row is a hit.
    handler.execute(place_stage(stage), as_inputs({"src": _noisy_src(["CHANGED", "ALSO"])}), _ctx(run_id="run2"))
    assert len(calls) == 2


def test_a_signature_declaring_no_anchor_reads_is_handed_empty_rows():
    seen: list[Row] = []
    with pytest.raises(KeyError, match="x"):
        _seen_rows_handler(seen).execute(
            place_stage(_two_column_stage(reads=[])), as_inputs({"src": _noisy_src(["a", "b"])}), _ctx())
    # So a row-mapped stage that reads its input must say so. The synthesis in
    # scripts/stage_signatures.py and migration 0010 leave no stored stage without
    # reads; this is what an under-declared one now gets.
    assert seen == [{}]


def test_a_registered_row_function_may_not_read_past_its_declared_reads():
    stage = _two_column_stage(
        code="def transform(row):\n    return {**row, 'y': row['noise']}\n")

    with pytest.raises(KeyError, match="noise"):
        _run(stage, _noisy_src(["a", "b"]), _ctx())


# ── the batched path keys on the declared reads, as the row path does ─────────
# A batched chunk is N rows of the row path's shape, so an unread column must
# not reach the key or the entry. Before narrowing it reached both.


_NOTE_SCHEMA = {"columns": [{"name": "x", "type": "int", "nullable": True},
                            {"name": "note", "type": "str", "nullable": True}]}


def _src_with_note(values: list[int], notes: list[str]) -> pd.DataFrame:
    """`note` is neither read by the signature nor injectable by the prompt."""
    return pd.DataFrame({"x": values, "note": notes})


def _run_over_note(stage: Stage, src: pd.DataFrame, ctx) -> StageOutput:
    """Declares the wider input, so `note` flows to the output rather than being trimmed."""
    placed = place_stage(stage, src=_NOTE_SCHEMA)
    out = HANDLERS[StageType(stage.type)].execute(placed, as_inputs({"src": src}), ctx)
    assert out is not None
    return out


def test_a_batched_entry_survives_an_edit_to_a_column_it_never_read(monkeypatch):
    batches: list[list[int]] = []
    _stub_call_llm_batch(monkeypatch, batches)
    stage = _llm_stage(batch_size=2)
    _run_over_note(stage, _src_with_note([1, 2], ["draft", "draft"]), _ctx(run_id="run1"))
    batches.clear()

    out = _run_over_note(stage, _src_with_note([1, 2], ["revised", "revised"]), _ctx(run_id="run2"))
    assert batches == []
    assert list(rows_of(out)["verdict"]) == ["v1", "v2"]
    assert list(rows_of(out)["note"]) == ["revised", "revised"]  # the live value flows


def test_a_batched_entry_holds_the_reads_and_the_adds_only(monkeypatch):
    _stub_call_llm_batch(monkeypatch, [])
    stage = _llm_stage(batch_size=2)
    _run_over_note(stage, _src_with_note([1, 2], ["draft", "draft"]), _ctx(run_id="run1"))

    entries = _entries(stage)
    assert len(entries) == 2
    assert all(sorted(entry.output_row) == ["verdict", "x"] for entry in entries)
    assert all(sorted(entry.frozen_input) == ["x"] for entry in entries)


def test_the_batched_key_matches_the_row_path_key(monkeypatch):
    """Both paths fingerprint the same narrowed row, so neither can key on what it did not see."""
    _stub_call_llm_batch(monkeypatch, [])
    stage = _llm_stage(batch_size=2)
    _run_over_note(stage, _src_with_note([1, 2], ["draft", "draft"]), _ctx(run_id="run1"))

    recorded = {entry.input_fingerprint for entry in _entries(stage)}
    assert recorded == {compute_row_fingerprint({"x": value}) for value in (1, 2)}
