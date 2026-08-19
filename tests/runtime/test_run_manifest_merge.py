from __future__ import annotations

from app.core.run_status import RunStatus, StageStatus
from app.models.run_manifest import StageRecord
from app.models.run_parameters import RunParameters
from app.runtime.manifest import (
    PRODUCTION_RUNS,
    RunManifest,
    read_run_manifest,
    write_manifest,
)


def _manifest(project: str, run_id: str) -> RunManifest:
    return RunManifest(
        id=RunManifest.compose_id(project, run_id, PRODUCTION_RUNS),
        run_id=run_id,
        started_at="2026-08-19T10:00:00",
        project=project,
        workflow_version="v1",
        parameters=RunParameters(),
        input_bindings={},
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        stage_records=[
            StageRecord(
                stage_id="left",
                type="input_data",
                status=StageStatus.PENDING,
                input_validation_report=[],
                output_validation_report=None,
                output_row_count=0,
            ),
            StageRecord(
                stage_id="right",
                type="input_data",
                status=StageStatus.PENDING,
                input_validation_report=[],
                output_validation_report=None,
                output_row_count=0,
            ),
        ],
    )


def test_write_manifest_merges_parallel_stage_updates() -> None:
    project = "demo"
    run_id = "20260819T100000"
    write_manifest(_manifest(project, run_id))

    left_view = read_run_manifest(project, run_id)
    right_view = read_run_manifest(project, run_id)

    left = left_view.find_stage_record("left")
    right = right_view.find_stage_record("right")
    assert left is not None
    assert right is not None

    left.status = StageStatus.OK
    right.status = StageStatus.OK

    write_manifest(left_view)
    write_manifest(right_view)

    merged = read_run_manifest(project, run_id)
    merged_left = merged.find_stage_record("left")
    merged_right = merged.find_stage_record("right")
    assert merged_left is not None
    assert merged_right is not None
    assert merged_left.status == StageStatus.OK
    assert merged_right.status == StageStatus.OK


def test_write_manifest_keeps_a_terminal_run_terminal() -> None:
    project = "demo"
    run_id = "20260819T101500"
    write_manifest(_manifest(project, run_id))

    stale_running = read_run_manifest(project, run_id)
    terminal = read_run_manifest(project, run_id)
    terminal.status = RunStatus.ERRORS
    terminal.finished_at = "2026-08-19T10:16:00"

    write_manifest(terminal)
    write_manifest(stale_running)

    merged = read_run_manifest(project, run_id)
    assert merged.status == RunStatus.ERRORS
    assert merged.finished_at == "2026-08-19T10:16:00"
