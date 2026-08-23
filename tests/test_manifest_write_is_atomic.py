"""A run flushes its manifest from a background thread while the run page and the run
tools read the same record. A reader must never see a half-written one — the file
write needed a stage-and-swap to promise that; a document write is atomic already,
and this pins that it still holds, by reading under a writer.
"""
from __future__ import annotations

import threading

from app.core.run_status import RunStatus, StageStatus
from app.models import StageType
from app.models.run_manifest import StageRecord
from app.models.run_parameters import RunParameters
from app.runtime.manifest import read_run_manifest, write_manifest
from app.runtime.manifest import RunManifest

_FLUSHES = 300
_PROJECT = "demo"


def _manifest(run_id: str, stages: int) -> RunManifest:
    return RunManifest(
        id=RunManifest.compose_id(_PROJECT, run_id),
        run_id=run_id,
        started_at="2026-08-10T00:00:00",
        project=_PROJECT,
        workflow_version="v1",
        parameters=RunParameters(),
        input_bindings={},
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        # Growing the record list changes the payload's length between flushes, so a
        # torn read is a short payload rather than a same-size overwrite.
        stage_records=[
            StageRecord(
                stage_id=f"stage_{i}", type=StageType.input_data, started_at=None,
                status=StageStatus.PENDING, input_validation_report=[],
                output_validation_report=None, elapsed_ms=0, output_row_count=0,
                error=None,
            )
            for i in range(stages)
        ],
    )


def test_a_reader_never_sees_a_half_written_manifest() -> None:
    write_manifest(_manifest("R-1", 1))
    stop = threading.Event()
    failures: list[Exception] = []

    def flush() -> None:
        for i in range(_FLUSHES):
            write_manifest(_manifest("R-1", 1 + i % 40))
        stop.set()

    writer = threading.Thread(target=flush)
    writer.start()
    try:
        while not stop.is_set():
            try:
                assert read_run_manifest(_PROJECT, "R-1").run_id == "R-1"
            except Exception as exc:  # noqa: BLE001 — the point is that NOTHING raises
                failures.append(exc)
                break
    finally:
        writer.join()

    assert not failures, f"reader saw a torn manifest: {failures[0]!r}"
