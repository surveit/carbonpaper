"""Behavior tests for handle_human_review_queue's cache-backed decision
matching (app/runtime/stages/human_review_queue.py): a queued row is matched
to a prior human decision by fingerprinting the row and the stage definition
(app.services.stage_cache), never by re-reading a legacy decisions/*.parquet
file. Every entry these tests seed goes through the seam (`StageCache.put`),
never a raw store write.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

import app.runtime.runner as runner
from app.models import RowReviewDecision, Stage
from app.runtime.context import RunIdentity
from app.runtime.errors import HaltForReview
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages.human_review_queue import handle_human_review_queue
from app.services import versioning
from app.services.stage_cache import (
    HumanDecision,
    StageCache,
    StageCacheEntry,
    build_cache_id,
    compute_row_fingerprint,
)
from app.services.versioning import create_version_from_disk
from conftest import make_run_context

PROJECT = "hrq-cache-tests"


def _stage(*, reviewer_instructions: str | None = None) -> Stage:
    queue: dict[str, str] = {}
    if reviewer_instructions is not None:
        queue["reviewer_instructions"] = reviewer_instructions
    return Stage.model_validate({
        "id": "review", "name": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}],
        "queue": queue,
    })


def _ctx(tmp_path, run_id="r1"):
    return make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache(),
    )


def _src(rows: int = 2) -> pd.DataFrame:
    return pd.DataFrame({"id": [f"r{i}" for i in range(rows)], "score": list(range(rows))})


def _halt_and_read_snapshot(stage: Stage, inputs: dict[str, pd.DataFrame], ctx) -> pd.DataFrame:
    with pytest.raises(HaltForReview) as exc_info:
        handle_human_review_queue(stage, inputs, ctx)
    return pd.read_parquet(exc_info.value.queue_path)


def _put_approval(row: pd.Series, run_id: str, *, project: str = PROJECT) -> None:
    """Cache an `approve` decision for one row of a halted snapshot, through
    the seam (StageCache.put) — never a raw store write."""
    entry = StageCacheEntry(
        id=build_cache_id(project, "review", row["stage_fingerprint"], row["input_fingerprint"]),
        project=project, stage_id="review",
        stage_fingerprint=row["stage_fingerprint"], input_fingerprint=row["input_fingerprint"],
        source_run_id=run_id,
        frozen_input={"id": row["id"], "score": int(row["score"])},
        human=HumanDecision(decision=RowReviewDecision.approve, modified_score=None,
                             reviewer="local", reviewed_at="2026-07-01T00:00:00"),
    )
    StageCache().put(entry)


# ── 1. Decided rows are reused across runs ──────────────────────────────────


def test_decided_rows_reused_across_runs(tmp_path):
    stage = _stage()
    src = _src(2)

    snapshot = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert len(snapshot) == 2
    for _, row in snapshot.iterrows():
        _put_approval(row, run_id="run1")

    out = handle_human_review_queue(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert len(out) == 2
    assert sorted(out["final_score"].tolist()) == [0, 1]
    assert (out["decision"] == RowReviewDecision.approve).all()


# ── 2. Editing the stage definition invalidates every cached decision ──────


def test_definition_change_invalidates_decisions(tmp_path):
    stage = _stage()
    src = _src(2)

    snapshot = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    for _, row in snapshot.iterrows():
        _put_approval(row, run_id="run1")

    # Byte-identical input rows, but `reviewer_instructions` changed — the
    # stage's definition fingerprint changes, so no cached decision matches.
    changed_stage = _stage(reviewer_instructions="look twice")
    new_snapshot = _halt_and_read_snapshot(
        changed_stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2")
    )
    assert len(new_snapshot) == 2  # every row re-halts
    assert set(new_snapshot["id"]) == {"r0", "r1"}


# ── 3. Only the changed row loses its cached decision ───────────────────────


def test_row_change_invalidates_only_that_row(tmp_path):
    stage = _stage()
    src = _src(2)

    snapshot = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    for _, row in snapshot.iterrows():
        _put_approval(row, run_id="run1")

    changed_src = src.copy()
    changed_src.loc[changed_src["id"] == "r1", "score"] = 999  # only r1's value changes

    new_snapshot = _halt_and_read_snapshot(
        stage, {"scored": changed_src}, _ctx(tmp_path, run_id="run2")
    )
    assert list(new_snapshot["id"]) == ["r1"]  # r0 reused its cached decision; r1 pending


# ── 4. A cache miss halts — never a substituted default ─────────────────────


def test_miss_never_falls_back(tmp_path):
    stage = _stage()
    snapshot = _halt_and_read_snapshot(stage, {"scored": _src(1)}, _ctx(tmp_path))
    assert len(snapshot) == 1
    assert snapshot["decision"].isna().all()
    # A pending row has no reviewed output populated from any default.
    assert "final_score" not in snapshot.columns


# ── 5. Fingerprints survive a parquet round trip (the resume path's reload) ─


def test_fingerprints_stable_across_parquet_round_trip(tmp_path):
    src = _src(2)
    original = [compute_row_fingerprint(row.to_dict()) for _, row in src.iterrows()]

    path = tmp_path / "upstream.parquet"
    src.to_parquet(path, index=False)
    reloaded = pd.read_parquet(path)

    roundtripped = [compute_row_fingerprint(row.to_dict()) for _, row in reloaded.iterrows()]
    assert original == roundtripped


def test_input_fingerprint_matches_original_row_before_any_bookkeeping_stamped(tmp_path):
    """`input_fingerprint` on a halted snapshot row must equal
    `compute_row_fingerprint` of that row's ORIGINAL upstream dict, recomputed
    independently here from `src` — never a value that shifts once the
    handler stamps `stage_fingerprint`/`decision`/`modified_score`/`reviewer`/
    `reviewed_at` onto the snapshot row. Fingerprinting happens on the
    upstream row before any bookkeeping column is added, so those later
    columns must never change the key a cached decision is matched on."""
    stage = _stage()
    src = _src(3)
    expected_by_id = {
        row["id"]: compute_row_fingerprint(row.to_dict()) for _, row in src.iterrows()
    }

    snapshot = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path))
    assert len(snapshot) == 3
    for _, row in snapshot.iterrows():
        assert row["input_fingerprint"] == expected_by_id[row["id"]]


# ── 6. A legacy decisions/*.parquet on disk is never read ───────────────────


def test_legacy_decisions_parquet_never_read(tmp_path):
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    pd.DataFrame([{
        "content_hash": "whatever", "decision": "approve", "modified_score": None,
        "reviewer": "local", "reviewed_at": "2026-07-01T00:00:00", "source_run_id": "run0",
    }]).to_parquet(decisions_dir / "review.parquet", index=False)

    stage = _stage()
    snapshot = _halt_and_read_snapshot(stage, {"scored": _src(2)}, _ctx(tmp_path))
    assert len(snapshot) == 2  # every row still pending — the legacy file was never consulted


# ── 7. A subset/preview context (no project scope) fails loudly ─────────────


def test_hrq_requires_project_grant(tmp_path):
    stage = _stage()
    ctx = make_run_context(run_dir=tmp_path)  # identity=None, stage_cache=None
    with pytest.raises(ValueError, match="project-scoped"):
        handle_human_review_queue(stage, {"scored": _src(1)}, ctx)


# ── Resume reattaches decisions written via the seam, from disk alone ───────


def _write_stage(root, filename, stage):
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def _seed_version(root):
    vid = create_version_from_disk(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")


def _load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b"], "score": [1, 2]}).to_csv(csv_path, index=False)
    return {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}}


def _review_stage_full():
    return {"id": "review", "name": "Review", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "score", "type": "int"}],
                "primary_key": ["id"]}}],
            "queue": {}}


def test_resume_reattaches_cached_decisions_written_via_the_seam(tmp_path):
    """A halted run, resumed with no in-process state beyond what prepare_run
    left on disk and in the store (this test does not reuse the ctx/objects
    prepare_run built — resume_run rebuilds everything from the manifest and a
    fresh RunContext, exactly as a resume in a new process would). Decisions
    cached via StageCache.put between the halt and the resume must reattach
    and let the run complete — pinning fingerprint reattachment across the
    upstream-frame reload resume_run performs from parquet."""
    project_dir = tmp_path / "resume_cache_project"
    _write_stage(project_dir, "01_load.json", _load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _review_stage_full())
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir, repo_root=project_dir))
    assert halted["status"] == "awaiting_review"
    run_id = halted["run_id"]

    run_dir = project_dir / "runs" / run_id
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    assert len(snapshot) == 2

    for _, row in snapshot.iterrows():
        _put_approval(row, run_id=run_id, project=project_dir.name)

    resumed = runner.resume_run(project_dir, run_id, project_dir)
    assert resumed["status"] == "ok"
    out = pd.read_parquet(run_dir / "outputs" / "review.parquet")
    assert sorted(out["final_score"].tolist()) == [1, 2]
