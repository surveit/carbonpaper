from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.errors import RunExecutionLeaseLost
from app.core.run_status import RunStatus, StageStatus
from app.models import StageType, Workflow
from app.models.run_manifest import StageErrorInfo, StageRecord
from app.runtime.manifest import (
    EVAL_RUNS,
    RunManifest,
    read_run_manifest,
    write_manifest,
)
from app.runtime.run_log import read_events_since
from app.runtime.run_lease import try_claim_run_execution
from app.runtime.runner import resume_run
from app.services.run import reconcile_interrupted_runs
from app.web.config import templates
from app.web.panel_links import AppPanelLinks
from run_seed import store_events


def test_reconcile_marks_interrupted_stage_and_run_terminal() -> None:
    manifest = _manifest(
        "project",
        "orphan",
        RunStatus.RUNNING,
        [
            _stage("complete", StageStatus.OK, finished_at="2026-08-18T10:00:02"),
            _stage("active", StageStatus.RUNNING),
            _stage("downstream", StageStatus.PENDING),
        ],
        finished_at="2026-08-18T21:55:35",
    )
    _write_expired(manifest)
    store_events("project", "orphan", [
        {"seq": 0, "ts": "2026-08-18T10:00:00", "level": 0,
         "kind": "run_start", "run_id": "orphan", "stage_count": 3},
        {"seq": 1, "ts": "2026-08-18T10:00:01", "level": 0,
         "kind": "stage_start", "stage": "active", "type": "input_data"},
    ])

    reconcile_interrupted_runs()

    settled = read_run_manifest("project", "orphan")
    complete, active, downstream = settled.stage_records
    assert settled.status == RunStatus.ERRORS
    assert settled.finished_at != "2026-08-18T21:55:35"
    assert complete.status == StageStatus.OK
    assert complete.finished_at == "2026-08-18T10:00:02"
    assert active.status == StageStatus.ERROR
    assert active.finished_at == settled.finished_at
    assert active.error == StageErrorInfo(
        type="RunInterrupted",
        message=(
            "The server process stopped before this stage finished. "
            "Carbon Paper cannot recover this stage's result."
        ),
        traceback=None,
    )
    assert downstream.status == StageStatus.PENDING
    assert downstream.finished_at is None
    events = read_events_since("project", "orphan", 0)
    assert [(event["kind"], event.get("stage")) for event in events] == [
        ("run_start", None),
        ("stage_start", "active"),
        ("stage_done", "active"),
        ("run_done", None),
    ]
    assert "elapsed_ms" not in events[2]
    with pytest.raises(RunExecutionLeaseLost):
        write_manifest(manifest)


def test_reconcile_anchors_a_not_started_run_without_claiming_the_stage_ran() -> None:
    manifest = _manifest(
        "project", "prepared", RunStatus.RUNNING,
        [
            _stage("first", StageStatus.PENDING),
            _stage("downstream", StageStatus.PENDING),
        ],
    )
    _write_expired(manifest)

    reconcile_interrupted_runs()

    settled = read_run_manifest("project", "prepared")
    first, downstream = settled.stage_records
    assert settled.status == RunStatus.ERRORS
    assert first.status == StageStatus.ERROR
    assert first.started_at is None
    assert first.finished_at is None
    assert first.error is not None
    assert first.error.type == "RunInterrupted"
    assert first.error.message == (
        "The server process stopped before this stage started. "
        "Carbon Paper cannot continue the run automatically."
    )
    assert downstream.status == StageStatus.PENDING
    assert [event["kind"] for event in read_events_since("project", "prepared", 0)] == [
        "run_done"
    ]


def test_interrupted_stage_panel_does_not_present_zero_as_a_duration() -> None:
    stage = _stage("active", StageStatus.ERROR)
    stage.error = StageErrorInfo(
        type="RunInterrupted",
        message="The server process stopped before this stage finished.",
        traceback=None,
    )

    html = templates.env.get_template("_run_stage_panel.html").render(
        project="project",
        run_id="orphan",
        stage=stage,
        stage_def=None,
        workflow_stage=None,
        stage_def_error=None,
        preview=None,
        diff=None,
        input_previews=[],
        function_code=None,
        llm_example=None,
        test_views=[],
        certification=None,
        eval_coverages=[],
        previewable=False,
        links=AppPanelLinks("project", "orphan"),
        event_tail=100,
        type_glyph={},
        type_class={},
    )

    facts = html.split('class="stage-facts"', 1)[1].split("</p>", 1)[0]
    assert "0 ms" not in facts
    assert "0 rows" in facts


def test_reconcile_is_a_no_op_for_terminal_runs_and_is_idempotent() -> None:
    terminal = _manifest(
        "project", "terminal", RunStatus.OK,
        [_stage("complete", StageStatus.OK, finished_at="2026-08-18T10:00:02")],
        finished_at="2026-08-18T10:00:02",
    )
    orphan = _manifest(
        "project", "orphan", RunStatus.RUNNING,
        [_stage("active", StageStatus.RUNNING)],
    )
    write_manifest(terminal)
    _write_expired(orphan)

    reconcile_interrupted_runs()
    terminal_after_first = read_run_manifest("project", "terminal").model_dump()
    orphan_after_first = read_run_manifest("project", "orphan").model_dump()
    events_after_first = read_events_since("project", "orphan", 0)
    reconcile_interrupted_runs()

    assert read_run_manifest("project", "terminal").model_dump() == terminal_after_first
    assert read_run_manifest("project", "orphan").model_dump() == orphan_after_first
    assert read_events_since("project", "orphan", 0) == events_after_first


def test_reconcile_does_not_touch_eval_subset_runs() -> None:
    manifest = _manifest(
        "project", "eval-subset", RunStatus.RUNNING,
        [_stage("active", StageStatus.RUNNING)],
        area=EVAL_RUNS,
    )
    _write_expired(manifest)

    reconcile_interrupted_runs()

    loaded = read_run_manifest("project", "eval-subset", EVAL_RUNS)
    assert loaded.status == RunStatus.RUNNING
    assert loaded.stage_records[0].status == StageStatus.RUNNING
    assert read_events_since("project", "eval-subset", 0) == []


def test_reconcile_preserves_a_run_owned_by_another_live_server() -> None:
    manifest = _manifest(
        "project", "live", RunStatus.RUNNING,
        [_stage("active", StageStatus.RUNNING)],
    )
    ownership = try_claim_run_execution(manifest.id)
    assert ownership is not None
    manifest.record_execution_attempt(ownership.holder)
    write_manifest(manifest)

    reconcile_interrupted_runs()

    preserved = read_run_manifest("project", "live")
    assert preserved.status == RunStatus.RUNNING
    assert preserved.stage_records[0].status == StageStatus.RUNNING
    assert read_events_since("project", "live", 0) == []


def test_reconcile_preserves_an_ownerless_legacy_run() -> None:
    manifest = _manifest(
        "project", "legacy", RunStatus.RUNNING,
        [_stage("active", StageStatus.RUNNING)],
    )
    write_manifest(manifest)

    reconcile_interrupted_runs()

    assert read_run_manifest("project", "legacy").status == RunStatus.RUNNING


def test_resume_clears_the_prior_finished_at_before_execution(tmp_path, monkeypatch) -> None:
    manifest = _manifest(
        "project", "resume", RunStatus.AWAITING_REVIEW,
        [], finished_at="2026-08-18T21:55:35",
    )
    write_manifest(manifest)
    observed = []

    def observe(ordered, ctx, loaded, run_dir, outputs_so_far):
        observed.append(loaded.finished_at)
        return loaded

    monkeypatch.setattr("app.runtime.runner._execute_stages", observe)

    result = resume_run(tmp_path, "project", "resume", Workflow(stages=[]), "v1")

    assert observed == [None]
    assert result["finished_at"] is None


def _manifest(
    project: str,
    run_id: str,
    status: RunStatus,
    stages: list[StageRecord],
    *,
    finished_at: str | None = None,
    area: str = "runs",
) -> RunManifest:
    return RunManifest(
        id=RunManifest.compose_id(project, run_id, area),
        run_id=run_id,
        started_at="2026-08-18T10:00:00",
        project=project,
        workflow_version="v1",
        human_review_queue_stats={},
        status=status,
        stage_records=stages,
        finished_at=finished_at,
    )


def _stage(
    stage_id: str, status: StageStatus, *, finished_at: str | None = None
) -> StageRecord:
    return StageRecord(
        stage_id=stage_id,
        type=StageType.input_data,
        started_at="2026-08-18T10:00:01" if status == StageStatus.RUNNING else None,
        status=status,
        input_validation_report=[],
        output_validation_report=None,
        elapsed_ms=0,
        output_row_count=1 if status == StageStatus.OK else 0,
        error=None,
        output_path="outputs/complete.parquet" if status == StageStatus.OK else None,
        finished_at=finished_at,
    )


def _write_expired(manifest: RunManifest) -> None:
    ownership = try_claim_run_execution(
        manifest.id, now=datetime(2020, 1, 1, tzinfo=UTC))
    assert ownership is not None
    manifest.record_execution_attempt(ownership.holder)
    write_manifest(manifest)
