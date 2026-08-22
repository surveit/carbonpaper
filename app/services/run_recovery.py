"""Restarting runs whose executor died, at boot. docs/run-leases.md"""
from __future__ import annotations

import logging
import threading

from app.core.background import run_in_background
from app.core.errors import DocumentNotFound, RunVersionUnresolvableError
from app.core.persistence import get_store
from app.core.run_lease import RunLease
from app.core.run_status import RunStatus
from app.services.errors import WorkflowLoadError
from app.services.run import (
    PRODUCTION_RUNS,
    RunLeaseLost,
    RunManifest,
    abandon,
    resume_now,
)

logger = logging.getLogger(__name__)

# Tenures, not automatic resumes: docs/run-leases.md
MAX_TENURES = 3

# Named once per process, not once per sweep.
_reported_ownerless: set[str] = set()

# A run that cannot execute again at all; one must not stop the sweep reaching the rest.
_UNRESTARTABLE = (
    DocumentNotFound, OSError, RunVersionUnresolvableError, ValueError, WorkflowLoadError,
)


# Why periodic and not a boot hook: docs/run-leases.md
SWEEP_EVERY_SECONDS = 30


def watch_for_interrupted_runs(stop: threading.Event) -> None:
    """Boot hook. Sweeps now, then keeps sweeping — a tenure expires long after any boot."""
    run_in_background(lambda: _sweep_until(stop))


def resume_interrupted_runs() -> None:
    """One sweep. Serialized: N runs resuming at once is what exhausts memory."""
    runs = load_production_runs()
    _report_ownerless_runs(runs)
    interrupted = find_interrupted_runs(runs)
    if not interrupted:
        return
    logger.info("resuming %d interrupted run(s)", len(interrupted))
    _resume_each(interrupted)


def _sweep_until(stop: threading.Event) -> None:
    resume_interrupted_runs()
    while not stop.wait(SWEEP_EVERY_SECONDS):
        resume_interrupted_runs()


def _report_ownerless_runs(runs: list[RunManifest]) -> None:
    """Naming them is the whole remedy: resuming one would assume what killed it."""
    ownerless = [m for m in find_ownerless_runs(runs) if m.id not in _reported_ownerless]
    _reported_ownerless.update(m.id for m in ownerless)
    if ownerless:
        logger.warning(
            "%d run(s) say `running` but never held a lease, so nothing proves their "
            "executor died; left alone for a human to resume or cancel: %s",
            len(ownerless), ", ".join(f"{m.project}/{m.run_id}" for m in ownerless))


def find_interrupted_runs(runs: list[RunManifest]) -> list[tuple[RunManifest, RunLease]]:
    """A `running` record whose lease has expired: its executor is provably gone."""
    now = get_store().store_now()
    interrupted = []
    for manifest in runs:
        held = _read_expired_lease(manifest, now)
        if held is not None:
            interrupted.append((manifest, held))
    return interrupted


def find_ownerless_runs(runs: list[RunManifest]) -> list[RunManifest]:
    """`running`, and no lease ever recorded. Their executors cannot be proven dead."""
    return [
        manifest
        for manifest in runs
        if manifest.status == RunStatus.RUNNING and get_store().read_lease(manifest.id) is None
    ]


def _read_expired_lease(manifest: RunManifest, now: int) -> RunLease | None:
    if manifest.status != RunStatus.RUNNING:
        return None
    held = get_store().read_lease(manifest.id)
    return held if held is not None and held.expires_at <= now else None


def _resume_each(interrupted: list[tuple[RunManifest, RunLease]]) -> None:
    for manifest, held in interrupted:
        if held.fence >= MAX_TENURES:
            _abandon(manifest, held.fence)
        else:
            _resume_one(manifest)


def _resume_one(manifest: RunManifest) -> None:
    logger.info("resuming run %s of %s", manifest.run_id, manifest.project)
    try:
        resume_now(manifest.project, manifest.run_id)
    except RunLeaseLost:
        logger.info("run %s was taken by another executor first", manifest.run_id)
    except _UNRESTARTABLE as exc:
        logger.warning("could not resume run %s: %s", manifest.run_id, exc)


def _abandon(manifest: RunManifest, tenures: int) -> None:
    detail = f"its executor died {tenures} times and was not picked up again"
    if abandon(manifest.project, manifest.run_id, detail):
        logger.warning("abandoned run %s of %s: %s", manifest.run_id, manifest.project, detail)


def load_production_runs() -> list[RunManifest]:
    """Off the store, not the projects root, which would miss a run whose directory is gone."""
    manifests = []
    for doc_id in get_store().list_ids(RunManifest.collection, ""):
        if f"/{PRODUCTION_RUNS}/" not in doc_id:
            continue
        manifest = RunManifest.load_or_none(doc_id)
        if manifest is not None:
            manifests.append(manifest)
    return manifests
