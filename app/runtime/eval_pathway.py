"""Execute the override→target slice of a workflow for an eval.

An eval doesn't run the whole workflow: it injects fixed tables AS certain stages'
outputs (the eval dataset at the override stage, plus any reference overrides) and
runs only the `frontier` — the target stage and its non-overridden ancestors — to
produce the target's output on those injected rows. This reuses the runner's
`_execute_stages` core so injected stages are indistinguishable from executed ones
to everything downstream; it just seeds their outputs instead of computing them.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.errors import EvalPathwayError
from app.models import Stage
from app.runtime.runner import _execute_stages, topological_sort
from app.services import versioning


def execute_eval_pathway(
    project_dir: Path,
    repo_root: Path,
    *,
    version_id: str,
    injected_outputs: dict[str, pd.DataFrame],
    frontier: list[str],
    target: str,
    run_dir: Path,
) -> pd.DataFrame:
    """Run the `frontier` stages of the version-`version_id` workflow with
    `injected_outputs` seeded as their named stages' outputs, and return the
    `target` stage's output frame.

    Raises EvalPathwayError if a frontier stage errors or the path halts for
    review, so the caller records an `error` run rather than scoring a partial
    result."""
    frontier_stages = _load_frontier_stages(project_dir, version_id, frontier)
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    outputs: dict[str, pd.DataFrame] = dict(injected_outputs)
    manifest = _execute_stages(
        frontier_stages, _build_pathway_ctx(project_dir, repo_root, run_dir),
        _build_initial_manifest(project_dir, version_id, run_dir, frontier_stages),
        run_dir, outputs)
    _raise_if_pathway_broke(manifest, target)
    return outputs[target]


def _load_frontier_stages(project_dir: Path, version_id: str, frontier: list[str]) -> list[Stage]:
    """The frontier stages from the pinned version snapshot, topologically ordered.
    Inputs that name a stage outside the frontier (an injected override) are simply
    not ordered here — their output is seeded, not computed."""
    by_id = {s.id: s for s in versioning.load_version_stages(project_dir, version_id)}
    missing = [sid for sid in frontier if sid not in by_id]
    if missing:
        raise EvalPathwayError(f"frontier names stage(s) not in the workflow: {missing}")
    return topological_sort([by_id[sid] for sid in frontier])


def _build_pathway_ctx(project_dir: Path, repo_root: Path, run_dir: Path) -> dict[str, Any]:
    return {
        "repo_root": repo_root, "run_dir": run_dir, "project_dir": project_dir,
        "queue_stats": {}, "limits": {}, "offsets": {},
    }


def _build_initial_manifest(
    project_dir: Path, version_id: str, run_dir: Path, frontier_stages: list[Stage],
) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "project": project_dir.name,
        "workflow_version": version_id,
        "status": "running",
        "stages": [{"stage_id": s.id, "type": s.type, "name": s.name,
                    "status": "pending", "input_validation": [], "output_validation": None,
                    "elapsed_ms": 0, "rows": 0, "error": None,
                    "started_at": None, "finished_at": None}
                   for s in frontier_stages],
    }


def _raise_if_pathway_broke(manifest: dict[str, Any], target: str) -> None:
    """A pathway that didn't finish `ok`/`warnings` can't be scored: surface the
    first stage error (or the halt) as the reason."""
    status = manifest.get("status")
    if status in ("ok", "warnings"):
        return
    if status == "awaiting_review":
        raise EvalPathwayError(
            f"pathway halted for human review at {manifest.get('halted_at')!r}; "
            "an eval can't clear a review queue")
    for stage in manifest.get("stages", []):
        if stage.get("status") == "error":
            error = stage.get("error") or {}
            raise EvalPathwayError(
                f"stage {stage['stage_id']!r} errored: {error.get('message', 'unknown error')}")
    raise EvalPathwayError(f"pathway did not produce target {target!r} (status {status!r})")
