from __future__ import annotations


import pandas as pd
import pytest

import app.runtime.runner as runner
from app.models import parse_stage, Stage
from app.models.stage import StageType
from app.models.stages.human_review_queue import ReviewVerdict
from app.models.records.queue_fingerprints import QueueFingerprints
from app.runtime.cancellation import request_cancel
from app.runtime.context import RunIdentity
from app.runtime.errors import RunCancelled
from app.runtime.runner import prepare_run, run_prepared
from app.runtime.stage_output import StageOutput
from app.runtime.stages import HANDLERS, human_review_queue
from app.services import review
from app.core.frames import list_table_rows, read_frame_table
from app.core.stage_cache import StageCache, compute_row_fingerprint
from app.services.project import save_working_copy_as_version
from conftest import (
    QUEUE_COLUMNS, as_inputs, contribution_of, make_run_context, pinned_stages,
    place_stage, queue_added_columns, reads_of, require_awaiting_review, resumed_stages,
    rows_of,
)

from stage_seed import add_stage
from run_seed import read_manifest

PROJECT = "hrq-cache-tests"


def _run_queue_stage(stage: Stage, inputs: dict[str, pd.DataFrame], ctx) -> StageOutput:
    out = HANDLERS[StageType.human_review_queue].execute(place_stage(stage), as_inputs(inputs), ctx)
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
        "inputs": [{"id": "scored"}],
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


def _read_fingerprints(project: str, run_id: str, stage_id: str = "review") -> dict:
    return QueueFingerprints.load(
        QueueFingerprints.compose_id(project, run_id, stage_id)).model_dump()


def _halt_and_read_snapshot(
    stage: Stage, inputs: dict[str, pd.DataFrame], ctx
) -> tuple[pd.DataFrame, dict]:
    queue_path = require_awaiting_review(_run_queue_stage(stage, inputs, ctx)).queue_path
    return pd.read_parquet(queue_path), _read_fingerprints(PROJECT, ctx.identity.run_id, queue_path.stem)


def _put_approval(
    row: pd.Series, input_fingerprint: str, stage_fingerprint: str,
    *, project: str = PROJECT, verdict: ReviewVerdict = ReviewVerdict.approve,
    modified_score: float | None = None,
) -> None:
    """Goes through the real review service, never a hand-assembled entry or a raw store write."""
    review.record_decision(
        project_id=project, stage=place_stage(_stage()),
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row={str(column): value for column, value in row.items()},
        verdict=verdict,
        reviewed_values={
            "human_score": row["score"] if modified_score is None else modified_score
        },
        review_notes=None,
        reviewer="local", reviewed_at="2026-07-01T00:00:00",
        workflow_version=None,
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
    assert len(rows_of(out)) == 2
    assert sorted(rows_of(out)["human_score"].tolist()) == [0, 1]
    assert (rows_of(out)["decision"] == "approve").all()


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


_ARRAY_COLUMNS = [
    *_SCORED_COLUMNS,
    {"name": "tags", "type": "list[str]", "nullable": True},
]


def _src_with_array(rows: int = 2) -> pd.DataFrame:
    return pd.DataFrame({
        "id": [f"r{i}" for i in range(rows)],
        "score": list(range(rows)),
        "tags": [[f"t{i}a", f"t{i}b"] for i in range(rows)],
    })


def test_an_array_column_survives_the_queue_snapshot_round_trip(tmp_path):
    stage = _stage(input_columns=_ARRAY_COLUMNS)
    src = _src_with_array(2)
    expected = [compute_row_fingerprint(row.to_dict()) for _, row in src.iterrows()]

    ctx = _ctx(tmp_path, run_id="arr1")
    queue_path = require_awaiting_review(_run_queue_stage(stage, {"scored": src}, ctx)).queue_path
    rows = list_table_rows(read_frame_table(queue_path))

    assert [compute_row_fingerprint(row) for row in rows] == expected


def test_input_fingerprint_matches_original_row_before_any_review_record_stamped(tmp_path):
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
    stage = _stage()
    src = _src(2)

    snapshot, _fingerprints = _halt_and_read_snapshot(stage, {"scored": src}, _ctx(tmp_path))
    assert list(snapshot.columns) == list(src.columns)


# ── 5b. bust_cache: the run re-asks the humans ──────────────────────────────


def test_bust_cache_defers_every_queueable_row_despite_cached_decisions(tmp_path):
    stage = _stage()
    src = _src(2)

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    busted, _fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src.copy()}, _bust_ctx(tmp_path, run_id="run2"))
    assert list(busted["id"]) == ["r0", "r1"]

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run3"))
    assert (rows_of(out)["decision"] == "approve").all()


def test_bust_cache_leaves_passed_through_rows_alone(tmp_path):
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    output = _run_queue_stage(stage, {"scored": src.copy()}, _bust_ctx(tmp_path, run_id="run2"))
    assert require_awaiting_review(output) is not None
    assert output.contribution.human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 2, "items_decided": 0,
    }


def test_bust_cache_reads_no_cache_entries_at_all(tmp_path, monkeypatch):
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
    require_awaiting_review(_run_queue_stage(_stage(), {"scored": _src(2)}, ctx))
    assert calls == []


# ── 6. A legacy decisions/*.parquet on disk is never read ───────────────────


def test_legacy_decisions_parquet_never_read(tmp_path):
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{
        "content_hash": "whatever", "decision": "approve", "modified_score": None,
        "reviewed_at": "2026-07-01T00:00:00", "source_run_id": "run0",
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
    """The alternating flag makes the queue subset NON-CONTIGUOUS: input order and decided-first differ."""
    return pd.DataFrame({
        "id": ["r0", "r1", "r2", "r3"],
        "score": [10, 11, 12, 13],
        "flag": ["skip", "review", "skip", "review"],
    })


def test_output_rows_stay_in_input_order(tmp_path):
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert list(snapshot["id"]) == ["r1", "r3"]  # only the filtered rows queue
    _approve_every_row(snapshot, fingerprints)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(rows_of(out)["id"]) == ["r0", "r1", "r2", "r3"]


def test_a_modified_row_stays_in_its_own_position_carrying_the_human_score(tmp_path):
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
    assert list(rows_of(out)["id"]) == ["r0", "r1", "r2"]
    assert list(rows_of(out)["decision"]) == ["approve", "modify", "approve"]
    modified = rows_of(out).loc[rows_of(out)["id"] == "r1"].iloc[0]
    assert modified["human_score"] == 77.0
    assert modified["score"] == 1                 # what the AI said is still on the row
    assert modified["reviewer_id"] == "local"     # and who changed it, when
    assert modified["reviewed_at"] == "2026-07-01T00:00:00"


def _every_outcome_src() -> pd.DataFrame:
    return pd.DataFrame({
        "id": ["r0", "r1", "r2", "r3", "r4"],
        "score": [10, 11, 12, 13, 14],
        "flag": ["skip", "review", "review", "review", "skip"],
    })


def test_every_output_row_carries_a_verdict_covering_every_outcome(tmp_path):
    # `decision == "approve"` silently drops the rows the queue passed through unreviewed.
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
    assert list(rows_of(out)["decision"]) == [
        "skipped", "approve", "modify", "modify", "skipped"]
    assert rows_of(out)["decision"].notna().all()

    approved_only = rows_of(out)[rows_of(out)["decision"] == ReviewVerdict.approve.value]
    assert list(approved_only["id"]) == ["r1"]  # the two unreviewed rows would be lost


def test_every_decided_row_is_emitted_with_only_the_declared_columns(tmp_path):
    stage = parse_stage({
        "id": "review", "description": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}],
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
    assert list(rows_of(out).columns) == ["id", "score"] + [
        c["name"] for c in queue_added_columns()]
    assert rows_of(out)["id"].tolist() == ["r0", "r1"]


def test_a_cached_entry_holding_no_output_row_re_queues_the_row(tmp_path):
    stage = _stage()
    src = _src(1)
    row = {str(k): v for k, v in src.to_dict("records")[0].items()}
    StageCache().record(
        project_id=PROJECT, stage_id="review",
        stage_fingerprint=stage.compute_definition_fingerprint(),
        input_fingerprint=compute_row_fingerprint(row),
        input_row=row, output_row=None, branches=None,
    )

    snapshot, _fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="no-output"))
    assert list(snapshot["id"]) == ["r0"]


def test_queue_stats_count_every_row_the_reviewer_answered(tmp_path):
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    # On the halting path the stage's contribution rides out on the halt itself
    # — the raise is that path's only return into the manifest.
    output = _run_queue_stage(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert output.contribution.human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 2, "items_decided": 0,
    }
    queue_path = require_awaiting_review(output).queue_path
    snapshot, fingerprints = pd.read_parquet(queue_path), _read_fingerprints(PROJECT, "run1", queue_path.stem)

    decided = [(ReviewVerdict.approve, None), (ReviewVerdict.modify, 8.0)]
    for (_, row), fp, (verdict, score) in zip(
        snapshot.iterrows(), fingerprints["input_fingerprints"], decided
    ):
        _put_approval(row, fp, fingerprints["stage_fingerprint"],
                      verdict=verdict, modified_score=score)

    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(rows_of(out)["id"]) == ["r0", "r1", "r2", "r3"]
    assert contribution_of(out).human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 0, "items_decided": 2,
    }


def test_cache_is_read_once_per_stage_execution(tmp_path, monkeypatch):
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
    require_awaiting_review(_run_queue_stage(_stage(), {"scored": _src(3)}, ctx))
    assert len(calls) == 1


def test_no_row_re_defers_when_every_row_is_already_decided(tmp_path, monkeypatch):
    """Resolution now lives INSIDE the mapper, so every row reaches __call__ — none may re-defer."""
    stage = _stage(flt="flag == 'review'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    snapshot, fingerprints = _halt_and_read_snapshot(
        stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    _approve_every_row(snapshot, fingerprints)

    deferred: list[int] = []
    defer_row = human_review_queue._defer_row

    def counting_defer(row, index):
        deferred.append(index)
        return defer_row(row, index)

    monkeypatch.setattr(human_review_queue, "_defer_row", counting_defer)
    out = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))

    assert deferred == []
    assert contribution_of(out).human_review_queue_stats == {
        "items_queued_total": 2, "items_passed_through": 2,
        "items_pending": 0, "items_decided": 2,
    }


def test_a_passed_through_row_round_trips_through_the_cache(tmp_path):
    stage = _stage(flt="flag == 'nothing-matches'", input_columns=_FLAGGED_COLUMNS)
    src = _alternating_src()

    first = _run_queue_stage(stage, {"scored": src}, _ctx(tmp_path, run_id="run1"))
    second = _run_queue_stage(stage, {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))

    assert list(rows_of(second)["decision"]) == [ReviewVerdict.skipped] * 4
    for column in ("id", "score", "decision", "human_score"):
        assert list(rows_of(second)[column]) == list(rows_of(first)[column])
    # The columns a skipped row leaves empty stay empty through the round trip;
    # the cache payload is JSON, so pandas' NA comes back as None.
    for column in ("reviewer_id", "reviewed_at", "review_notes"):
        assert rows_of(first)[column].isna().all() and rows_of(second)[column].isna().all()
    assert contribution_of(second).human_review_queue_stats == {
        "items_queued_total": 0, "items_passed_through": 4,
        "items_pending": 0, "items_decided": 0,
    }


def test_changing_the_filter_re_evaluates_a_passed_through_row(tmp_path):
    """`filter` feeds the definition fingerprint, so entries recorded under one are in another key space."""
    src = _alternating_src()

    out = _run_queue_stage(
        _stage(flt="flag == 'nothing-matches'", input_columns=_FLAGGED_COLUMNS), {"scored": src}, _ctx(tmp_path, run_id="run1"))
    assert list(rows_of(out)["decision"]) == [ReviewVerdict.skipped] * 4

    snapshot, _fingerprints = _halt_and_read_snapshot(
        _stage(flt="flag == 'skip'", input_columns=_FLAGGED_COLUMNS), {"scored": src.copy()}, _ctx(tmp_path, run_id="run2"))
    assert list(snapshot["id"]) == ["r0", "r2"]


def test_fingerprint_matches_the_drivers_own_row_dict(tmp_path):
    src = _src(3)
    _snapshot, fingerprints = _halt_and_read_snapshot(
        _stage(), {"scored": src}, _ctx(tmp_path))

    expected = [
        compute_row_fingerprint({str(k): v for k, v in record.items()})
        for record in src.to_dict("records")
    ]
    assert fingerprints["input_fingerprints"] == expected


def test_nullable_extension_dtype_cells_reach_the_reviewer_as_plain_numpy_values(tmp_path):
    """The widened value is frozen as the cache entry's input, so it is what flows downstream."""
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
    ctx = _ctx(tmp_path, run_id="cancel-me")
    request_cancel(PROJECT, "cancel-me")
    with pytest.raises(RunCancelled):
        _run_queue_stage(_stage(), {"scored": _src(2)}, ctx)


def test_cancelled_execution_reports_no_queue_counts(tmp_path, monkeypatch):
    """A manifest reading 0 queued for a stage that queued 2 is a wrong number, not a missing one."""
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
    root.mkdir(parents=True, exist_ok=True)
    add_stage(root, stage)


def _seed_version(root):
    save_working_copy_as_version(root.name, message="test seed")


def _load_stage(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    csv_path = root / "data" / "items.csv"
    pd.DataFrame({"id": ["a", "b"], "score": [1, 2]}).to_csv(csv_path, index=False)
    return {"id": "load", "description": "Load", "type": "input_data",
            "signature": {"form": "replaces", "produces": _SCORED_COLUMNS},
            "connector": {"kind": "file", "params": {"path": str(csv_path), "format": "csv"}}}


def _review_stage_full():
    return {"id": "review", "description": "Review", "type": "human_review_queue",
            "inputs": [{"id": "load"}],
            "signature": {"form": "extends",
                          "reads": reads_of("load", [
                              {"name": "id", "type": "str", "nullable": True},
                              {"name": "score", "type": "int", "nullable": True}]),
                          "adds": _REVIEW_COLUMNS},
            "queue": dict(QUEUE_COLUMNS)}


def test_resume_reattaches_cached_decisions_written_via_the_seam(tmp_path):
    """Pins fingerprint reattachment across the upstream-frame reload resume_run performs from parquet."""
    project_dir = tmp_path / "resume_cache_project"
    _write_stage(project_dir, "01_load.json", _load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _review_stage_full())
    _seed_version(project_dir)

    halted = run_prepared(prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir)))
    assert halted["status"] == "awaiting_review"
    run_id = halted["run_id"]

    run_dir = project_dir / "runs" / run_id
    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    assert len(snapshot) == 2
    fingerprints = _read_fingerprints(project_dir.name, run_dir.name)

    _approve_every_row(snapshot, fingerprints, project=project_dir.name)

    resumed = runner.resume_run(project_dir / "runs" / run_id, project_dir.name, run_id,
                            *resumed_stages(project_dir, run_id))
    assert resumed["status"] == "ok"
    out = pd.read_parquet(run_dir / "outputs" / "review.parquet")
    assert sorted(out["human_score"].tolist()) == [1, 2]


def test_resume_replays_the_runs_bust_cache(tmp_path):
    project_dir = tmp_path / "resume_bust_project"
    _write_stage(project_dir, "01_load.json", _load_stage(project_dir))
    _write_stage(project_dir, "02_review.json", _review_stage_full())
    _seed_version(project_dir)

    halted = run_prepared(
        prepare_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir), bust_cache=True))
    assert halted["status"] == "awaiting_review"
    run_id = halted["run_id"]

    run_dir = project_dir / "runs" / run_id
    assert read_manifest(run_dir.parent.parent, run_dir.name)["parameters"]["bust_cache"]

    snapshot = pd.read_parquet(run_dir / "queue" / "review.parquet")
    fingerprints = _read_fingerprints(project_dir.name, run_dir.name)
    _approve_every_row(snapshot, fingerprints, project=project_dir.name)

    resumed = runner.resume_run(project_dir / "runs" / run_id, project_dir.name, run_id,
                            *resumed_stages(project_dir, run_id))
    assert resumed["status"] == "awaiting_review"
