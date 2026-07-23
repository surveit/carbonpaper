"""Smoke-run seam: a non-production run reaches the shared execution engine
through app.runtime.executor (run_subset), never the production run-lifecycle
entry points in app.runtime.runner — so a smoke run can never mint a production
run record under runs/.

A smoke run samples a workflow's bound source, injects that sample as the source
stage's output, and runs the frontier — every stage from the source (exclusive)
up to but excluding any publish stage — over just those rows, so an author can
see the pipeline execute on real data without paying for the full run or writing
published artifacts. It writes ONLY inside its own `<project_dir>/smoke_runs/<id>/`
dir: run_subset's ctx carries no project_dir, so no cross-run state (e.g. the
decisions store) is reachable. `smoke_runs/` is naming hygiene — no special
semantics attach to the directory.

Manifest shape: the persisted manifest is the production run-manifest shape,
minted through executor.create_run_manifest (the single source of that shape).
run_subset finalizes its own manifest onto disk as it runs (real observed
per-stage statuses and the final run status); this seam reads that finalized
manifest back and overlays its observed `stages`/`status` onto the production
skeleton before the final write, so the recorded statuses are the engine's own —
never synthesized here.

A human_review_queue stage mid-frontier makes a subset run fail loudly: the
subset ctx carries no project_dir (run-scoping — the decisions store is
unreachable), so the queue handler raises on the missing key before it can reach
its own HaltForReview, and run_subset surfaces that as a SubsetRunError. A smoke
run reports it as a failure ({ok: False}) whose message names the queue stage. It
never reads through the queue either way."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import NoSmokeSourceError, SubsetRunError
from app.models import Stage, StageType, Workflow
from app.runtime.executor import (
    create_run_manifest,
    run_subset,
    topological_sort,
    write_manifest,
)
from app.runtime.stages.input_data import read_input_data
from app.services.run import resolve_version
from app.services.versioning import load_version_stages


def run_smoke(
    project_dir: Path,
    repo_root: Path,
    *,
    version_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Sample the resolved version's bound source and run its frontier over the
    sample, writing a production-shape manifest under
    `<project_dir>/smoke_runs/<smoke_run_id>/`. Returns
    `{ok, smoke_run_id, version_id, stages_run, rows_out, error}` — `ok` is True
    when the frontier ran clean, False on any stage error or a human-review halt
    (the halt named in `error`); `rows_out` is the last executed stage's output
    row count when ok, else None (never a fabricated count).

    Version resolution is the production run service's — a smoke run pins a
    published version, same as a real run. Raises NoSmokeSourceError if the
    workflow has no input_data stage to sample from."""
    version = resolve_version(project_dir, version_id)
    stages = load_version_stages(project_dir, version)
    # Sample the source(s) before building the Workflow, so a sourceless workflow
    # fails on the missing source rather than on downstream graph validation.
    injected = _sample_source_outputs(stages, limit=limit, offset=offset)
    workflow = Workflow(stages=stages)
    frontier = topological_sort(_frontier_stages(stages))

    smoke_run_id = _mint_smoke_run_id()
    smoke_dir = project_dir / "smoke_runs" / smoke_run_id
    manifest = create_run_manifest(
        frontier, run_id=smoke_run_id, project=project_dir.name,
        workflow_version=version, run_bindings={}, input_bindings={},
        limits={}, offsets={})
    smoke_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(smoke_dir, manifest)

    stage_ids = [stage.id for stage in frontier]
    ok, error, outputs = _run_frontier(workflow, injected, stage_ids, smoke_dir, repo_root)

    _overlay_observed_outcome(manifest, smoke_dir)
    write_manifest(smoke_dir, manifest)

    return {
        "ok": ok,
        "smoke_run_id": smoke_run_id,
        "version_id": version,
        "stages_run": stage_ids,
        "rows_out": _last_stage_row_count(frontier, outputs) if ok else None,
        "error": error,
    }


def _run_frontier(
    workflow: Workflow,
    injected: dict[str, pd.DataFrame],
    stage_ids: list[str],
    smoke_dir: Path,
    repo_root: Path,
) -> tuple[bool, str | None, dict[str, pd.DataFrame]]:
    """Execute the frontier subset, translating run_subset's clean-or-loud
    contract into a smoke verdict: normal return -> (True, None, outputs); a
    SubsetRunError (a stage errored, or a mid-frontier human_review_queue halted)
    -> (False, its message, {})."""
    try:
        outputs = run_subset(
            workflow, injected_outputs=injected, stage_ids=stage_ids,
            run_dir=smoke_dir, repo_root=repo_root)
    except SubsetRunError as exc:
        return False, str(exc), {}
    return True, None, outputs


def _frontier_stages(stages: list[Stage]) -> list[Stage]:
    """The stages a smoke run executes: every stage that is neither the source
    (input_data — its output is injected, not computed) nor a publish stage
    (excluded so a smoke run writes no published artifacts)."""
    excluded = {StageType.input_data.value, StageType.publish.value}
    return [stage for stage in stages if stage.type not in excluded]


def _sample_source_outputs(
    stages: list[Stage], *, limit: int, offset: int,
) -> dict[str, pd.DataFrame]:
    """Read each input_data stage's bound file and take `df.iloc[offset:offset+limit]`,
    keyed by stage id — the seeded outputs the frontier runs over. Raises
    NoSmokeSourceError if the workflow declares no input_data stage."""
    sources = [stage for stage in stages if stage.type == StageType.input_data.value]
    if not sources:
        raise NoSmokeSourceError(
            "workflow has no input_data stage to sample a smoke run from")
    return {
        source.id: read_input_data(source, {}).iloc[offset:offset + limit]
        for source in sources
    }


def _last_stage_row_count(
    frontier: list[Stage], outputs: dict[str, pd.DataFrame],
) -> int | None:
    """Row count of the last stage in the frontier's topological order — the
    observed output size of the deepest stage a smoke run executed. None when the
    frontier is empty (source straight into publish) — no stage ran, so there is
    no count to report rather than a fabricated zero."""
    if not frontier:
        return None
    return int(len(outputs[frontier[-1].id]))


def _overlay_observed_outcome(manifest: dict[str, Any], smoke_dir: Path) -> None:
    """Overlay the per-stage statuses and final run status that run_subset
    finalized onto disk onto the production-shape `manifest`, so the recorded
    outcome is the engine's own observation, not one synthesized here. Copies the
    run-outcome fields run_subset writes (`stages`, `status`, and the optional
    `halted_at`/`finished_at`/`queue_stats`/`dropped_columns`) when present."""
    finalized = json.loads((smoke_dir / "manifest.json").read_text(encoding="utf-8"))
    for field in ("stages", "status", "halted_at", "finished_at",
                  "queue_stats", "dropped_columns"):
        if field in finalized:
            manifest[field] = finalized[field]


def _mint_smoke_run_id() -> str:
    """A smoke run's id, in the runner's run-id timestamp format so smoke runs
    sort and read consistently with production runs and versions."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")
