"""Run one eval against a pinned workflow version and record the result.

Ties the pieces together: check the config still fits the workflow, inject the eval
dataset as the override stage's output, run the grain-preserving stage subset to the
target (app.runtime.runner.run_subset), score the target's output against the
dataset's expected columns (app.evals.scoring), and write an EvalRun.

v1 scores DECLARATIVELY only — a path that isn't grain-preserving is recorded as
`vetoed` (a code scorer is the escape hatch, but executing one is not built yet). An
eval that no longer fits the workflow, or has no dataset, can't be run at all and
raises rather than recording a fake result.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from app.core.errors import EvalGrainViolationError, EvalNotScorableError, SubsetRunError
from app.evals.scoring import score_expected_outputs
from app.core.models import (
    EvalConfig, EvalRun, EvalRunSettings, FileFormat, Stage, TableRef, Workflow,
)
from app.runtime.runner import run_subset
from app.evals.compatibility import CompatibilityReport, validate_eval_compatibility
from app.evals.dataset_columns import (
    deconflict_column_names,
    get_output_columns_from_stage,
)
from app.evals.store import latest_version_id, save_eval_run
from app.services.versioning import load_version_meta, load_version_stages


def run_eval(
    project_dir: Path, config: EvalConfig, repo_root: Path, *, version_id: str | None = None,
) -> EvalRun:
    """Run `config` against a workflow version (the newest PUBLISHED version if
    `version_id` is None; see `_resolve_version`) and return the saved EvalRun.
    Raises EvalNotScorableError if the eval can't be run at all (incompatible,
    no dataset attached, or the resolved version isn't published)."""
    version = _resolve_version(project_dir, version_id)
    workflow = Workflow(stages=load_version_stages(project_dir, version))
    report = validate_eval_compatibility(config, workflow.stages)
    _require_runnable(config, report)
    settings = report.settings
    assert settings is not None  # report.ok (checked above) guarantees settings

    if not settings.can_score_declaratively:
        run = _vetoed_run(config, version, settings)
    else:
        run = _score_run(project_dir, repo_root, config, version, settings, workflow)
    save_eval_run(project_dir, run)
    return run


def _require_runnable(config: EvalConfig, report: CompatibilityReport) -> None:
    """An eval that doesn't fit the workflow, or has no dataset, is not runnable —
    raise rather than record a run that scored nothing."""
    if not report.ok:
        raise EvalNotScorableError(
            "eval is incompatible with the workflow: " + "; ".join(report.problems))
    if config.table is None:
        raise EvalNotScorableError("eval has no dataset attached")


def _score_run(
    project_dir: Path, repo_root: Path, config: EvalConfig, version: str,
    settings: EvalRunSettings, workflow: Workflow,
) -> EvalRun:
    """Run the injected stage subset to the target and score its output. A run
    failure or a grain violation is recorded as an `error` run (with the reason),
    not raised — the run happened, it just couldn't produce a score."""
    by_id = {stage.id: stage for stage in workflow.stages}
    override, target = by_id[config.override_stage], by_id[config.target_stage]
    assert config.table is not None  # _require_runnable checked this
    dataset = _read_table_ref(repo_root, config.table)
    run_id = _mint_run_id()
    run_dir = project_dir / "eval_run" / run_id
    started = _now()
    try:
        outputs = run_subset(
            workflow, stage_ids=settings.frontier, run_dir=run_dir, repo_root=repo_root,
            injected_outputs=_build_injected_outputs(repo_root, config, override, target, dataset))
        score = score_expected_outputs(config, override, target, dataset,
                                       outputs[config.target_stage])
    except (SubsetRunError, EvalGrainViolationError) as exc:
        return _build_run(config, version, settings, run_id=run_id, status="error",
                          started=started, notes=[str(exc)])
    result_ref = _write_result_table(run_dir, score.per_row).relative_to(project_dir).as_posix()
    return _build_run(config, version, settings, run_id=run_id, status="scored",
                      started=started, metrics=score.metrics, result_ref=result_ref)


def _build_injected_outputs(
    repo_root: Path, config: EvalConfig, override: Stage, target: Stage, dataset: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """The tables seeded as stage outputs before the subset runs: the eval dataset
    as the override stage's output, plus each reference override's table."""
    outputs = {config.override_stage: _derive_override_output(override, target, config, dataset)}
    for ref in config.reference_overrides:
        outputs[ref.stage_id] = _read_table_ref(repo_root, ref.table)
    return outputs


def _derive_override_output(
    override: Stage, target: Stage, config: EvalConfig, dataset: pd.DataFrame,
) -> pd.DataFrame:
    """Compute the override stage's output from the eval dataset: take its injected
    columns and rename them from their (possibly deconflicted) dataset names back to
    the override stage's own output column names, so downstream stages see the schema
    they expect."""
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

def _vetoed_run(config: EvalConfig, version: str, settings: EvalRunSettings) -> EvalRun:
    return _build_run(config, version, settings, run_id=_mint_run_id(), status="vetoed",
                started=_now(), notes=[
                    "path is not grain-preserving, so it can't be scored row-by-row; "
                    f"needs a code scorer for stages {settings.blocking_stages}"])


def _build_run(
    config: EvalConfig, version: str, settings: EvalRunSettings, *,
    run_id: str, status: Literal["scored", "vetoed", "error"], started: str,
    metrics: dict[str, Any] | None = None, result_ref: str | None = None,
    notes: list[str] | None = None,
) -> EvalRun:
    return EvalRun(
        id=run_id, config=config.id, project=config.project, workflow_version=version,
        status=status, settings=settings, metrics=metrics or {}, result_ref=result_ref,
        started_at=started, finished_at=_now(), notes=notes or [])


def _write_result_table(run_dir: Path, per_row: pd.DataFrame) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "result.parquet"
    per_row.to_parquet(path, index=False)
    return path


# ── Small helpers ────────────────────────────────────────────────────────────

def _resolve_version(project_dir: Path, version_id: str | None) -> str:
    """Resolve the workflow version an eval run will be pinned to, mirroring
    app.runtime.runner.resolve_version_id's gate: an eval run pins a PUBLISHED
    version only, never an agent-minted draft that merely happens to be
    newest. An explicit `version_id` must name an existing, published version
    (a missing version id still raises FileNotFoundError, from
    load_version_meta); None resolves to the newest published version, or
    raises if none is published."""
    if version_id is not None:
        meta = load_version_meta(project_dir, version_id)
        if not meta["published"]:
            raise EvalNotScorableError(
                f"version '{version_id}' of '{project_dir.name}' is not published; "
                "an eval run pins a published version — publish it first."
            )
        return version_id
    version = latest_version_id(project_dir)
    if version is None:
        raise EvalNotScorableError(
            "project has no published workflow version to run the eval against")
    return version


def _read_table_ref(repo_root: Path, table: TableRef) -> pd.DataFrame:
    """Read a TableRef into a DataFrame by its declared format. One case per
    supported format; geojson is not a tabular eval input."""
    path = repo_root / table.path
    if table.format == FileFormat.csv:
        return pd.read_csv(path)
    if table.format == FileFormat.parquet:
        return pd.read_parquet(path)
    if table.format == FileFormat.json:
        return pd.read_json(path, lines=True)
    raise EvalNotScorableError(f"unsupported eval dataset format: {table.format}")


def _mint_run_id() -> str:
    # Lowercase so it passes the EvalRun slug rule (no uppercase 'T' separator).
    return datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
