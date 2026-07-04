"""Filesystem storage for eval configs and runs, plus status derivation.

Configs are mutable authored objects (`eval_config/{id}.yaml`, overwrite
allowed). Runs are written elsewhere and only read here (`eval_run/*.json`).
Dataset uploads are immutable once written (`eval_data/{filename}`) — a second
upload under the same name is refused rather than silently replacing the file
a config may already point at.

Directories are created on write and never assumed to exist on read: listing
or loading against a methodology with no eval activity yet returns empty
results rather than creating scaffolding.
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
from app.services.eval_compat import CompatibilityReport
from app.services.versioning import list_versions

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _eval_config_dir(methodology_dir: Path) -> Path:
    return Path(methodology_dir) / "eval_config"


def _eval_run_dir(methodology_dir: Path) -> Path:
    return Path(methodology_dir) / "eval_run"


def _eval_data_dir(methodology_dir: Path) -> Path:
    return Path(methodology_dir) / "eval_data"


@dataclass
class EvalConfigEntry:
    """One `eval_config/*.yaml` file: its parsed EvalConfig (None if unreadable
    or invalid) and any issues encountered while loading it."""
    config: EvalConfig | None
    path: Path
    issues: list[str] = field(default_factory=list)


def list_eval_configs(methodology_dir: Path) -> list[EvalConfigEntry]:
    """All eval configs under `eval_config/`, tolerant of per-file problems: a
    malformed or invalid file becomes an entry with `issues` set and
    `config=None` rather than being dropped or aborting the whole listing.
    Empty (or absent) `eval_config/` yields an empty list."""
    config_dir = _eval_config_dir(methodology_dir)
    if not config_dir.is_dir():
        return []
    entries: list[EvalConfigEntry] = []
    for path in sorted(config_dir.glob("*.yaml")):
        entry = EvalConfigEntry(config=None, path=path, issues=[])
        entries.append(entry)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            entry.issues.append(f"YAML parse error in {path}: {exc}")
            continue
        if not data:
            entry.issues.append(f"file is empty: {path}")
            continue
        try:
            entry.config = EvalConfig.model_validate(data)
        except ValidationError as exc:
            entry.issues.extend(format_errors(exc))
    return entries


def load_eval_config(methodology_dir: Path, eval_id: str) -> EvalConfig:
    """Load one eval config by id. Raises `FileNotFoundError` if the file is
    absent, `ValueError` (naming the path) if it is unreadable YAML or fails
    the EvalConfig contract — never returns a partial or best-guess config."""
    path = _eval_config_dir(methodology_dir) / f"{eval_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no eval config at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed eval config YAML at {path}: {exc}") from exc
    if not data:
        raise ValueError(f"eval config file is empty: {path}")
    try:
        return EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"invalid eval config at {path}: {'; '.join(format_errors(exc))}"
        ) from exc


def save_eval_config(methodology_dir: Path, config: EvalConfig) -> Path:
    """Write `config` to `eval_config/{config.id}.yaml`. Overwrite allowed —
    configs are mutable authored objects, unlike dataset uploads."""
    config_dir = _eval_config_dir(methodology_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{config.id}.yaml"
    path.write_text(
        yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)),
        encoding="utf-8")
    return path


def list_eval_runs(methodology_dir: Path, config_id: str) -> list[EvalRun]:
    """All runs of `config_id`, newest-first by `(started_at or "", id)`. Runs
    are written elsewhere; this only reads `eval_run/*.json` (absent dir ->
    empty list, never created here)."""
    run_dir = _eval_run_dir(methodology_dir)
    if not run_dir.is_dir():
        return []
    runs: list[EvalRun] = []
    for path in run_dir.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        run = EvalRun.model_validate(data)
        if run.config == config_id:
            runs.append(run)
    runs.sort(key=lambda r: (r.started_at or "", r.id), reverse=True)
    return runs


def save_dataset_upload(methodology_dir: Path, filename: str, content: bytes) -> Path:
    """Write an uploaded dataset file to `eval_data/{filename}`. `filename`
    must be a bare slugish name (starts alnum, then letters/digits/`_`/`-`/`.`;
    no path separators, so it is safe to use directly as a path component).
    Raises `FileExistsError` if the target already exists: uploaded data files
    are immutable once written."""
    if not _SLUG_RE.match(filename):
        raise ValueError(f"not a valid upload filename: {filename!r}")
    data_dir = _eval_data_dir(methodology_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / filename
    if path.exists():
        raise FileExistsError(f"dataset upload already exists: {path}")
    path.write_bytes(content)
    return path


def latest_version_id(methodology_dir: Path) -> str | None:
    """The id of the newest version, or None if the methodology has never been
    versioned."""
    versions = list_versions(methodology_dir)
    if not versions:
        return None
    latest = versions[0]
    version_id = latest["id"]
    return str(version_id) if version_id is not None else None


def eval_status(report: CompatibilityReport, runs: list[EvalRun],
                latest_version: str | None) -> str:
    """One word for "what do we currently know about this eval". Ordered by
    alarm: incompatible beats everything; a result only counts as current when
    its run pinned the version the methodology is at now. "run succeeded" means
    the run produced a result — the metrics say whether the result is good."""
    if not report.ok:
        return "broken"
    if not runs:
        return "never run"
    latest = runs[0]
    if latest_version is None or latest.methodology_version != latest_version:
        return "stale"
    if latest.status in ("error", "vetoed"):
        return "run errored"
    return "run succeeded"


__all__ = [
    "EvalConfigEntry",
    "list_eval_configs",
    "load_eval_config",
    "save_eval_config",
    "list_eval_runs",
    "save_dataset_upload",
    "latest_version_id",
    "eval_status",
]
