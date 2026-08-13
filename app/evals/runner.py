"""Run one eval against a pinned workflow version and record the result.

v1 scores DECLARATIVELY only -- a path that isn't grain-preserving is recorded as
`vetoed`. An eval that no longer fits the workflow, or has no dataset, raises
rather than recording a fake result.
"""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from app.core.errors import EvalGrainViolationError, EvalNotScorableError, SubsetRunError
from app.core.frames import write_frame_file
from app.evals.dataset import read_table_ref
from app.evals.scoring import score_expected_outputs
from app.models import EvalConfig, EvalRun, EvalRunSettings, Workflow, WorkflowStage
from app.core.frames import table_to_frame
from app.runtime.executor import run_subset
from app.evals.compatibility import CompatibilityReport, validate_eval_compatibility
from app.evals.dataset_columns import (
    deconflict_column_names,
    get_output_columns_from_stage,
)
from app.evals.store import latest_version_id, resolve_eval_run_dir, save_eval_run
from app.services.versioning import load_version, load_version_stages
from app.services.workspace import resolve_project_dir


def run_eval(
    project_id: str, config: EvalConfig, *, version_id: str | None = None,
) -> EvalRun:
    """Blocks until the eval is scored; the returned record is the final one."""
    prepared = _prepare_eval_run(project_id, config, version_id)
    run = (_score_run(prepared, _mint_run_id(), _now())
           if prepared.settings.can_score_declaratively else _vetoed_run(prepared))
    save_eval_run(project_id, run)
    return run


def start_eval_run(
    project_id: str, config: EvalConfig, *, version_id: str | None = None,
) -> EvalRun:
    """Returns at once with a `running` record; scoring lands on a daemon thread."""
    prepared = _prepare_eval_run(project_id, config, version_id)
    if not prepared.settings.can_score_declaratively:
        # Nothing executes, so there is nothing to wait on — the veto is the result.
        vetoed = _vetoed_run(prepared)
        save_eval_run(project_id, vetoed)
        return vetoed
    run_id, started = _mint_run_id(), _now()
    running = _build_run(prepared, run_id=run_id, status="running",
                         started=started, finished=None)
    save_eval_run(project_id, running)
    threading.Thread(target=_score_in_background, daemon=True,
                     args=(prepared, run_id, started)).start()
    return running


def _score_in_background(prepared: _PreparedEval, run_id: str, started: str) -> None:
    try:
        run = _score_run(prepared, run_id, started)
    except Exception:  # noqa: BLE001 — a record left at `running` forever is a silent lie
        run = _build_run(prepared, run_id=run_id, status="error", started=started,
                         finished=_now(), notes=[traceback.format_exc()])
    save_eval_run(prepared.project_id, run)


# ── What both entry points resolve before anything is recorded ───────────────

@dataclass(frozen=True)
class _PreparedEval:
    project_id: str
    config: EvalConfig
    version: str
    settings: EvalRunSettings
    workflow: Workflow


def _prepare_eval_run(
    project_id: str, config: EvalConfig, version_id: str | None,
) -> _PreparedEval:
    version = _resolve_version(project_id, version_id)
    workflow = Workflow(stages=load_version_stages(project_id, version))
    report = validate_eval_compatibility(config, workflow)
    _require_runnable(config, report)
    settings = report.settings
    assert settings is not None  # report.ok (checked above) guarantees settings
    return _PreparedEval(project_id=project_id, config=config, version=version,
                         settings=settings, workflow=workflow)


def _require_runnable(config: EvalConfig, report: CompatibilityReport) -> None:
    if not report.ok:
        raise EvalNotScorableError(
            "eval is incompatible with the workflow: " + "; ".join(report.problems))
    if config.table is None:
        raise EvalNotScorableError("eval has no dataset attached")


def _score_run(prepared: _PreparedEval, run_id: str, started: str) -> EvalRun:
    config, workflow = prepared.config, prepared.workflow
    by_id = workflow.index_workflow_stages_by_id()
    override, target = by_id[config.override_stage], by_id[config.target_stage]
    assert config.table is not None  # _require_runnable checked this
    dataset = read_table_ref(config.table)
    run_dir = resolve_eval_run_dir(prepared.project_id, run_id)
    try:
        outputs = run_subset(
            workflow, stage_ids=prepared.settings.frontier, run_dir=run_dir,
            injected_outputs=_build_injected_outputs(config, override, target, dataset),
            project_id=prepared.project_id, workflow_version=prepared.version)
        score = score_expected_outputs(config, override, target, dataset,
                                       table_to_frame(outputs[config.target_stage]))
    except (SubsetRunError, EvalGrainViolationError) as exc:
        return _build_run(prepared, run_id=run_id, status="error",
                          started=started, finished=_now(), notes=[str(exc)])
    result_ref = _write_result_table(run_dir, score.per_row).relative_to(
        resolve_project_dir(prepared.project_id)).as_posix()
    return _build_run(prepared, run_id=run_id, status="scored", started=started,
                      finished=_now(), metrics=score.metrics, result_ref=result_ref)


def _build_injected_outputs(
    config: EvalConfig, override: WorkflowStage, target: WorkflowStage,
    dataset: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    outputs = {config.override_stage: _compute_override_output(override, target, config, dataset)}
    for ref in config.reference_overrides:
        outputs[ref.stage_id] = read_table_ref(ref.table)
    return outputs


def _compute_override_output(
    override: WorkflowStage, target: WorkflowStage, config: EvalConfig,
    dataset: pd.DataFrame,
) -> pd.DataFrame:
    override_columns = get_output_columns_from_stage(override)
    expected_source = [
        {c.name: c for c in get_output_columns_from_stage(target)}[check.output_column]
        for check in config.expected_outputs
    ]
    injected_columns, _ = deconflict_column_names(override_columns, expected_source)
    dataset_to_original = {injected_columns[i].name: override_columns[i].name
                           for i in range(len(override_columns))}
    return dataset[list(dataset_to_original)].rename(columns=dataset_to_original)


# ── Run records ──────────────────────────────────────────────────────────────

def _vetoed_run(prepared: _PreparedEval) -> EvalRun:
    return _build_run(prepared, run_id=_mint_run_id(), status="vetoed",
                started=_now(), finished=_now(), notes=[
                    "path is not grain-preserving, so it can't be scored row-by-row; "
                    f"needs a code scorer for stages {prepared.settings.blocking_stages}"])


def _build_run(
    prepared: _PreparedEval, *,
    run_id: str, status: Literal["running", "scored", "vetoed", "error"],
    started: str, finished: str | None,
    metrics: dict[str, Any] | None = None, result_ref: str | None = None,
    notes: list[str] | None = None,
) -> EvalRun:
    return EvalRun(
        id=run_id, config=prepared.config.id, project=prepared.config.project,
        workflow_version=prepared.version, status=status, settings=prepared.settings,
        metrics=metrics or {}, result_ref=result_ref,
        started_at=started, finished_at=finished, notes=notes or [])


def _write_result_table(run_dir: Path, per_row: pd.DataFrame) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "result.parquet"
    write_frame_file(per_row, path)
    return path


# ── Small helpers ────────────────────────────────────────────────────────────

def _resolve_version(project_id: str, version_id: str | None) -> str:
    """Scores the SELECTED version, published or NOT — that is how a proposal is validated."""
    if version_id is not None:
        load_version(project_id, version_id)  # raises FileNotFoundError if missing
        return version_id
    version = latest_version_id(project_id)
    if version is None:
        raise EvalNotScorableError(
            "project has no workflow version to run the eval against")
    return version


# datetime.now() advances in ~15ms steps on Windows, so two runs started inside one tick
# read the same instant. The last instant minted is kept here so the next id is nudged
# past it rather than colliding and overwriting that run's record.
_mint_lock = threading.Lock()
_last_minted = datetime.min


def _mint_run_id() -> str:
    global _last_minted
    with _mint_lock:
        _last_minted = max(datetime.now(), _last_minted + timedelta(microseconds=1))
        # Lowercase so it passes the EvalRun slug rule (no uppercase 'T' separator).
        return _last_minted.strftime("run_%Y%m%d_%H%M%S_%f")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
