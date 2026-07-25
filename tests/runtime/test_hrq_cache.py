"""Behavior tests for the human_review_queue handler's cache-backed decision
matching (app/runtime/stages/human_review_queue.py): a queued row is matched
to a prior human decision by fingerprinting the row and the stage definition
(app.core.stage_cache), never by re-reading a legacy decisions/*.parquet
file. Every entry these tests seed goes through the seam (`StageCache.put`),
never a raw store write.

The stage is always exercised through its registered handler
(`HANDLERS[StageType.human_review_queue].execute`), so what these tests pin is
the whole row-driven path — the per-row mapper, the driver's assembly and row
dropping, and the handler's own post-map collection — not a function called
underneath it.

Fingerprints never live on the snapshot itself: they're read from the sidecar
`<stage>.fingerprints.json` written alongside it, POSITIONALLY aligned to the
snapshot's row order.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

import app.runtime.runner as runner
from app.models import RowReviewDecision, Stage
from app.models.stage import StageType
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import HaltForReview, RunCancelled
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import HANDLERS
from app.services import review, versioning
from app.core.stage_cache import StageCache, compute_row_fingerprint
from app.services.versioning import create_version_from_disk
from conftest import contribution_of, make_run_context

PROJECT = "hrq-cache-tests"


def _run_queue_stage(stage: Stage, inputs: dict[str, pd.DataFrame], ctx) -> pd.DataFrame:
    out = HANDLERS[StageType.human_review_queue].execute(stage, inputs, ctx)
    assert out is not None  # a row-mapped stage always produces a frame
    return out


def _stage(*, reviewer_instructions: str | None = None, flt: str | None = None) -> Stage:
    queue: dict[str, str] = {}
    if reviewer_instructions is not None:
        queue["reviewer_instructions"] = reviewer_instructions
    if flt is not None:
        queue["filter"] = flt
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


def _read_fingerprints(queue_path) -> dict:
    """The sidecar `<stage>.fingerprints.json` beside a halted queue's own
    snapshot file — same stem, whichever extension the snapshot landed on."""
    sidecar = queue_path.parent / f"{queue_path.stem}.fingerprints.json"
    parsed: dict = json.loads(sidecar.read_text(encoding="utf-8"))
    return parsed


def _halt_and_read_snapshot(
    stage: Stage, inputs: dict[str, pd.DataFrame], ctx
) -> tuple[pd.DataFrame, dict]:
    with pytest.raises(HaltForReview) as exc_info:
        _run_queue_stage(stage, inputs, ctx)
    queue_path = exc_info.value.queue_path
    return pd.read_parquet(queue_path), _read_fingerprints(queue_path)


def _put_approval(
    row: pd.Series, input_fingerprint: str, stage_fingerprint: str,
    *, project: str = PROJECT, verdict: RowReviewDecision = RowReviewDecision.approve,
) -> None:
    """Cache one verdict (`approve` unless told otherwise) for one row of a
    halted snapshot, matched to it by the sidecar's fingerprints — built
    through the real review service (record_decision → the production cache
    seam), never a hand-assembled entry or a raw store write. The frozen row is
    the whole snapshot row, which is what the reviewer saw."""
    review.record_decision(
        project=project, stage_id="review",
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row={str(column): value for column, value in row.items()},
        verdict=verdict, modified_score=None,
        reviewer="local", reviewed_at="2026-07-01T00:00:00",
    )


def _approve_every_row(snapshot: pd.DataFrame, fingerprints: dict, *, project: str = PROJECT) -> None:
    for (_, row), fp in zip(snapshot.iterrows(), fingerprints["input_fingerprints"]):
        _put_approval(row, fp, fingerprints["stage_fingerprint"], project=project)


# ── 1. Decided rows are reused across runs ──────────────────────────────────


def test_decided_rows_reused_across_runs(tmp_path):
    stage = _stage()
    src = _src(2)

    snapshot, fingerprints = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert len(snapshot) == 2
    _approve_every_row(snapshot, fingerprints)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert len(out) == 2
    assert sorted(out["final_score"].tolist()) == [0, 1]
    assert (out["decision"] == "approve").all()


# ── 2. Editing the stage definition invalidates every cached decision ──────


def test_definition_change_invalidates_decisions(tmp_path):
    stage = _stage()
    src = _src(2)

    snapshot, fingerprints = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    # Byte-identical input rows, but `reviewer_instructions` changed — the
    # stage's definition fingerprint changes, so no cached decision matches.
    changed_stage = _stage(reviewer_instructions="look twice")
    new_snapshot, _new_fingerprints = _halt_and_read_snapshot(
        changed_stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2")
    )
    assert len(new_snapshot) == 2  # every row re-halts
    assert set(new_snapshot["id"]) == {"r0", "r1"}


# ── 3. Only the changed row loses its cached decision ───────────────────────


def test_row_change_invalidates_only_that_row(tmp_path):
    stage = _stage()
    src = _src(2)

    snapshot, fingerprints = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    changed_src = src.copy()
    changed_src.loc[changed_src["id"] == "r1", "score"] = 999  # only r1's value changes

    new_snapshot, _new_fingerprints = _halt_and_read_snapshot(
        stage, {"scored": changed_src}, _ctx(tmp_path, run_id="run2")
    )
    assert list(new_snapshot["id"]) == ["r1"]  # r0 reused its cached decision; r1 pending


# ── 4. A cache miss halts — never a substituted default ─────────────────────


def test_miss_never_falls_back(tmp_path):
    stage = _stage()
    snapshot, fingerprints = _halt_and_read_snapshot(stage, {"scored": _src(1)}, _ctx(tmp_path))
    assert len(snapshot) == 1
    assert len(fingerprints["input_fingerprints"]) == 1
    # A pending row has no reviewed output populated from any default, and the
    # snapshot carries no bookkeeping column at all.
    assert "decision" not in snapshot.columns
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
    """The sidecar's `input_fingerprint` for a halted snapshot row must equal
    `compute_row_fingerprint` of that row's ORIGINAL upstream dict, recomputed
    independently here from `src` — never a value that shifts once the
    handler applies a cached decision. Fingerprinting happens on the upstream
    row before any bookkeeping is added, so a later column can never change
    the key a cached decision is matched on."""
    stage = _stage()
    src = _src(3)
    expected_by_id = {
        row["id"]: compute_row_fingerprint(row.to_dict()) for _, row in src.iterrows()
    }

    snapshot, fingerprints = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path))
    assert len(snapshot) == 3
    for (_, row), fp in zip(snapshot.iterrows(), fingerprints["input_fingerprints"]):
        assert fp == expected_by_id[row["id"]]


def test_snapshot_columns_match_original_upstream_columns_exactly(tmp_path):
    """The pending snapshot is written PURE: exactly the pending rows'
    original upstream columns, no fingerprint or decision-bookkeeping column
    ever added."""
    stage = _stage()
    src = _src(2)

    snapshot, _fingerprints = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path))
    assert list(snapshot.columns) == list(src.columns)


# ── 6. A legacy decisions/*.parquet on disk is never read ───────────────────


def test_legacy_decisions_parquet_never_read(tmp_path):
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    pd.DataFrame([{
        "content_hash": "whatever", "decision": "approve", "modified_score": None,
        "reviewer": "local", "reviewed_at": "2026-07-01T00:00:00", "source_run_id": "run0",
    }]).to_parquet(decisions_dir / "review.parquet", index=False)

    stage = _stage()
    snapshot, _fingerprints = _halt_and_read_snapshot(stage, {"scored": _src(2)}, _ctx(tmp_path))
    assert len(snapshot) == 2  # every row still pending — the legacy file was never consulted


# ── 7. A subset/preview context (no project scope) fails loudly ─────────────


def test_hrq_requires_project_grant(tmp_path):
    stage = _stage()
    ctx = make_run_context(run_dir=tmp_path)  # identity=None, stage_cache=None
    with pytest.raises(ValueError, match="project-scoped"):
        _run_queue_stage(stage, {"scored": _src(1)}, ctx)


# ── 8. Row-driven shape: input order, dropping, one cache read, cancel ──────


def _alternating_src() -> pd.DataFrame:
    """Rows whose `flag` alternates, so the filter below selects a
    NON-CONTIGUOUS subset — the shape that tells input order apart from
    decided-first order."""
    return pd.DataFrame({
        "id": ["r0", "r1", "r2", "r3"],
        "score": [10, 11, 12, 13],
        "flag": ["skip", "review", "skip", "review"],
    })


def test_output_rows_stay_in_input_order(tmp_path):
    """A frame whose reviewed and passed-through rows alternate comes back in
    INPUT order — r0, r1, r2, r3 — not with the two decided rows hoisted to the
    front."""
    stage = _stage(flt="flag == 'review'")
    src = _alternating_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert list(snapshot["id"]) == ["r1", "r3"]  # only the filtered rows queue
    _approve_every_row(snapshot, fingerprints)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out["id"]) == ["r0", "r1", "r2", "r3"]


def test_rejected_row_is_dropped_and_the_rest_keep_input_order(tmp_path):
    """A cached tombstone (a `reject` verdict) removes exactly its own row; the
    rows around it keep their input order."""
    stage = _stage()
    src = _src(3)

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    verdicts = [RowReviewDecision.approve, RowReviewDecision.reject, RowReviewDecision.approve]
    for (_, row), fp, verdict in zip(
        snapshot.iterrows(), fingerprints["input_fingerprints"], verdicts
    ):
        _put_approval(row, fp, fingerprints["stage_fingerprint"], verdict=verdict)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out["id"]) == ["r0", "r2"]


def test_queue_stats_count_every_row_including_the_rejected_one(tmp_path):
    """The manifest's per-stage item counts, over a run where every outcome
    occurs. `items_decided` counts the REJECTED row too — it was decided, and
    it is the one row that no longer exists by the time the stage's frame is
    assembled, so it can only be counted as the row is mapped."""
    stage = _stage(flt="flag == 'review'")
    src = _alternating_src()

    # On the halting path the stage's contribution rides out on the halt itself
    # — the raise is that path's only return into the manifest.
    with pytest.raises(HaltForReview) as exc_info:
        _run_queue_stage(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert exc_info.value.contribution.human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 2, "items_decided": 0,
    }
    queue_path = exc_info.value.queue_path
    snapshot, fingerprints = pd.read_parquet(queue_path), _read_fingerprints(queue_path)

    verdicts = [RowReviewDecision.approve, RowReviewDecision.reject]
    for (_, row), fp, verdict in zip(
        snapshot.iterrows(), fingerprints["input_fingerprints"], verdicts
    ):
        _put_approval(row, fp, fingerprints["stage_fingerprint"], verdict=verdict)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out["id"]) == ["r0", "r1", "r2"]  # r3 was rejected
    assert contribution_of(out).human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 0, "items_decided": 2,
    }


def test_cache_is_read_once_per_stage_execution(tmp_path, monkeypatch):
    """The cached decisions are looked up ONCE for the whole stage, not once
    per row: the lookup belongs to building the mapper, and a per-row store
    read would make a queue stage's cost scale with its row count."""
    cache = StageCache()
    calls: list[tuple[str, str, str]] = []
    find_entries = cache.find_entries

    def counting_find_entries(project: str, stage_id: str, stage_fingerprint: str):
        calls.append((project, stage_id, stage_fingerprint))
        return find_entries(project, stage_id, stage_fingerprint)

    monkeypatch.setattr(cache, "find_entries", counting_find_entries)
    ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id="count"),
        stage_cache=cache,
    )
    with pytest.raises(HaltForReview):
        _run_queue_stage(_stage(), {"scored": _src(3)}, ctx)
    assert len(calls) == 1


def test_fingerprint_matches_the_drivers_own_row_dict(tmp_path):
    """The sidecar's fingerprints are computed over the row dicts the row
    driver builds (`src.to_dict("records")`, str-keyed), position by position —
    so the key a decision is filed under is the key the next run's driver
    recomputes."""
    src = _src(3)
    _snapshot, fingerprints = _halt_and_read_snapshot(
        _stage(), {"scored": src}, _ctx(tmp_path))

    expected = [
        compute_row_fingerprint({str(k): v for k, v in record.items()})
        for record in src.to_dict("records")
    ]
    assert fingerprints["input_fingerprints"] == expected


def test_nullable_extension_dtype_cells_reach_the_reviewer_as_plain_numpy_values(tmp_path):
    """The snapshot is REBUILT from the driver's row dicts rather than sliced
    off the upstream frame, so pandas' nullable extension dtypes do not survive
    it — and neither do the cell values that only those dtypes can hold. An
    `Int64` column carrying a null comes back `float64`, so the reviewer sees
    `1.0` where upstream held the integer `1`; a `boolean` column comes back
    `object`, which changes the dtype but not the values.

    This matters beyond display: what the reviewer decides on is frozen as the
    cache entry's input, and the output row replayed on the next run is built
    from that frozen row — so the widened value is what flows downstream."""
    src = pd.DataFrame({
        "id": ["r0", "r1"],
        "score": pd.array([1, None], dtype="Int64"),
        "flag": pd.array([True, None], dtype="boolean"),
    })
    snapshot, _fingerprints = _halt_and_read_snapshot(
        _stage(), {"scored": src}, _ctx(tmp_path))

    assert list(snapshot.columns) == ["id", "score", "flag"]
    assert snapshot["score"].dtype == "float64"      # Int64 did not survive
    assert snapshot.loc[0, "score"] == 1.0           # the integer 1 upstream
    assert pd.isna(snapshot.loc[1, "score"])         # the null is still a null
    assert snapshot["flag"].dtype == object          # boolean did not survive
    assert snapshot.loc[0, "flag"] is True           # its values did
    assert snapshot.loc[1, "flag"] is None


def test_cancel_mid_queue_map_marks_the_stage_cancelled(tmp_path):
    """A cancel requested before the stage runs stops the row map with
    RunCancelled — the run was cancelled, so it must not also be reported as
    awaiting review."""
    ctx = _ctx(tmp_path, run_id="cancel-me")
    request_cancel(PROJECT, "cancel-me")
    with pytest.raises(RunCancelled):
        _run_queue_stage(_stage(), {"scored": _src(2)}, ctx)


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
    fingerprints = _read_fingerprints(run_dir / "queue" / "review.parquet")

    _approve_every_row(snapshot, fingerprints, project=project_dir.name)

    resumed = runner.resume_run(project_dir, run_id, project_dir)
    assert resumed["status"] == "ok"
    out = pd.read_parquet(run_dir / "outputs" / "review.parquet")
    assert sorted(out["final_score"].tolist()) == [1, 2]
