"""Filesystem storage for eval configs and runs, plus status derivation.

Configs are mutable authored objects (`eval_config/{id}.yaml`, overwrite
allowed). Runs are written elsewhere and only read here (`eval_run/*.json`).
Dataset uploads are immutable once written (`eval_data/{filename}`) — a second
upload under the same name is refused rather than silently replacing the file
a config may already point at.

Directories are created on write and never assumed to exist on read: listing
or loading against a project with no eval activity yet returns empty results
rather than creating scaffolding.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.models import EvalConfig, EvalRun
from app.models.schema import format_errors
from app.services.eval_compatibility import CompatibilityReport
from app.services.versioning import list_versions

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class EvalConfigEntry:
    """One `eval_config/*.yaml` file: its parsed EvalConfig (None if unreadable
    or invalid) and any issues encountered while loading it."""
    config: EvalConfig | None
    path: Path
    issues: list[str] = field(default_factory=list)


def list_eval_configs(project_dir: Path) -> list[EvalConfigEntry]:
    """All eval configs under `eval_config/`, tolerant of per-file problems: a
    malformed or invalid file becomes an entry with `issues` set and
    `config=None` rather than being dropped or aborting the whole listing.
    Empty (or absent) `eval_config/` yields an empty list."""
    config_dir = _resolve_eval_config_dir(project_dir)
    if not config_dir.is_dir():
        return []
    entries: list[EvalConfigEntry] = []
    for path in sorted(config_dir.glob("*.yaml")):
        entries.append(_load_config_entry(path))
    return entries


def load_eval_config(project_dir: Path, eval_id: str) -> EvalConfig:
    """Load one eval config by id. Raises `FileNotFoundError` if the file is
    absent, `ValueError` (naming the path) if it is unreadable YAML or fails
    the EvalConfig contract — never returns a partial or best-guess config."""
    path = _resolve_eval_config_dir(project_dir) / f"{eval_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no eval config at {path}")
    data = _read_yaml(path, what="eval config")
    try:
        return EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"invalid eval config at {path}: {'; '.join(format_errors(exc))}"
        ) from exc


def save_eval_config(project_dir: Path, config: EvalConfig) -> Path:
    """Write `config` to `eval_config/{config.id}.yaml`. Overwrite allowed —
    configs are mutable authored objects, unlike dataset uploads."""
    config_dir = _resolve_eval_config_dir(project_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{config.id}.yaml"
    path.write_text(
        yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)),
        encoding="utf-8")
    return path


def save_eval_run(project_dir: Path, run: EvalRun) -> Path:
    """Write `run` to `eval_run/{run.id}.json`. Runs are immutable results —
    a run id is minted per execution, so this never overwrites a real prior run."""
    run_dir = _resolve_eval_run_dir(project_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{run.id}.json"
    path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_eval_run(project_dir: Path, run_id: str) -> EvalRun:
    """Load one run by id, reading only `eval_run/{run_id}.json` -- never the
    whole `eval_run/` directory, so a corrupt sibling run file can't block
    loading a run that is itself fine. Raises `FileNotFoundError` if the file
    is absent, `ValueError` (naming the path) if it is unreadable JSON or
    fails the EvalRun contract. `run_id` must be a bare slugish name (same
    filename-safety check as dataset uploads), so it is safe to use directly
    as a path component."""
    if not _SLUG_RE.match(run_id):
        raise ValueError(f"not a valid run id: {run_id!r}")
    path = _resolve_eval_run_dir(project_dir) / f"{run_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no eval run at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed eval run JSON at {path}: {exc}") from exc
    try:
        return EvalRun.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"invalid eval run at {path}: {'; '.join(format_errors(exc))}"
        ) from exc


def list_eval_runs(project_dir: Path, config_id: str) -> list[EvalRun]:
    """All runs of `config_id`, newest-first by `(started_at or "", id)`, reading
    `eval_run/*.json` (absent dir -> empty list, never created here). Runs are
    written by `save_eval_run`."""
    run_dir = _resolve_eval_run_dir(project_dir)
    if not run_dir.is_dir():
        return []
    runs = [EvalRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
            for path in run_dir.glob("*.json")]
    runs = [r for r in runs if r.config == config_id]
    runs.sort(key=lambda r: (r.started_at or "", r.id), reverse=True)
    return runs


def save_dataset_upload(project_dir: Path, filename: str, content: bytes) -> Path:
    """Write an uploaded dataset file to `eval_data/{filename}`. `filename`
    must be a bare slugish name (starts alnum, then letters/digits/`_`/`-`/`.`;
    no path separators, so it is safe to use directly as a path component).
    Raises `FileExistsError` if the target already exists: uploaded data files
    are immutable once written."""
    if not _SLUG_RE.match(filename):
        raise ValueError(f"not a valid upload filename: {filename!r}")
    data_dir = _resolve_eval_data_dir(project_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / filename
    if path.exists():
        raise FileExistsError(f"dataset upload already exists: {path}")
    path.write_bytes(content)
    return path


def latest_version_id(project_dir: Path) -> str | None:
    """The id of the newest version, or None if the project has never been
    versioned."""
    versions = list_versions(project_dir)
    if not versions:
        return None
    version_id = versions[0]["id"]
    return str(version_id) if version_id is not None else None


def eval_status(report: CompatibilityReport, runs: list[EvalRun],
                latest_version: str | None, *, has_eval_dataset: bool) -> str:
    """One word for "what do we currently know about this eval". Ordered by
    alarm: incompatible beats everything; a config with no eval-dataset file
    can't run yet; a result only counts as current when its run pinned the
    version the project is at now. "run succeeded" means the run produced a
    result — the metrics say whether the result is good."""
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
def _resolve_eval_config_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "eval_config"


def _resolve_eval_run_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "eval_run"


def _resolve_eval_data_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "eval_data"


# ── YAML loading ─────────────────────────────────────────────────────────────
def _read_yaml(path: Path, *, what: str) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed {what} YAML at {path}: {exc}") from exc
    if not data:
        raise ValueError(f"{what} file is empty: {path}")
    return data


def _load_config_entry(path: Path) -> EvalConfigEntry:
    entry = EvalConfigEntry(config=None, path=path, issues=[])
    try:
        data = _read_yaml(path, what="eval config")
    except ValueError as exc:
        entry.issues.append(str(exc))
        return entry
    try:
        entry.config = EvalConfig.model_validate(data)
    except ValidationError as exc:
        entry.issues.extend(format_errors(exc))
    return entry
