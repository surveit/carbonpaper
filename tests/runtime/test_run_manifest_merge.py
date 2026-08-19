from __future__ import annotations

from app.core.run_status import RunStatus, StageStatus
from app.models.run_manifest import StageRecord
from app.models.run_parameters import RunParameters
from app.runtime.manifest import PRODUCTION_RUNS, RunManifest, read_run_manifest, write_manifest


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

    assert left_view.find_stage_record("left") is not None
    assert right_view.find_stage_record("right") is not None

    left_view.find_stage_record("left").status = StageStatus.OK  # type: ignore[union-attr]
    right_view.find_stage_record("right").status = StageStatus.OK  # type: ignore[union-attr]

    write_manifest(left_view)
    write_manifest(right_view)

    merged = read_run_manifest(project, run_id)
    assert merged.find_stage_record("left").status == StageStatus.OK  # type: ignore[union-attr]
    assert merged.find_stage_record("right").status == StageStatus.OK  # type: ignore[union-attr]
