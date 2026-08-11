"""A run flushes its manifest from a background thread while the run page and the run
tools read the same file. A truncating write leaves a window where a reader sees an
empty file, so the write has to be a swap — this pins that, by reading under a writer.
"""
from __future__ import annotations

import threading
from pathlib import Path

from app.core.run_status import RunStatus, StageStatus
from app.models import StageType
from app.models.run_manifest import RunManifest, StageRecord, read_run_manifest
from app.runtime.manifest import write_manifest
from app.models.run_parameters import RunParameters

_FLUSHES = 300


def _manifest(run_id: str, stages: int) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        started_at="2026-08-10T00:00:00",
        project="demo",
        workflow_version="v1",
        parameters=RunParameters(),
        input_bindings={},
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        # Growing the record list changes the file's length between flushes, so a
        # torn read is a short file rather than a same-size overwrite.
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


def test_a_reader_never_sees_a_half_written_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_manifest(run_dir, _manifest("R-1", 1))
    stop = threading.Event()
    failures: list[Exception] = []

    def flush() -> None:
        for i in range(_FLUSHES):
            write_manifest(run_dir, _manifest("R-1", 1 + i % 40))
        stop.set()

    writer = threading.Thread(target=flush)
    writer.start()
    try:
        while not stop.is_set():
            try:
                assert read_run_manifest(run_dir).run_id == "R-1"
            except Exception as exc:  # noqa: BLE001 — the point is that NOTHING raises
                failures.append(exc)
                break
    finally:
        writer.join()

    assert not failures, f"reader saw a torn manifest: {failures[0]!r}"


def test_the_swap_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    write_manifest(run_dir, _manifest("R-2", 3))

    assert [p.name for p in run_dir.iterdir()] == ["manifest.json"]
