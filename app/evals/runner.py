"""Run one eval against a pinned workflow version and record the result.

Scores DECLARATIVELY only: an eval whose path is not grain-preserving, which no
longer fits the workflow, or which has no dataset raises rather than recording a
fake result.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from app.core.background import run_in_background
from app.core.errors import EvalGrainViolationError, EvalNotScorableError, SubsetRunError
from app.core.frames import write_frame_file
from app.evals.dataset import read_table_ref
from app.evals.scoring import score_expected_outputs
from app.models import EvalConfig, EvalRun, Workflow, WorkflowStage
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
    workflow, run = _mint_eval_run(project_id, config, version_id)
    final = _score_run(project_id, config, workflow, run)
    save_eval_run(project_id, final)
    return final


def start_eval_run(
    project_id: str, config: EvalConfig, *, version_id: str | None = None,
) -> EvalRun:
    """Returns at once with a `running` record; scoring lands on a daemon thread."""
    workflow, run = _mint_eval_run(project_id, config, version_id)
    save_eval_run(project_id, run)
    if run.is_running():
        run_in_background(
            lambda: save_eval_run(project_id, _score_run(project_id, config, workflow, run)),
            # A record left at `running` forever is a silent lie.
            on_error=lambda tb: save_eval_run(project_id, _unscored(run, "error", [tb])))
    return run


# ── The record both entry points start from ──────────────────────────────────

def _mint_eval_run(
    project_id: str, config: EvalConfig, version_id: str | None,
) -> tuple[Workflow, EvalRun]:
    """The record every later status is a transition off — minted `running`, saved by the caller."""
    version = _resolve_version(project_id, version_id)
    workflow = Workflow(stages=load_version_stages(project_id, version))
    report = validate_eval_compatibility(config, workflow)
    _require_runnable(config, report)
    settings = report.settings
    assert settings is not None  # report.ok (checked above) guarantees settings
    return workflow, EvalRun(
        id=_mint_run_id(), config=config.id, project=config.project,
        workflow_version=version, status="running", settings=settings,
        started_at=_now())


def _require_runnable(config: EvalConfig, report: CompatibilityReport) -> None:
    if not report.ok:
        raise EvalNotScorableError(
            "eval is incompatible with the workflow: " + "; ".join(report.problems))
    if config.table is None:
        raise EvalNotScorableError("eval has no dataset attached")


def _score_run(
    project_id: str, config: EvalConfig, workflow: Workflow, run: EvalRun,
) -> EvalRun:
    by_id = workflow.index_workflow_stages_by_id()
    override, target = by_id[config.override_stage], by_id[config.target_stage]
    assert config.table is not None  # _require_runnable checked this
    dataset = read_table_ref(config.table)
    run_dir = resolve_eval_run_dir(project_id, run.id)
    try:
        outputs = run_subset(
            workflow, stage_ids=run.settings.frontier, run_dir=run_dir,
            injected_outputs=_build_injected_outputs(config, override, target, dataset),
            project_id=project_id, workflow_version=run.workflow_version)
        score = score_expected_outputs(config, override, target, dataset,
                                       table_to_frame(outputs[config.target_stage]))
    except (SubsetRunError, EvalGrainViolationError) as exc:
        return _unscored(run, "error", [str(exc)])
    result_ref = _write_result_table(run_dir, score.per_row).relative_to(
        resolve_project_dir(project_id)).as_posix()
    return _scored(run, score.metrics, result_ref)


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

def _scored(run: EvalRun, metrics: dict[str, Any], result_ref: str) -> EvalRun:
    return run.model_copy(update={
        "status": "scored", "finished_at": _now(),
        "metrics": metrics, "result_ref": result_ref})


def _unscored(
    run: EvalRun, status: Literal["error"], notes: list[str],
) -> EvalRun:
    return run.model_copy(update={
        "status": status, "finished_at": _now(), "notes": notes})


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
