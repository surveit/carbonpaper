"""Eval config/run storage (the document store), plus the status rule.
Configs are mutable authored documents (write is upsert); runs are immutable,
minted by the runner and only read here. Both are project-scoped by document id,
so a project with no eval activity returns empty rather than needing scaffolding.
Dataset uploads stay on disk at `eval_data/{filename}`, immutable once written.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.core.errors import DocumentNotFound
from app.models import EvalConfig, EvalRun
from app.core.persistence import get_store
from app.core.utils import format_errors
from app.evals.compatibility import CompatibilityReport
from app.services.project import write_eval_config
from app.services.workspace import resolve_project_dir
from app.services.versioning import find_latest_version_id

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class EvalConfigEntry:
    config: EvalConfig | None
    id: str
    issues: list[str] = field(default_factory=list)


def list_eval_configs(project_id: str) -> list[EvalConfigEntry]:
    entries: list[EvalConfigEntry] = []
    for doc_id, data in get_store().read_all("eval", f"{project_id}/"):
        local_id = doc_id.split("/", 1)[1]
        try:
            entries.append(EvalConfigEntry(config=EvalConfig.model_validate(data), id=local_id))
        except ValidationError as exc:
            entries.append(EvalConfigEntry(config=None, id=local_id, issues=format_errors(exc)))
    return entries


def load_eval_config(project_id: str, eval_id: str) -> EvalConfig:
    try:
        data = get_store().read("eval", f"{project_id}/{eval_id}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(
            f"no eval config '{eval_id}' in project '{project_id}'") from exc
    try:
        return EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"invalid eval config '{eval_id}' in project '{project_id}': "
            f"{'; '.join(format_errors(exc))}"
        ) from exc


def save_eval_config(project_id: str, config: EvalConfig) -> None:
    write_eval_config(project_id, config)


def save_eval_run(project_id: str, run: EvalRun) -> None:
    get_store().write("eval_run", f"{project_id}/{run.id}", run.model_dump(mode="json"))


def load_eval_run(project_id: str, run_id: str) -> EvalRun:
    if not _SLUG_RE.match(run_id):
        raise ValueError(f"not a valid run id: {run_id!r}")
    try:
        data = get_store().read("eval_run", f"{project_id}/{run_id}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(
            f"no eval run '{run_id}' in project '{project_id}'") from exc
    try:
        return EvalRun.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"invalid eval run '{run_id}' in project '{project_id}': "
            f"{'; '.join(format_errors(exc))}"
        ) from exc


def list_eval_runs(project_id: str, config_id: str) -> list[EvalRun]:
    runs = [EvalRun.model_validate(data)
            for _, data in get_store().read_all("eval_run", f"{project_id}/")]
    runs = [r for r in runs if r.config == config_id]
    runs.sort(key=lambda r: (r.started_at or "", r.id), reverse=True)
    return runs


def save_dataset_upload(project_id: str, filename: str, content: bytes) -> Path:
    if not _SLUG_RE.match(filename):
        raise ValueError(f"not a valid upload filename: {filename!r}")
    data_dir = _resolve_eval_data_dir(project_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / filename
    if path.exists():
        raise FileExistsError(f"dataset upload already exists: {path}")
    path.write_bytes(content)
    return path


def latest_version_id(project_id: str) -> str | None:
    return find_latest_version_id(project_id)


def eval_status(report: CompatibilityReport, runs: list[EvalRun],
                latest_version: str | None, *, has_eval_dataset: bool) -> str:
    """A "run succeeded" says a result came back, NOT that it is good — read the metrics."""
    if not report.ok:
        return "broken"
    if not has_eval_dataset:
        return "no eval dataset yet"
    if not runs:
        return "never run"
    latest = runs[0]
    if latest.is_running():
        # Before staleness: a run in flight has no verdict to be stale about yet.
        return "running"
    if latest_version is None or latest.workflow_version != latest_version:
        return "stale"
    if latest.status in ("error", "vetoed"):
        return "run errored"
    return "run succeeded"


# `result_ref` is recorded project-relative by the runner, so only this package knows
# what it hangs off — a reader is handed the run's id, never the directory.
def resolve_eval_result_path(project_id: str, result_ref: str) -> Path:
    return resolve_project_dir(project_id) / result_ref


def resolve_eval_run_dir(project_id: str, run_id: str) -> Path:
    return resolve_project_dir(project_id) / "eval_run" / run_id


# The one thing here still on disk rather than in the document store, so the one
# place an id becomes a path — through the resolver that refuses an id escaping
# the workspace.
def _resolve_eval_data_dir(project_id: str) -> Path:
    return resolve_project_dir(project_id) / "eval_data"
