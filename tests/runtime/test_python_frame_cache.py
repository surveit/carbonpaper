"""Behavior tests for the whole-frame stage-result cache
(app/runtime/stages/python_functions.py::handle_python_frame_function).

A deterministic python_frame_function caches its entire output frame under one
key over its entire input — `(stage-definition fingerprint, whole-input
fingerprint)` — through the frame seam (app.core.frames.FrameStore), never
inline in a document. This is the first stage type where the RUNNER itself
WRITES a cache entry, so these tests pin the write path as well as the read
path: a miss runs the transform and records the frame; an identical later run
hits the cache and does NOT re-run the transform; editing the stage's code (its
definition fingerprint) or its input invalidates the entry.

"Did the transform actually run?" is observed by spying on `_run_frame_function`
— the un-cached execution the handler calls only on a miss.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

import app.runtime.runner as runner
import app.runtime.stages.python_functions as pf
from app.models import Stage
from app.runtime.context import RunIdentity
from app.runtime.runner import execute_run
from app.runtime.stages.python_functions import handle_python_frame_function
from app.services import versioning
from app.services.stage_cache import ReadOnlyStageCache, StageCache
from app.services.versioning import create_version_from_disk
from conftest import make_run_context

PROJECT = "frame-cache-tests"


def _frame_stage(*, factor: int = 2) -> Stage:
    """A python_frame_function whose inline code multiplies `score` by `factor`
    — varying `factor` changes the code, hence the definition fingerprint."""
    code = (
        "def transform(df):\n"
        "    out = df.copy()\n"
        f"    out['scaled'] = out['score'] * {factor}\n"
        "    return out\n"
    )
    return Stage.model_validate({
        "id": "compute", "name": "Compute", "type": "python_frame_function",
        "inputs": [{"id": "src"}],
        "function": {"kind": "inline", "code": code},
    })


def _src(rows: int = 3, base: int = 0) -> pd.DataFrame:
    return pd.DataFrame({
        "id": [f"r{i}" for i in range(rows)],
        "score": [base + i for i in range(rows)],
    })


def _prod_ctx(tmp_path, run_id: str = "r1"):
    """A production-style context: project identity + a read+WRITE StageCache."""
    return make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache(),
    )


@pytest.fixture
def spy(monkeypatch):
    """Count how many times the handler actually runs the transform (a miss),
    delegating to the real implementation so the output is unchanged."""
    calls = {"n": 0}
    original = pf._run_frame_function

    def counting(stage, inputs):
        calls["n"] += 1
        return original(stage, inputs)

    monkeypatch.setattr(pf, "_run_frame_function", counting)
    return calls


# ── 1. Miss runs and records; an identical later run hits and skips ─────────


def test_cache_miss_runs_then_identical_run_hits(tmp_path, spy):
    stage = _frame_stage()
    src = _src()

    out1 = handle_python_frame_function(stage, {"src": src.copy()}, _prod_ctx(tmp_path, "run1"))
    assert spy["n"] == 1  # miss: the transform ran

    out2 = handle_python_frame_function(stage, {"src": src.copy()}, _prod_ctx(tmp_path, "run2"))
    assert spy["n"] == 1  # hit: served from the cache, transform NOT re-run

    # The cached frame is the same output the transform produced.
    assert out2["scaled"].tolist() == [0, 2, 4]
    pd.testing.assert_frame_equal(
        out1.reset_index(drop=True), out2.reset_index(drop=True)
    )


# ── 2. Editing the stage's code invalidates every cache entry ───────────────


def test_definition_change_invalidates_cache(tmp_path, spy):
    src = _src()

    handle_python_frame_function(_frame_stage(factor=2), {"src": src.copy()}, _prod_ctx(tmp_path, "run1"))
    assert spy["n"] == 1

    # Byte-identical input, but the transform's code changed (*2 -> *3): the
    # definition fingerprint changes, so nothing matches — the transform re-runs.
    out = handle_python_frame_function(_frame_stage(factor=3), {"src": src.copy()}, _prod_ctx(tmp_path, "run2"))
    assert spy["n"] == 2
    assert out["scaled"].tolist() == [0, 3, 6]


# ── 3. A changed input frame is a different key — the transform re-runs ─────


def test_input_change_invalidates_cache(tmp_path, spy):
    stage = _frame_stage()

    handle_python_frame_function(stage, {"src": _src(base=0)}, _prod_ctx(tmp_path, "run1"))
    assert spy["n"] == 1

    handle_python_frame_function(stage, {"src": _src(base=100)}, _prod_ctx(tmp_path, "run2"))
    assert spy["n"] == 2  # different frame content -> miss


# ── 4. A run with no project scope carries no cache — it always runs ────────


def test_non_production_run_never_caches(tmp_path, spy):
    stage = _frame_stage()
    ctx = make_run_context(run_dir=tmp_path)  # identity=None, stage_cache=None

    handle_python_frame_function(stage, {"src": _src()}, ctx)
    handle_python_frame_function(stage, {"src": _src()}, ctx)
    assert spy["n"] == 2  # no cache: the transform runs every time


# ── 5. A read-only accessor reuses a hit but never writes a new entry ───────


def test_read_only_cache_reads_but_never_writes(tmp_path, spy):
    stage = _frame_stage()
    written = _src(base=0)

    # A production run records the entry.
    handle_python_frame_function(stage, {"src": written.copy()}, _prod_ctx(tmp_path, "run1"))
    assert spy["n"] == 1

    ro_ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id="run2"),
        stage_cache=ReadOnlyStageCache(),
    )
    # The read-only run reuses the entry the production run wrote.
    handle_python_frame_function(stage, {"src": written.copy()}, ro_ctx)
    assert spy["n"] == 1

    # On a NEW input the read-only run must run the transform AND persist
    # nothing — so a second read-only pass over that same new input misses too.
    fresh = _src(base=500)
    handle_python_frame_function(stage, {"src": fresh.copy()}, ro_ctx)
    assert spy["n"] == 2
    handle_python_frame_function(stage, {"src": fresh.copy()}, ro_ctx)
    assert spy["n"] == 3  # still a miss: read-only wrote nothing to reuse


# ── 6. Multiple inputs fold into one whole-input key ────────────────────────


def _two_input_stage() -> Stage:
    code = (
        "def transform(a, b):\n"
        "    import pandas as pd\n"
        "    return pd.concat([a, b], ignore_index=True)\n"
    )
    return Stage.model_validate({
        "id": "merge", "name": "Merge", "type": "python_frame_function",
        "inputs": [{"id": "left"}, {"id": "right"}],
        "function": {"kind": "inline", "code": code},
    })


def test_multi_input_frame_change_invalidates_only_on_change(tmp_path, spy):
    stage = _two_input_stage()
    left, right = _src(base=0), _src(base=10)

    handle_python_frame_function(stage, {"left": left.copy(), "right": right.copy()}, _prod_ctx(tmp_path, "run1"))
    assert spy["n"] == 1

    # Same two inputs -> hit.
    handle_python_frame_function(stage, {"left": left.copy(), "right": right.copy()}, _prod_ctx(tmp_path, "run2"))
    assert spy["n"] == 1

    # Change the second input only -> the whole-input key changes -> miss.
    handle_python_frame_function(stage, {"left": left.copy(), "right": _src(base=99)}, _prod_ctx(tmp_path, "run3"))
    assert spy["n"] == 2


# ── 7. End-to-end through the production runner: a re-run hits the cache ─────


class _MonotonicClock:
    """A `datetime` stand-in whose `now()` advances one second per call, so two
    `execute_run`s in the same wall-clock second still mint distinct run ids."""

    def __init__(self) -> None:
        self._t = datetime(2026, 1, 1, 0, 0, 0)

    def now(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _write_stage(project_dir, filename, stage):
    (project_dir / "compiled").mkdir(parents=True, exist_ok=True)
    (project_dir / "compiled" / filename).write_text(_json(stage), encoding="utf-8")


def _json(obj) -> str:
    import json
    return json.dumps(obj)


def _seed_version(project_dir):
    vid = create_version_from_disk(project_dir, message="seed", reviewer="test").version_id
    versioning.publish_version(project_dir, vid, reviewer="human")


def test_end_to_end_rerun_serves_frame_from_cache(tmp_path, monkeypatch, spy):
    monkeypatch.setattr(runner, "datetime", _MonotonicClock())

    project_dir = tmp_path / "frame_cache_project"
    csv_path = project_dir / "data" / "items.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"id": ["a", "b"], "score": [1, 2]}).to_csv(csv_path, index=False)

    _write_stage(project_dir, "01_load.json", {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}},
    })
    _write_stage(project_dir, "02_compute.json", {
        "id": "compute", "name": "Compute", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
            "primary_key": ["id"]}}],
        "function": {"kind": "inline", "code": (
            "def transform(df):\n"
            "    out = df.copy()\n"
            "    out['scaled'] = out['score'] * 10\n"
            "    return out\n"
        )},
    })
    _seed_version(project_dir)

    m1 = execute_run(project_dir, repo_root=project_dir)
    assert m1["status"] == "ok"
    assert spy["n"] == 1  # first run: the frame transform executed

    m2 = execute_run(project_dir, repo_root=project_dir)
    assert m2["status"] == "ok"
    assert spy["n"] == 1  # second run: the frame-function output came from cache

    out = pd.read_parquet(project_dir / "runs" / m2["run_id"] / "outputs" / "compute.parquet")
    assert sorted(out["scaled"].tolist()) == [10, 20]
