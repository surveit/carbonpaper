from __future__ import annotations

import json

import pandas as pd
import pytest

import app.runtime.runner as runner
from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.models.stages.human_review_queue import ReviewVerdict
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import HaltForReview, RunCancelled
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stages import HANDLERS, human_review_queue
from app.services import review, versioning
from app.core.stage_cache import StageCache, compute_row_fingerprint
from app.services.project import save_working_copy_as_version
from conftest import (
    QUEUE_COLUMNS, contribution_of, make_run_context, pinned_stages,
    queue_added_columns, reads_of, resumed_stages,
)

PROJECT = "hrq-cache-tests"


def _run_queue_stage(stage: Stage, inputs: dict[str, pd.DataFrame], ctx) -> pd.DataFrame:
    out = HANDLERS[StageType.human_review_queue].execute(stage, inputs, ctx)
    assert out is not None  # a row-mapped stage always produces a frame
    return out


# The upstream columns `_src()` builds — the default input edge below.
_SCORED_COLUMNS = [{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}]
_FLAGGED_COLUMNS = [*_SCORED_COLUMNS, {"name": "flag", "type": "str", "nullable": True}]

# The columns `QUEUE_COLUMNS` declares this stage adds to every row it emits.
# The stage's output is projected onto its declared columns, so output_schema has
# to name these as well as the upstream ones it carries through.
_REVIEW_COLUMNS = queue_added_columns()


def _stage(
    *,
    reviewer_instructions: str | None = None,
    flt: str | None = None,
    input_columns: list[dict] = _SCORED_COLUMNS,
) -> Stage:
    queue: dict[str, object] = dict(QUEUE_COLUMNS)
    if reviewer_instructions is not None:
        queue["reviewer_instructions"] = reviewer_instructions
    if flt is not None:
        queue["filter"] = flt
    return parse_stage({
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored", "schema": {"columns": input_columns}}],
        "signature": {"form": "extends", "reads": reads_of("scored", input_columns),
                      "adds": _REVIEW_COLUMNS},
        "queue": queue,
    })


def _ctx(tmp_path, run_id="r1"):
    return make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache(),
    )


def _bust_ctx(tmp_path, run_id="r1"):
    return make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id=run_id),
        stage_cache=StageCache(),
        bust_cache=True,
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
    *, project: str = PROJECT, verdict: ReviewVerdict = ReviewVerdict.approve,
    modified_score: float | None = None,
) -> None:
    """Cache one verdict (`approve` unless told otherwise) for one row of a
    halted snapshot, matched to it by the sidecar's fingerprints — built
    through the real review service (record_decision → the production cache
    seam), never a hand-assembled entry or a raw store write. The frozen row is
    the whole snapshot row, which is what the reviewer saw; `modified_score` is
    the value the reviewer entered for QUEUE_COLUMNS' one reviewed column,
    defaulting to the AI value the reviewer was shown."""
    review.record_decision(
        project=project, stage=_stage(),
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row={str(column): value for column, value in row.items()},
        verdict=verdict,
        reviewed_values={
            "human_score": row["score"] if modified_score is None else modified_score
        },
        review_notes=None,
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
    assert len(out.frame) == 2
    assert sorted(out.frame["human_score"].tolist()) == [0, 1]
    assert (out.frame["decision"] == "approve").all()


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
    # snapshot carries no review-record column at all.
    assert "decision" not in snapshot.columns
    assert "human_score" not in snapshot.columns


# ── 5. Fingerprints survive a parquet round trip (the resume path's reload) ─


def test_fingerprints_stable_across_parquet_round_trip(tmp_path):
    src = _src(2)
    original = [compute_row_fingerprint(row.to_dict()) for _, row in src.iterrows()]

    path = tmp_path / "upstream.parquet"
    src.to_parquet(path, index=False)
    reloaded = pd.read_parquet(path)

    roundtripped = [compute_row_fingerprint(row.to_dict()) for _, row in reloaded.iterrows()]
    assert original == roundtripped


def test_input_fingerprint_matches_original_row_before_any_review_record_stamped(tmp_path):
    # The sidecar's `input_fingerprint` for a halted snapshot row must equal
    # `compute_row_fingerprint` of that row's ORIGINAL upstream dict, recomputed
    # independently here from `src` — never a value that shifts once the handler applies a
    # cached decision. Fingerprinting happens on the upstream row before any review-record
    # column is stamped, so a later column can never change the key a cached decision is
    # matched on.
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
    original upstream columns, no fingerprint or review-record column
    ever added."""
    stage = _stage()
    src = _src(2)

    snapshot, _fingerprints = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path))
    assert list(snapshot.columns) == list(src.columns)


# ── 5b. bust_cache: the run re-asks the humans ──────────────────────────────


def test_bust_cache_defers_every_queueable_row_despite_cached_decisions(tmp_path):
    """`RunContext.bust_cache` skips the decision READ entirely: every row a
    prior run decided halts again, so the humans are re-asked. The decisions
    themselves are untouched on disk — a run without the flag still replays
    them."""
    stage = _stage()
    src = _src(2)

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    busted, _fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src.copy()}, _bust_ctx(tmp_path, run_id="run2"))
    assert list(busted["id"]) == ["r0", "r1"]

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run3"))
    assert (out.frame["decision"] == "approve").all()


def test_bust_cache_leaves_passed_through_rows_alone(tmp_path):
    """Only QUEUEABLE rows are re-asked: a row the queue filter does not select
    still passes through, because no cached decision was involved in its
    outcome."""
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    with pytest.raises(HaltForReview) as exc_info:
        _run_queue_stage(stage, {"scored": src.copy()}, _bust_ctx(tmp_path, run_id="run2"))
    assert exc_info.value.contribution.human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 2, "items_decided": 0,
    }


def test_bust_cache_reads_no_cache_entries_at_all(tmp_path, monkeypatch):
    """The read is SKIPPED, not filtered afterwards: the stage makes no
    find_entries call under bust_cache."""
    cache = StageCache()
    calls: list[tuple[str, str, str]] = []

    def recording_find_entries(project: str, stage_id: str, stage_fingerprint: str):
        calls.append((project, stage_id, stage_fingerprint))
        return []

    monkeypatch.setattr(cache, "find_entries", recording_find_entries)
    ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project=PROJECT, run_id="busted"),
        stage_cache=cache,
        bust_cache=True,
    )
    with pytest.raises(HaltForReview):
        _run_queue_stage(_stage(), {"scored": _src(2)}, ctx)
    assert calls == []


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


# ── 8. Row-driven shape: input order, rejections, one cache read, cancel ────


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
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert list(snapshot["id"]) == ["r1", "r3"]  # only the filtered rows queue
    _approve_every_row(snapshot, fingerprints)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out.frame["id"]) == ["r0", "r1", "r2", "r3"]


def test_a_modified_row_stays_in_its_own_position_carrying_the_human_score(tmp_path):
    # A verdict that changes the row's value removes no row: the modified row is emitted
    # in its own input position with the human score the reviewer entered, and the rows
    # around it are untouched.
    stage = _stage()
    src = _src(3)

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    decided = [(ReviewVerdict.approve, None),
               (ReviewVerdict.modify, 77.0),
               (ReviewVerdict.approve, None)]
    for (_, row), fp, (verdict, score) in zip(
        snapshot.iterrows(), fingerprints["input_fingerprints"], decided
    ):
        _put_approval(row, fp, fingerprints["stage_fingerprint"],
                      verdict=verdict, modified_score=score)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out.frame["id"]) == ["r0", "r1", "r2"]
    assert list(out.frame["decision"]) == ["approve", "modify", "approve"]
    modified = out.frame.loc[out.frame["id"] == "r1"].iloc[0]
    assert modified["human_score"] == 77.0
    assert modified["score"] == 1                 # what the AI said is still on the row
    assert modified["reviewer_id"] == "local"     # and who changed it, when
    assert modified["reviewed_at"] == "2026-07-01T00:00:00"


def _every_outcome_src() -> pd.DataFrame:
    """Five rows: three the filter below selects (one per verdict), with a row
    the filter passes through unreviewed on either side of them."""
    return pd.DataFrame({
        "id": ["r0", "r1", "r2", "r3", "r4"],
        "score": [10, 11, 12, 13, 14],
        "flag": ["skip", "review", "review", "review", "skip"],
    })


def test_every_output_row_carries_a_verdict_covering_every_outcome(tmp_path):
    # A real queue output covering every outcome: two rows the filter passed through
    # unreviewed either side of three the reviewer decided. EVERY row carries a verdict
    # value, so a downstream filter is a plain string comparison and never has to reason
    # about a missing one. Filtering on `decision == "approve"` is asserted here as the
    # trap it is: it silently takes the unreviewed rows with it — the queue deliberately
    # let those through, and losing them is data loss.
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _every_outcome_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert list(snapshot["id"]) == ["r1", "r2", "r3"]
    decided = [(ReviewVerdict.approve, None),
               (ReviewVerdict.modify, 99.0),
               (ReviewVerdict.modify, 5.0)]
    for (_, row), fp, (verdict, score) in zip(
        snapshot.iterrows(), fingerprints["input_fingerprints"], decided
    ):
        _put_approval(row, fp, fingerprints["stage_fingerprint"],
                      verdict=verdict, modified_score=score)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out.frame["decision"]) == [
        "skipped", "approve", "modify", "modify", "skipped"]
    assert out.frame["decision"].notna().all()

    approved_only = out.frame[out.frame["decision"] == ReviewVerdict.approve.value]
    assert list(approved_only["id"]) == ["r1"]  # the two unreviewed rows would be lost


def test_every_decided_row_is_emitted_with_only_the_declared_columns(tmp_path):
    # Deciding EVERY queued row still emits every row, projected onto the columns
    # output_schema declares. A queue stage can no longer hand a non-empty input on as a
    # zero-row frame at all, whatever the reviewer decided.
    stage = parse_stage({
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored", "schema": {"columns": _SCORED_COLUMNS}}],
        "signature": {"form": "extends", "reads": reads_of("scored", _SCORED_COLUMNS),
                      "adds": _REVIEW_COLUMNS},
        "queue": dict(QUEUE_COLUMNS),
    })
    src = _src(2)

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    for (_, row), fp in zip(snapshot.iterrows(), fingerprints["input_fingerprints"]):
        _put_approval(row, fp, fingerprints["stage_fingerprint"],
                      verdict=ReviewVerdict.modify, modified_score=3.0)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out.frame.columns) == ["id", "score"] + [
        c["name"] for c in queue_added_columns()]
    assert out.frame["id"].tolist() == ["r0", "r1"]


def test_a_cached_entry_holding_no_output_row_re_queues_the_row(tmp_path):
    """The cache payload still permits an entry with no output row at all. A
    row-mapped stage owes one output row per input row, so such an entry
    replays nothing: the row is a MISS and defers, which re-queues it for the
    human — the only thing anyone could do about it anyway."""
    stage = _stage()
    src = _src(1)
    row = {str(k): v for k, v in src.to_dict("records")[0].items()}
    StageCache().record(
        project=PROJECT, stage_id="review",
        stage_fingerprint=stage.compute_definition_fingerprint(),
        input_fingerprint=compute_row_fingerprint(row),
        input_row=row, output_row=None,
    )

    snapshot, _fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="no-output"))
    assert list(snapshot["id"]) == ["r0"]


def test_queue_stats_count_every_row_the_reviewer_answered(tmp_path):
    # The manifest's per-stage item counts, over a run where every outcome occurs.
    # `items_decided` counts what the reviewer ANSWERED, whatever the verdict — not what a
    # downstream stage would keep.
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
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

    decided = [(ReviewVerdict.approve, None), (ReviewVerdict.modify, 8.0)]
    for (_, row), fp, (verdict, score) in zip(
        snapshot.iterrows(), fingerprints["input_fingerprints"], decided
    ):
        _put_approval(row, fp, fingerprints["stage_fingerprint"],
                      verdict=verdict, modified_score=score)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(out.frame["id"]) == ["r0", "r1", "r2", "r3"]
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


def test_queue_stats_hold_when_every_row_is_served_from_the_cache(tmp_path, monkeypatch):
    """The counts are computed from the assembled frame, not accumulated as rows
    are mapped, so they survive a run where the driver's cache answers EVERY row
    and the mapper is never called once — decided rows replaying a human's
    verdict and passed-through rows replaying their own recorded output."""
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    mapped: list[int] = []
    call = human_review_queue._QueueRowMapper.__call__

    def counting_call(self, row, index):
        mapped.append(index)
        return call(self, row, index)

    monkeypatch.setattr(human_review_queue._QueueRowMapper, "__call__", counting_call)
    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))

    assert mapped == []
    assert contribution_of(out).human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 0, "items_decided": 2,
    }


def test_a_passed_through_row_round_trips_through_the_cache(tmp_path):
    """A row the filter did not select is recorded like any other computed row.
    The second run replays it rather than re-evaluating the filter for it, and
    what comes back is the same output row."""
    stage = _stage(flt="flag == 'nothing-matches'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    first = _run_queue_stage(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    second = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))

    assert list(second.frame["decision"]) == [ReviewVerdict.skipped] * 4
    for column in ("id", "score", "decision", "human_score"):
        assert list(second.frame[column]) == list(first.frame[column])
    # The columns a skipped row leaves empty stay empty through the round trip;
    # the cache payload is JSON, so pandas' NA comes back as None.
    for column in ("reviewer_id", "reviewed_at", "review_notes"):
        assert first.frame[column].isna().all() and second.frame[column].isna().all()
    assert contribution_of(second).human_review_queue_stats == {
        "items_queued_total": 0, "items_passed_through": 4,
        "items_pending": 0, "items_decided": 0,
    }


def test_changing_the_filter_re_evaluates_a_passed_through_row(tmp_path):
    """`filter` is part of the stage's definition fingerprint, so entries
    recorded under one filter are not in the key space the next definition
    reads. A row recorded as passed-through therefore cannot replay "the filter
    did not select me" once the filter DOES select it — it queues for the
    human."""
    src = _alternating_src()

    out = _run_queue_stage(
        _stage(flt="flag == 'nothing-matches'", input_columns=_FLAGGED_COLUMNS), {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert list(out.frame["decision"]) == [ReviewVerdict.skipped] * 4

    snapshot, _fingerprints = _halt_and_read_snapshot(
        _stage(flt="flag == 'skip'", input_columns=_FLAGGED_COLUMNS), {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(snapshot["id"]) == ["r0", "r2"]


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
        _stage(input_columns=[*_SCORED_COLUMNS, {"name": "flag", "type": "bool", "nullable": True}]),
        {"scored": src}, _ctx(tmp_path))

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


def test_cancelled_execution_reports_no_queue_counts(tmp_path, monkeypatch):
    """A resumed run's manifest already carries the halted run's queue counts —
    the executor merged them when the halt fired, and the manifest, not the
    context, is where they live. If the queue stage then cancels, this
    execution produced no counts of its own: the cancel raises inside the row
    map, before the post-map step that would report any. So the stage reports
    nothing and the halt's counts stand, rather than being replaced by the
    zeros a re-execution starts from. A manifest reading
    `items_queued_total: 0` for a stage that queued 2 rows is a wrong number,
    not a missing one."""
    reported: list[object] = []
    monkeypatch.setattr(
        human_review_queue._QueueRowMapper,
        "finish_mapped_rows",
        lambda self, stage, df, ctx, contribution: reported.append(contribution),
    )

    request_cancel(PROJECT, "cancel-resume")
    with pytest.raises(RunCancelled):
        _run_queue_stage(_stage(), {"scored": _src(2)}, _ctx(tmp_path, run_id="cancel-resume"))
    assert reported == []  # nothing was ever produced for the executor to merge


# ── Resume reattaches decisions written via the seam, from disk alone ───────


def _write_stage(root, filename, stage):
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "compiled" / filename).write_text(json.dumps(stage), encoding="utf-8")


def _seed_version(root):
    vid = save_working_copy_as_version(root, message="test seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")


def _load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b"], "score": [1, 2]}).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load", "type": "input_data",
            "signature": {"form": "replaces", "produces": _SCORED_COLUMNS},
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}}


def _review_stage_full():
    return {"id": "review", "description": "Review", "type": "human_review_queue",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "score", "type": "int", "nullable": True}]}}],
            "signature": {"form": "extends",
                          "reads": reads_of("load", [
                              {"name": "id", "type": "str", "nullable": True},
                              {"name": "score", "type": "int", "nullable": True}]),
                          "adds": _REVIEW_COLUMNS},
            "queue": dict(QUEUE_COLUMNS)}


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

    halted = run_prepared(prepare_run(project_dir, project_dir, *pinned_stages(project_dir)))
    assert halted["status"] == "awaiting_review"
    run_id = halted["run_id"]

    run_dir = project_dir / "runs" / run_id
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    assert len(snapshot) == 2
    fingerprints = _read_fingerprints(run_dir / "queue" / "review.parquet")

    _approve_every_row(snapshot, fingerprints, project=project_dir.name)

    resumed = runner.resume_run(project_dir, run_id, project_dir,
                            *resumed_stages(project_dir, run_id))
    assert resumed["status"] == "ok"
    out = pd.read_parquet(run_dir / "outputs" / "review.parquet")
    assert sorted(out["human_score"].tolist()) == [1, 2]


def test_resume_replays_the_runs_bust_cache(tmp_path):
    """`bust_cache` is per-run state recorded on the manifest, so a RESUME of a
    busted run is still busted: decisions recorded between the halt and the
    resume are not read, and the queue stage halts again. The un-busted resume
    of the same shape completes (test above), which is what makes this the
    discriminating outcome."""
    project_dir = tmp_path / "resume_bust_project"
    _write_stage(project_dir, "01_load.json", _load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _review_stage_full())
    _seed_version(project_dir)

    halted = run_prepared(
        prepare_run(project_dir, project_dir, *pinned_stages(project_dir), bust_cache=True))
    assert halted["status"] == "awaiting_review"
    run_id = halted["run_id"]

    run_dir = project_dir / "runs" / run_id
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["parameters"]["bust_cache"]

    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    fingerprints = _read_fingerprints(run_dir / "queue" / "review.parquet")
    _approve_every_row(snapshot, fingerprints, project=project_dir.name)

    resumed = runner.resume_run(project_dir, run_id, project_dir,
                            *resumed_stages(project_dir, run_id))
    assert resumed["status"] == "awaiting_review"
