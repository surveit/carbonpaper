"""Picking a run back up after the process executing it went away.
A deploy kills the run's daemon thread outright, leaving the record saying
`running` — which is TRUE, the run is not over. So nothing here writes a terminal
status except `_give_up`; the rest restarts the work."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from app.core.run_status import RunStatus, StageStatus
from app.models.run_manifest import StageErrorInfo
from app.core.persistence import get_store
from app.services.run import (
    PRODUCTION_RUNS,
    RunManifest,
    resume,
    write_manifest,
)

logger = logging.getLogger(__name__)

# A run that crashes its own process is resumed into the same crash next boot.
# Each pass costs whatever the stage spends before dying, so the count is low.
MAX_RECOVERY_ATTEMPTS = 3

# Off unless the deployment states that this process is the only executor of its
# store. Resuming is only safe under that claim: `running` means "someone is
# executing this", and only a single-executor deployment may read a fresh boot as
# proof that the someone was us and is gone. Two processes over one store (a dev
# server beside `python -m app.cli`) would otherwise both execute the same run.
_SOLE_EXECUTOR_ENV = "CARBON_PAPER_SOLE_EXECUTOR"


def resume_interrupted_runs() -> None:
    """Boot hook. A run whose executor died keeps going; it does not die and it is not buried."""
    if not is_sole_executor():
        return
    for manifest in find_interrupted_runs():
        _pick_up(manifest)


def is_sole_executor() -> bool:
    return os.environ.get(_SOLE_EXECUTOR_ENV, "") == "1"


def find_interrupted_runs() -> list[RunManifest]:
    """Every `running` record: at boot, under sole-executor, each one's executor is gone."""
    return [
        # Off the store, not per project: walking the projects root would miss a
        # run whose project directory is gone while its record — and its spend —
        # remain.
        manifest
        for manifest in _load_every_run_record()
        if manifest.status == RunStatus.RUNNING
    ]


def _load_every_run_record() -> list[RunManifest]:
    """One unreadable record must not stop the rest from being picked up."""
    manifests: list[RunManifest] = []
    for doc_id in get_store().list_ids(RunManifest.collection, ""):
        if f"/{PRODUCTION_RUNS}/" not in doc_id:
            continue
        manifest = RunManifest.load_or_none(doc_id)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _pick_up(manifest: RunManifest) -> None:
    if manifest.recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
        _give_up(manifest)
        return
    if not manifest.workflow_version:
        _give_up(manifest, "the manifest records no workflow version")
        return
    manifest.recovery_attempts += 1
    manifest.resumed_at = datetime.now().isoformat(timespec="seconds")
    write_manifest(manifest)
    logger.info(
        "resuming run %s of %s (attempt %d)",
        manifest.run_id, manifest.project, manifest.recovery_attempts,
    )
    _execute(manifest)


def _execute(manifest: RunManifest) -> None:
    """Through the run service, which owns every production run entry point and backgrounds it."""
    resume(manifest.project, manifest.run_id)


def _give_up(manifest: RunManifest, reason: str = "") -> None:
    """The one place a run IS buried. Silence here is worse than the burial: the page spins forever."""
    detail = reason or (
        f"its executor died {manifest.recovery_attempts} times and was not picked "
        "up again"
    )
    manifest.status = RunStatus.ERRORS
    manifest.finished_at = datetime.now().isoformat(timespec="seconds")
    for record in manifest.stage_records:
        if record.status != StageStatus.RUNNING:
            continue
        record.status = StageStatus.ERROR
        record.error = StageErrorInfo(
            type="RunAbandoned", message=f"Run abandoned: {detail}.", traceback=None
        )
        record.add_note(f"Run abandoned: {detail}.")
    write_manifest(manifest)
    logger.warning("abandoning run %s of %s: %s", manifest.run_id, manifest.project, detail)
