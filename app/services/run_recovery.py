"""Restarting runs whose executor died, at boot.

A killed executor leaves the record saying `running`, which is TRUE. So nothing here
writes a terminal status except `_abandon`; the rest restarts the work. The lease is what
makes that safe on any number of machines — only an EXPIRED tenure is restarted.
"""
from __future__ import annotations

import logging

from app.core.background import run_in_background
from app.core.errors import DocumentNotFound, RunVersionUnresolvableError
from app.core.persistence import RunLease, get_store
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

# Tenures, not automatic restarts: a human pressing Restart takes one too. A run picked
# up this many times that finished none of them is killing its own process — an OOM on a
# large frame restarts into the same OOM, spending a model call every boot.
MAX_TENURES = 3

# A run that cannot execute again at all: its version no longer loads, or its record or
# directory is gone. One of these must not stop the sweep from reaching the rest.
_UNRESTARTABLE = (
    DocumentNotFound, OSError, RunVersionUnresolvableError, ValueError, WorkflowLoadError,
)


def restart_interrupted_runs() -> None:
    """Boot hook. Serialized on one thread: N runs restarting at once is what exhausts memory."""
    runs = load_production_runs()
    _report_ownerless_runs(runs)
    interrupted = find_interrupted_runs(runs)
    if not interrupted:
        return
    logger.info("restarting %d interrupted run(s)", len(interrupted))
    run_in_background(lambda: _restart_each(interrupted))


def _report_ownerless_runs(runs: list[RunManifest]) -> None:
    """Naming them is the whole remedy: restarting one would assume what killed it."""
    ownerless = find_ownerless_runs(runs)
    if ownerless:
        logger.warning(
            "%d run(s) say `running` but never held a lease, so nothing proves their "
            "executor died; left alone for a human to restart or cancel: %s",
            len(ownerless), ", ".join(f"{m.project}/{m.run_id}" for m in ownerless))


# A record with NO lease predates leasing, so nothing ever proved its executor dead;
# `find_ownerless_runs` surfaces those, and only a human clears them.
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


def _restart_each(interrupted: list[tuple[RunManifest, RunLease]]) -> None:
    for manifest, held in interrupted:
        if held.fence >= MAX_TENURES:
            _abandon(manifest, held.fence)
        else:
            _restart(manifest)


def _restart(manifest: RunManifest) -> None:
    logger.info("restarting run %s of %s", manifest.run_id, manifest.project)
    try:
        resume_now(manifest.project, manifest.run_id)
    except RunLeaseLost:
        # Claimed between the sweep's read and the resume's claim. Theirs, then.
        logger.info("run %s was claimed by another executor first", manifest.run_id)
    except _UNRESTARTABLE as exc:
        logger.warning("could not restart run %s: %s", manifest.run_id, exc)


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
