"""Eval config/run storage (the document store), plus the status rule.
Configs are mutable authored documents (write is upsert); runs are immutable,
minted by the runner and only read here. Both are project-scoped by document id,
so a project with no eval activity returns empty rather than needing scaffolding.
Datasets stay at `<project_dir>/eval_data/{f}` — the path the TableRef carries.
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
from app.services.versioning import find_latest_version_id

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class EvalConfigEntry:
    config: EvalConfig | None
    id: str
    issues: list[str] = field(default_factory=list)


def list_eval_configs(project_dir: Path) -> list[EvalConfigEntry]:
    return list_project_eval_configs(project_dir.name)


def list_project_eval_configs(project_id: str) -> list[EvalConfigEntry]:
    """The id-taking twin: these documents are keyed by project name, never by path."""
    entries: list[EvalConfigEntry] = []
    for doc_id, data in get_store().read_all("eval", f"{project_id}/"):
        local_id = doc_id.split("/", 1)[1]
        try:
            entries.append(EvalConfigEntry(config=EvalConfig.model_validate(data), id=local_id))
        except ValidationError as exc:
            entries.append(EvalConfigEntry(config=None, id=local_id, issues=format_errors(exc)))
    return entries


def load_eval_config(project_dir: Path, eval_id: str) -> EvalConfig:
    try:
        data = get_store().read("eval", f"{project_dir.name}/{eval_id}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(
            f"no eval config '{eval_id}' in project '{project_dir.name}'") from exc
    try:
        return EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"invalid eval config '{eval_id}' in project '{project_dir.name}': "
            f"{'; '.join(format_errors(exc))}"
        ) from exc


def save_eval_config(project_dir: Path, config: EvalConfig) -> None:
    write_eval_config(project_dir.name, config)


def save_eval_run(project_dir: Path, run: EvalRun) -> None:
    get_store().write("eval_run", f"{project_dir.name}/{run.id}", run.model_dump(mode="json"))


def load_eval_run(project_dir: Path, run_id: str) -> EvalRun:
    if not _SLUG_RE.match(run_id):
        raise ValueError(f"not a valid run id: {run_id!r}")
    try:
        data = get_store().read("eval_run", f"{project_dir.name}/{run_id}")
    except DocumentNotFound as exc:
        raise FileNotFoundError(
            f"no eval run '{run_id}' in project '{project_dir.name}'") from exc
    try:
        return EvalRun.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"invalid eval run '{run_id}' in project '{project_dir.name}': "
            f"{'; '.join(format_errors(exc))}"
        ) from exc


def list_eval_runs(project_dir: Path, config_id: str) -> list[EvalRun]:
    return list_project_eval_runs(project_dir.name, config_id)


def list_project_eval_runs(project_id: str, config_id: str) -> list[EvalRun]:
    runs = [EvalRun.model_validate(data)
            for _, data in get_store().read_all("eval_run", f"{project_id}/")]
    runs = [r for r in runs if r.config == config_id]
    runs.sort(key=lambda r: (r.started_at or "", r.id), reverse=True)
    return runs


def save_dataset_upload(project_dir: Path, filename: str, content: bytes) -> str:
    if _resolve_dataset_path(project_dir, filename).exists():
        raise FileExistsError(f"dataset upload already exists: {filename}")
    return write_eval_dataset(project_dir, filename, content)


def write_eval_dataset(project_dir: Path, filename: str, content: bytes) -> str:
    """Returns what a TableRef then carries, so writer and config cannot name different files."""
    path = _resolve_dataset_path(project_dir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path.relative_to(project_dir).as_posix()


def latest_version_id(project_dir: Path) -> str | None:
    return find_latest_version_id(project_dir)


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
    if latest_version is None or latest.workflow_version != latest_version:
        return "stale"
    if latest.status in ("error", "vetoed"):
        return "run errored"
    return "run succeeded"


# ── Directory layout ─────────────────────────────────────────────────────────
def _resolve_dataset_path(project_dir: Path, filename: str) -> Path:
    if not _SLUG_RE.match(filename):
        raise ValueError(f"not a valid dataset filename: {filename!r}")
    return Path(project_dir) / "eval_data" / filename
