"""The frame-level cache interceptor, which lives in app/runtime/stages/execution.py."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.stage_cache import ReadOnlyStageCache, StageCache, StageCacheEntry
from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.runtime.context import RunIdentity
from app.runtime.manifest import CONTRIBUTION_ATTR
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import FrameTransformHandler
from conftest import make_run_context, place_stage

PROJECT = "frame-cache-tests"

_X = {"columns": [{"name": "x", "type": "int", "nullable": True}]}

_DOUBLING_CODE = "def transform(df):\n    return df.assign(y=df['x'] * 2)\n"


def _frame_stage(code: str = _DOUBLING_CODE, *, cache: bool = True) -> Stage:
    return parse_stage({
        "id": "double", "description": "Double", "type": "python_frame_function",
        "inputs": [{"id": "src"}], "cache": cache,
        "signature": {
            "form": "replaces",
            "reads": [{"input": "src", "columns": _X["columns"]}],
            "produces": [
                {"name": "x", "type": "int", "nullable": True},
                {"name": "y", "type": "int", "nullable": True},
            ],
        },
        "function": {"kind": "inline", "code": code},
    })


def _ctx(*, run_id: str = "r1", cache=None, bust_cache: bool = False):
    return make_run_context(
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache() if cache is None else cache,
        bust_cache=bust_cache,
    )


def _src(values: list[int]) -> pd.DataFrame:
    return pd.DataFrame({"x": values})


def _counting_frame_handler(calls: list[int], **kwargs) -> FrameTransformHandler:
    def apply(stage, inputs, ctx):
        src = inputs[stage.inputs[0].id]
        calls.append(len(src))
        return src.assign(y=src["x"] * 2)

    return FrameTransformHandler(apply=apply, **kwargs)


def _entries(stage: Stage) -> list[StageCacheEntry]:
    return ReadOnlyStageCache().find_entries(
        PROJECT, stage.id, stage.compute_definition_fingerprint()
    )


def _cached_frame(stage: Stage, inputs: list[pd.DataFrame]) -> pd.DataFrame | None:
    return ReadOnlyStageCache().find_cached_frame(
        PROJECT, stage.id, stage.compute_definition_fingerprint(), inputs
    )


# ── python_frame_function ────────────────────────────────────────────────────


def test_a_second_run_returns_the_cached_frame_without_calling_the_transform():
    stage, src = _frame_stage(), _src([1, 2])
    calls: list[int] = []
    handler = _counting_frame_handler(calls)

    first = handler.execute(place_stage(stage), {"src": src}, _ctx(run_id="run1"))
    assert first is not None and list(first["y"]) == [2, 4]
    assert calls == [2]

    second = handler.execute(place_stage(stage), {"src": src.copy()}, _ctx(run_id="run2"))
    assert second is not None and list(second["y"]) == [2, 4]
    assert calls == [2]  # apply was not called again


def test_the_registered_python_frame_function_replays_its_recorded_frame():
    stage, src = _frame_stage(), _src([1, 2])
    StageCache().record_frame(
        project=PROJECT, stage_id=stage.id,
        stage_fingerprint=stage.compute_definition_fingerprint(),
        input_frames=[src], frame=pd.DataFrame({"x": [1, 2], "y": [999, 999]}),
    )

    out = HANDLERS[StageType.python_frame_function].execute(place_stage(stage), {"src": src}, _ctx(run_id="run1"))
    assert out is not None
    assert list(out["y"]) == [999, 999]  # the authored `x * 2` would have said [2, 4]


def test_a_definition_change_invalidates_the_cached_frame():
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(_frame_stage()), {"src": _src([1])}, _ctx())

    changed = _frame_stage("def transform(df):\n    return df.assign(y=df['x'] * 3)\n")
    _counting_frame_handler(calls).execute(place_stage(changed), {"src": _src([1])}, _ctx(run_id="r2"))
    assert calls == [1, 1]  # a new definition fingerprint: recomputed


def test_a_changed_input_cell_invalidates_the_cached_frame():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1, 2])}, _ctx())
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1, 9])}, _ctx(run_id="r2"))
    assert calls == [2, 2]


def test_reordering_the_input_rows_invalidates_the_cached_frame():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1, 2])}, _ctx())
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([2, 1])}, _ctx(run_id="r2"))
    assert calls == [2, 2]


def _two_input_stage() -> Stage:
    return parse_stage({
        "id": "merge", "description": "Merge", "type": "python_frame_function",
        "inputs": [{"id": "left"}, {"id": "right"}],
        "signature": {
            "form": "replaces",
            "reads": [
                {"input": "left", "columns": _X["columns"]},
                {"input": "right", "columns": _X["columns"]},
            ],
            "produces": _X["columns"],
        },
        "function": {"kind": "inline",
                     "code": "def transform(left, right):\n    return left\n"},
    })


def test_the_key_covers_every_input_in_declared_order():
    stage = _two_input_stage()
    left = _src([1, 2])
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"left": left, "right": pd.DataFrame({"z": ["a"]})}, _ctx())
    _counting_frame_handler(calls).execute(place_stage(stage), {"left": left, "right": pd.DataFrame({"z": ["b"]})}, _ctx(run_id="r2"))
    assert calls == [2, 2]


# ── joins and aggregate: bounded primitives, not worth a hash ────────────────


def _enrich_stage() -> Stage:
    return parse_stage({
        "id": "j", "description": "Enrich", "type": "enrich",
        "inputs": [{"id": "left"},
                   {"id": "right"}],
        "signature": {
            "form": "extends",
            "reads": [
                {"input": "left", "columns": _X["columns"]},
                {"input": "right", "columns": _X["columns"]},
            ],
            "adds": [{"name": "z", "type": "str", "nullable": True}],
        },
        "join": {"keys": [{"left": "x", "right": "x"}], "enrich_with": {"z": "z"}},
    })


def _aggregate_stage() -> Stage:
    return parse_stage({
        "id": "agg", "description": "Agg", "type": "aggregate",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "replaces",
            "reads": [
                {
                    "input": "src",
                    "columns": [{"name": "g", "type": "str", "nullable": True}],
                },
            ],
            "produces": [
                {"name": "g", "type": "str", "nullable": True},
                {"name": "n", "type": "int", "nullable": True},
            ],
        },
        "aggregate": {"group_by": ["g"], "aggregations": [
            {"output_column": "n", "formula": "count"}]},
    })


def test_enrich_computes_every_run_and_records_nothing():
    """Fingerprinting an enrich's two input frames costs more than the join a hit would skip."""
    stage = _enrich_stage()
    left, right = pd.DataFrame({"x": [1, 2]}), pd.DataFrame({"x": [1], "z": ["a"]})
    out = HANDLERS[StageType.enrich].execute(place_stage(stage), {"left": left, "right": right}, _ctx())
    assert out is not None and list(out["x"]) == [1, 2]
    assert out["z"].tolist()[0] == "a" and pd.isna(out["z"].tolist()[1])

    assert _cached_frame(stage, [left, right]) is None
    assert _entries(stage) == []


def test_aggregate_computes_every_run_and_records_nothing():
    stage = _aggregate_stage()
    src = pd.DataFrame({"g": ["a", "a", "b"]})
    out = HANDLERS[StageType.aggregate].execute(place_stage(stage), {"src": src}, _ctx())
    assert out is not None and sorted(out["n"]) == [1, 2]

    assert _cached_frame(stage, [src]) is None
    assert _entries(stage) == []


# ── the gating conditions ────────────────────────────────────────────────────


def test_cache_false_writes_nothing():
    stage = _frame_stage(cache=False)
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx())
    assert _cached_frame(stage, [_src([1])]) is None

    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx(run_id="r2"))
    assert calls == [1, 1]  # so the second run has nothing to replay


def test_cache_false_reads_nothing_that_is_already_pinned():
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(_frame_stage(cache=True)), {"src": _src([1])}, _ctx(run_id="seed"))
    assert _cached_frame(_frame_stage(), [_src([1])]) is not None

    _counting_frame_handler(calls).execute(place_stage(_frame_stage(cache=False)), {"src": _src([1])}, _ctx(run_id="uncached"))
    assert calls == [1, 1]  # the pinned frame was there to be had, and was not taken


def test_bust_cache_skips_the_read_but_still_re_pins():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx())

    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx(run_id="r2", bust_cache=True))
    assert calls == [1, 1]  # read skipped: the pinned frame was recomputed

    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1, 2])}, _ctx(run_id="r3", bust_cache=True))
    assert calls == [1, 1, 2]  # a frame nothing has pinned

    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1, 2])}, _ctx(run_id="r4"))
    # The two-row frame was computed by a busted run and by nothing else, so
    # replaying it here is the evidence that a busted run records.
    assert calls == [1, 1, 2]


def test_a_run_without_project_scope_neither_reads_nor_writes_the_cache():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx(run_id="seed"))
    assert _cached_frame(stage, [_src([1])]) is not None

    ctx = make_run_context()  # identity=None, stage_cache=None
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, ctx)
    assert calls == [1, 1]                            # the pinned frame was not read
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([5])}, ctx)
    assert calls == [1, 1, 1]
    assert _cached_frame(stage, [_src([5])]) is None  # and nothing was written


def test_a_read_only_accessor_reuses_a_hit_but_records_nothing():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx(run_id="seed"))

    read_only = _ctx(run_id="reader", cache=ReadOnlyStageCache())
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, read_only)
    assert calls == [1]  # the seeded frame replayed

    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([5])}, read_only)
    assert calls == [1, 1]                       # a miss: computed
    assert _cached_frame(stage, [_src([5])]) is None  # and NOT recorded


def test_a_handler_that_returns_none_records_nothing():
    stage = _frame_stage()
    handler = FrameTransformHandler(apply=lambda stage, inputs, ctx: None)
    assert handler.execute(place_stage(stage), {"src": _src([1])}, _ctx()) is None
    assert _cached_frame(stage, [_src([1])]) is None


# ── which registrations opt out ──────────────────────────────────────────────


def test_only_the_unbounded_frame_shaped_type_caches():
    assert _frame_handler(StageType.python_frame_function).caches_frames is True
    for stage_type in (StageType.enrich, StageType.expand, StageType.aggregate,
                       StageType.publish):
        assert _frame_handler(stage_type).caches_frames is False


def _frame_handler(stage_type: StageType) -> FrameTransformHandler:
    handler = HANDLERS[stage_type]
    assert isinstance(handler, FrameTransformHandler)
    return handler


def test_publish_runs_its_side_effect_every_run_and_writes_no_entry(tmp_path):
    code = (
        "import pandas as pd\n"
        "CALLS = []\n"
        "def transform(df, output_dir):\n"
        "    CALLS.append(len(df))\n"
        "    return pd.DataFrame({'path': [output_dir]})\n"
    )
    stage = parse_stage({
        "id": "pub", "description": "Publish", "type": "publish",
        "inputs": [{"id": "src"}],
        "publish": {"destination": "build/"}, "signature": {"form": "replaces"},
        "function": {"kind": "inline", "code": code},
    })
    handler = HANDLERS[StageType.publish]
    for run_id in ("run1", "run2"):
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        out = handler.execute(place_stage(stage), {"src": _src([1])}, make_run_context(
            run_dir=run_dir,
            identity=RunIdentity(project=PROJECT, run_id=run_id),
            stage_cache=StageCache(),
        ))
        # The side effect happened this run: the path names THIS run's dir.
        assert out is not None and run_id in out["path"].iloc[0]
    assert _cached_frame(stage, [_src([1])]) is None


# ── an uncacheable frame is surfaced, not swallowed ──────────────────────────


def test_a_frame_parquet_cannot_serialize_leaves_the_run_uncached_with_a_note():
    stage = _frame_stage()

    def apply(stage, inputs, ctx):
        return pd.DataFrame({"x": [{"nested": np.array([1, 2])}, 3]})

    out = FrameTransformHandler(apply=apply).execute(place_stage(stage), {"src": _src([1])}, _ctx())
    assert out is not None and len(out) == 2       # the run succeeded
    assert _cached_frame(stage, [_src([1])]) is None  # uncached

    notes = out.attrs[CONTRIBUTION_ATTR].notes
    assert len(notes) == 1 and "uncached" in notes[0]


# ── an unconfigured frame store skips caching, loudly, and never fails ────────


def test_no_frame_store_configured_computes_normally_and_caches_nothing(monkeypatch):
    monkeypatch.setattr("app.core.frames._frame_store", None)
    stage = _frame_stage()
    calls: list[int] = []

    out = _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1, 2])}, _ctx())
    assert out is not None and list(out["y"]) == [2, 4]
    assert calls == [2]

    notes = out.attrs[CONTRIBUTION_ATTR].notes
    assert len(notes) == 1
    assert "no frame store" in notes[0] and stage.id in notes[0]


def test_no_frame_store_configured_leaves_nothing_to_replay(monkeypatch):
    monkeypatch.setattr("app.core.frames._frame_store", None)
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx(run_id="r1"))
    _counting_frame_handler(calls).execute(place_stage(stage), {"src": _src([1])}, _ctx(run_id="r2"))
    assert calls == [1, 1]  # nothing was pinned, so nothing replayed


def test_a_deliberate_opt_out_carries_no_note(monkeypatch):
    calls: list[int] = []
    out = _counting_frame_handler(calls).execute(place_stage(_frame_stage(cache=False)), {"src": _src([1])}, _ctx())
    assert out is not None
    assert CONTRIBUTION_ATTR not in out.attrs

    out = _counting_frame_handler(calls).execute(place_stage(_frame_stage()), {"src": _src([1])}, make_run_context())
    assert out is not None
    assert CONTRIBUTION_ATTR not in out.attrs
