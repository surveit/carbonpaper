"""The frame-level cache interceptor (app/runtime/stages/execution.py).

Caching is a property of the handler SHAPE: every `FrameHandler` is intercepted
by the same wrapper around `apply` — one cache entry for the whole output frame,
keyed by the stage definition plus every input frame in declared order.

`publish` excludes itself at the registration site, because a publish is read by
the world rather than by future runs. `python_frame_function` runs unbounded user
code and caches.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.stage_cache import ReadOnlyStageCache, StageCache, StageCacheEntry
from app.models import Stage
from app.models.stage import StageType
from app.runtime.context import RunIdentity
from app.runtime.manifest import CONTRIBUTION_ATTR
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import FrameHandler
from conftest import make_run_context

PROJECT = "frame-cache-tests"

_DOUBLING_CODE = "def transform(df):\n    return df.assign(y=df['x'] * 2)\n"


def _frame_stage(code: str = _DOUBLING_CODE, *, cache: bool = True) -> Stage:
    return Stage.model_validate({
        "id": "double", "name": "Double", "type": "python_frame_function",
        "inputs": [{"id": "src"}], "cache": cache,
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


def _counting_frame_handler(calls: list[int], **kwargs) -> FrameHandler:
    def apply(stage, inputs, ctx):
        src = inputs[stage.inputs[0].id]
        calls.append(len(src))
        return src.assign(y=src["x"] * 2)

    return FrameHandler(apply=apply, **kwargs)


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

    first = handler.execute(stage, {"src": src}, _ctx(run_id="run1"))
    assert first is not None and list(first["y"]) == [2, 4]
    assert calls == [2]

    second = handler.execute(stage, {"src": src.copy()}, _ctx(run_id="run2"))
    assert second is not None and list(second["y"]) == [2, 4]
    assert calls == [2]  # apply was not called again


def test_the_registered_python_frame_function_replays_its_recorded_frame():
    stage, src = _frame_stage(), _src([1, 2])
    handler = HANDLERS[StageType.python_frame_function]
    handler.execute(stage, {"src": src}, _ctx(run_id="run1"))

    replayed = _cached_frame(stage, [src])
    assert replayed is not None
    assert list(replayed["y"]) == [2, 4]


def test_a_definition_change_invalidates_the_cached_frame():
    calls: list[int] = []
    _counting_frame_handler(calls).execute(_frame_stage(), {"src": _src([1])}, _ctx())

    changed = _frame_stage("def transform(df):\n    return df.assign(y=df['x'] * 3)\n")
    _counting_frame_handler(calls).execute(changed, {"src": _src([1])}, _ctx(run_id="r2"))
    assert calls == [1, 1]  # a new definition fingerprint: recomputed


def test_a_changed_input_cell_invalidates_the_cached_frame():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(stage, {"src": _src([1, 2])}, _ctx())
    _counting_frame_handler(calls).execute(stage, {"src": _src([1, 9])}, _ctx(run_id="r2"))
    assert calls == [2, 2]


def test_reordering_the_input_rows_invalidates_the_cached_frame():
    """Order-sensitivity is a claim about correctness, not a comment: a whole-
    frame transform may depend on row order, so a reorder must recompute."""
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(stage, {"src": _src([1, 2])}, _ctx())
    _counting_frame_handler(calls).execute(stage, {"src": _src([2, 1])}, _ctx(run_id="r2"))
    assert calls == [2, 2]


def _two_input_stage() -> Stage:
    return Stage.model_validate({
        "id": "merge", "name": "Merge", "type": "python_frame_function",
        "inputs": [{"id": "left"}, {"id": "right"}],
        "function": {"kind": "inline",
                     "code": "def transform(left, right):\n    return left\n"},
    })


def test_the_key_covers_every_input_in_declared_order():
    """A stage with two inputs keys on BOTH: changing the second one alone must
    invalidate."""
    stage = _two_input_stage()
    left = _src([1, 2])
    calls: list[int] = []
    _counting_frame_handler(calls).execute(
        stage, {"left": left, "right": pd.DataFrame({"z": ["a"]})}, _ctx())
    _counting_frame_handler(calls).execute(
        stage, {"left": left, "right": pd.DataFrame({"z": ["b"]})}, _ctx(run_id="r2"))
    assert calls == [2, 2]


# ── the gating conditions ────────────────────────────────────────────────────


def test_cache_false_neither_reads_nor_writes():
    stage = _frame_stage(cache=False)
    calls: list[int] = []
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, _ctx())
    assert _cached_frame(stage, [_src([1])]) is None

    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="r2"))
    assert calls == [1, 1]


def test_bust_cache_skips_the_read_but_still_re_pins():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, _ctx())

    _counting_frame_handler(calls).execute(
        stage, {"src": _src([1])}, _ctx(run_id="r2", bust_cache=True))
    assert calls == [1, 1]  # read skipped: recomputed

    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="r3"))
    assert calls == [1, 1]  # the busted run left the entry re-pinned, not stale


def test_a_run_without_project_scope_touches_the_cache_at_all():
    stage = _frame_stage()
    calls: list[int] = []
    ctx = make_run_context()  # identity=None, stage_cache=None
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, ctx)
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, ctx)
    assert calls == [1, 1]
    assert _cached_frame(stage, [_src([1])]) is None


def test_a_read_only_accessor_reuses_a_hit_but_records_nothing():
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="seed"))

    read_only = _ctx(run_id="reader", cache=ReadOnlyStageCache())
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, read_only)
    assert calls == [1]  # the seeded frame replayed

    _counting_frame_handler(calls).execute(stage, {"src": _src([5])}, read_only)
    assert calls == [1, 1]                       # a miss: computed
    assert _cached_frame(stage, [_src([5])]) is None  # and NOT recorded


def test_a_handler_that_returns_none_records_nothing():
    stage = _frame_stage()
    handler = FrameHandler(apply=lambda stage, inputs, ctx: None)
    assert handler.execute(stage, {"src": _src([1])}, _ctx()) is None
    assert _cached_frame(stage, [_src([1])]) is None


# ── which registrations opt out ──────────────────────────────────────────────


def test_a_terminal_side_effecting_stage_opts_out():
    """`python_frame_function` runs arbitrary user code, so a hit can skip
    unbounded work; `publish` writes artifacts the world reads, so replaying a
    cached frame would leave this run's artifacts absent."""
    assert _frame_handler(StageType.python_frame_function).caches_frames is True
    assert _frame_handler(StageType.publish).caches_frames is False


def _frame_handler(stage_type: StageType) -> FrameHandler:
    handler = HANDLERS[stage_type]
    assert isinstance(handler, FrameHandler)
    return handler


def test_publish_runs_its_side_effect_every_run_and_writes_no_entry(tmp_path):
    code = (
        "import pandas as pd\n"
        "CALLS = []\n"
        "def transform(df, output_dir):\n"
        "    CALLS.append(len(df))\n"
        "    return pd.DataFrame({'path': [output_dir]})\n"
    )
    stage = Stage.model_validate({
        "id": "pub", "name": "Publish", "type": "publish",
        "inputs": [{"id": "src"}],
        "publish": {"destination": "build/"},
        "function": {"kind": "inline", "code": code},
    })
    handler = HANDLERS[StageType.publish]
    for run_id in ("run1", "run2"):
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        out = handler.execute(stage, {"src": _src([1])}, make_run_context(
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

    out = FrameHandler(apply=apply).execute(stage, {"src": _src([1])}, _ctx())
    assert out is not None and len(out) == 2       # the run succeeded
    assert _cached_frame(stage, [_src([1])]) is None  # uncached

    notes = out.attrs[CONTRIBUTION_ATTR].notes
    assert len(notes) == 1 and "uncached" in notes[0]


# ── an unconfigured frame store skips caching, loudly, and never fails ────────


def test_no_frame_store_configured_computes_normally_and_caches_nothing(monkeypatch):
    """A cache MISS must never fail a stage. Every entry point except the web
    app's lifespan reaches a run with no frame store configured."""
    monkeypatch.setattr("app.core.frames._frame_store", None)
    stage = _frame_stage()
    calls: list[int] = []

    out = _counting_frame_handler(calls).execute(stage, {"src": _src([1, 2])}, _ctx())
    assert out is not None and list(out["y"]) == [2, 4]
    assert calls == [2]

    notes = out.attrs[CONTRIBUTION_ATTR].notes
    assert len(notes) == 1
    assert "no frame store" in notes[0] and stage.id in notes[0]


def test_no_frame_store_configured_leaves_nothing_to_replay(monkeypatch):
    monkeypatch.setattr("app.core.frames._frame_store", None)
    stage = _frame_stage()
    calls: list[int] = []
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="r1"))
    _counting_frame_handler(calls).execute(stage, {"src": _src([1])}, _ctx(run_id="r2"))
    assert calls == [1, 1]  # nothing was pinned, so nothing replayed


def test_a_deliberate_opt_out_carries_no_note(monkeypatch):
    """Only an UNAVAILABLE cache is noted. `cache: false` and a run with no
    project scope are choices, not failures — noting them would be noise."""
    calls: list[int] = []
    out = _counting_frame_handler(calls).execute(
        _frame_stage(cache=False), {"src": _src([1])}, _ctx())
    assert out is not None
    assert CONTRIBUTION_ATTR not in out.attrs

    out = _counting_frame_handler(calls).execute(
        _frame_stage(), {"src": _src([1])}, make_run_context())
    assert out is not None
    assert CONTRIBUTION_ATTR not in out.attrs
