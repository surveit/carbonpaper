"""Behavior tests for llm_transform's stage-result cache participation
(app/runtime/stages/llm_transform.py): a row this stage already generated a
reply for is served from app.services.stage_cache instead of re-asking the
model — the LLM counterpart to human_review_queue's cached-decision matching
(tests/runtime/test_hrq_cache.py, from PR #225) — plus the run-mode controls
from #228 that gate it: a per-stage `cache: false` declaration
(Stage.cache) and a per-run "recompute everything" flag (RunContext.bust_cache).

Every entry these tests seed or read goes through the seam (StageCache.put /
.get), never a raw store write, mirroring test_hrq_cache.py's own discipline.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.models import Stage
from app.models.stage import StageType
from app.runtime.context import RunIdentity
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.services.stage_cache import StageCache, compute_row_fingerprint
from conftest import make_run_context

PROJECT = "llm-cache-tests"


def _stage(*, batch_size: int = 1, cache: bool = True, prompt_instructions: str = "") -> Stage:
    return Stage.model_validate({
        "id": "score", "name": "score", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                        {"name": "score", "type": "int", "nullable": False}],
            "primary_key": ["id"]},
        "llm": {"prompt_template": "Rate: {text}", "batch_size": batch_size,
                "prompt_instructions": prompt_instructions, "max_retries": 0},
        "cache": cache,
    })


def _ctx(run_id: str = "r1", bust_cache: bool = False):
    return make_run_context(
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache(),
        bust_cache=bust_cache,
    )


def _src(rows: int = 2) -> pd.DataFrame:
    return pd.DataFrame({"id": [f"r{i}" for i in range(rows)], "text": [f"t{i}" for i in range(rows)]})


def _run(stage: Stage, frames: dict[str, pd.DataFrame], ctx) -> pd.DataFrame:
    out = HANDLERS[StageType.llm_transform].execute(stage, frames, ctx)
    assert out is not None  # llm_transform's RowMapHandler shape always returns a frame
    return out


def _counting_call_llm(monkeypatch):
    """Patch lt.call_llm to a per-row-id-deterministic reply, counting calls."""
    calls: dict[str, int] = {"n": 0}

    def fake(stage_id: str, llm_config: Any, row: dict[str, Any], *, reply_model: Any, **kw: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {"score": 100 + calls["n"]}

    monkeypatch.setattr(lt, "call_llm", fake)
    return calls


def _counting_call_llm_batch(monkeypatch):
    calls: dict[str, int] = {"n": 0}

    def fake(*a: Any, **kw: Any) -> dict[str, Any]:
        calls["n"] += 1
        k = kw["task"].count("### item ")
        return {"results": [{"row_number": i, "score": 200 + calls["n"] * 10 + i} for i in range(k)]}

    monkeypatch.setattr(lt, "call_llm_batch", fake)
    return calls


# ── 1. A row's reply is reused across runs (per-row path) ───────────────────


def test_llm_row_reused_across_runs(monkeypatch):
    stage = _stage()
    src = _src(2)
    calls = _counting_call_llm(monkeypatch)

    out1 = _run(stage, {"load": src.copy()}, _ctx(run_id="run1"))
    assert calls["n"] == 2

    out2 = _run(stage, {"load": src.copy()}, _ctx(run_id="run2"))
    assert calls["n"] == 2  # no new model calls — both rows served from cache
    assert sorted(out1["score"].tolist()) == sorted(out2["score"].tolist())


# ── 2. Editing the stage definition invalidates every cached reply ─────────


def test_definition_change_invalidates_llm_cache(monkeypatch):
    src = _src(2)
    calls = _counting_call_llm(monkeypatch)

    _run(_stage(), {"load": src.copy()}, _ctx(run_id="run1"))
    assert calls["n"] == 2

    changed = _stage(prompt_instructions="be extra careful")
    _run(changed, {"load": src.copy()}, _ctx(run_id="run2"))
    assert calls["n"] == 4  # a different stage_fingerprint misses entirely


# ── 3. Only the changed row loses its cached reply ───────────────────────────


def test_row_change_invalidates_only_that_row(monkeypatch):
    stage = _stage()
    src = _src(2)
    calls = _counting_call_llm(monkeypatch)

    _run(stage, {"load": src.copy()}, _ctx(run_id="run1"))
    assert calls["n"] == 2

    changed_src = src.copy()
    changed_src.loc[changed_src["id"] == "r1", "text"] = "different text"
    _run(stage, {"load": changed_src}, _ctx(run_id="run2"))
    assert calls["n"] == 3  # only r1 (the changed row) re-called the model


# ── 4. A miss writes a cache entry recoverable through the seam ─────────────


def test_cache_miss_writes_an_entry_through_the_seam(monkeypatch):
    stage = _stage()
    _counting_call_llm(monkeypatch)
    src = _src(1)

    _run(stage, {"load": src.copy()}, _ctx(run_id="run1"))

    row_fp = compute_row_fingerprint({"id": "r0", "text": "t0"})
    stage_fp = stage.compute_definition_fingerprint()
    entry = StageCache().get(PROJECT, "score", stage_fp, row_fp)
    assert entry is not None
    assert entry.human is None
    assert entry.llm_output == {"score": 101}


# ── 5. A failed row is never cached ──────────────────────────────────────────


def test_failed_row_is_never_cached(monkeypatch):
    stage = _stage()

    def boom(stage_id, llm_config, row, **kw):
        raise RuntimeError("backend down")

    monkeypatch.setattr(lt, "call_llm", boom)
    ctx = _ctx()
    _run(stage, {"load": _src(1)}, ctx)
    assert ctx.row_errors["score"] == [{"row": 0, "message": "backend down"}]

    stage_fp = stage.compute_definition_fingerprint()
    row_fp = compute_row_fingerprint({"id": "r0", "text": "t0"})
    assert StageCache().get(PROJECT, "score", stage_fp, row_fp) is None


# ── 6. cache: false — always re-rolls, never reads or writes ────────────────


def test_stage_cache_false_never_reads_or_writes(monkeypatch):
    stage = _stage(cache=False)
    src = _src(1)
    calls = _counting_call_llm(monkeypatch)

    _run(stage, {"load": src.copy()}, _ctx(run_id="run1"))
    _run(stage, {"load": src.copy()}, _ctx(run_id="run2"))
    assert calls["n"] == 2  # every run re-calls the model — nothing was ever cached

    stage_fp = stage.compute_definition_fingerprint()
    row_fp = compute_row_fingerprint({"id": "r0", "text": "t0"})
    assert StageCache().get(PROJECT, "score", stage_fp, row_fp) is None


# ── 7. bust_cache skips the read but still writes a fresh entry ─────────────


def test_bust_cache_skips_read_but_still_writes(monkeypatch):
    stage = _stage()
    src = _src(1)
    calls = _counting_call_llm(monkeypatch)

    _run(stage, {"load": src.copy()}, _ctx(run_id="run1"))
    assert calls["n"] == 1

    # A normal run would hit the cache and make no new call; a bust_cache run
    # re-calls the model even though a decision already exists.
    _run(stage, {"load": src.copy()}, _ctx(run_id="run2", bust_cache=True))
    assert calls["n"] == 2

    # The fresh reply re-pinned the entry: a later ordinary run reuses IT, not
    # the original, and makes no further call.
    out3 = _run(stage, {"load": src.copy()}, _ctx(run_id="run3"))
    assert calls["n"] == 2
    assert out3.loc[0, "score"] == 102  # the second (bust) call's reply, not the first


# ── 8. No project scope (subset/preview) never touches the cache ────────────


def test_no_project_scope_never_touches_cache(monkeypatch):
    stage = _stage()
    calls = _counting_call_llm(monkeypatch)
    ctx = make_run_context()  # identity=None, stage_cache=None

    _run(stage, {"load": _src(1)}, ctx)
    _run(stage, {"load": _src(1)}, ctx)
    assert calls["n"] == 2  # both calls hit the model — nothing to cache against


# ── batched path (batch_size > 1): same contract, chunked ───────────────────


def test_llm_batch_reused_across_runs(monkeypatch):
    stage = _stage(batch_size=2)
    src = _src(2)
    calls = _counting_call_llm_batch(monkeypatch)

    out1 = _run(stage, {"load": src.copy()}, _ctx(run_id="run1"))
    assert calls["n"] == 1  # one batched call for both rows

    out2 = _run(stage, {"load": src.copy()}, _ctx(run_id="run2"))
    assert calls["n"] == 1  # fully served from cache — no new batched call
    assert sorted(out1["score"].tolist()) == sorted(out2["score"].tolist())


def test_llm_batch_only_calls_model_for_the_rows_that_miss(monkeypatch):
    stage = _stage(batch_size=2)
    src = _src(2)
    calls = _counting_call_llm_batch(monkeypatch)

    _run(stage, {"load": src.copy()}, _ctx(run_id="run1"))
    assert calls["n"] == 1

    changed_src = src.copy()
    changed_src.loc[changed_src["id"] == "r1", "text"] = "different text"
    out2 = _run(stage, {"load": changed_src}, _ctx(run_id="run2"))
    assert calls["n"] == 2  # a second, smaller batched call — just the one miss
    assert len(out2) == 2  # grain preserved: cache hit + fresh row both present


def test_llm_batch_failed_row_is_never_cached(monkeypatch):
    stage = _stage(batch_size=2, prompt_instructions="unused")

    def fake(*a, **kw):
        # Deliberately return only one result for a 2-item chunk — an anomaly
        # that fails the whole chunk (see _process_chunk / _validate_batch_reply).
        return {"results": [{"row_number": 0, "score": 1}]}

    monkeypatch.setattr(lt, "call_llm_batch", fake)
    ctx = _ctx()
    _run(stage, {"load": _src(2)}, ctx)
    assert ctx.row_errors["score"]  # both rows failed

    stage_fp = stage.compute_definition_fingerprint()
    for rid, text in (("r0", "t0"), ("r1", "t1")):
        row_fp = compute_row_fingerprint({"id": rid, "text": text})
        assert StageCache().get(PROJECT, "score", stage_fp, row_fp) is None
