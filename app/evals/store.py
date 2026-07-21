"""Eval config/run storage (the document store), plus status derivation.

Eval configs are mutable authored documents in the "eval" collection (write is
upsert, so overwrite is inherent). Eval runs are immutable results in the
"eval_run" collection: minted once per execution by the runner and only read
here. Both collections are project-scoped -- each document id is
`f"{project_dir.name}/{local_id}"` -- so listing or loading against a project
with no eval activity yet returns empty results rather than requiring any
scaffolding to exist first.

Dataset uploads are a different kind of payload (raw file bytes, not an eval
document) and stay on disk at `eval_data/{filename}`, immutable once written --
a second upload under the same name is refused rather than silently replacing
the file a config may already point at. (Deferred: dataset uploads and each
run's per-row result table move to a tabular FrameStore in a later slice; this
module only converts configs and runs.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.core.errors import DocumentNotFound
from app.core.models import EvalConfig, EvalRun
from app.core.persistence import get_store
from app.core.utils import format_errors
from app.evals.compatibility import CompatibilityReport
from app.services.versioning import list_versions

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class EvalConfigEntry:
    """One stored eval config: its parsed EvalConfig (None if invalid), the local
    eval `id` (the doc id with the project prefix stripped), and any issues
    encountered while validating it."""
    config: EvalConfig | None
    id: str
    issues: list[str] = field(default_factory=list)


def list_eval_configs(project_dir: Path) -> list[EvalConfigEntry]:
    """All eval configs stored for this project, tolerant of per-document
    problems: a document that fails the EvalConfig contract becomes an entry
    with `issues` set and `config=None` rather than being dropped or aborting
    the whole listing. No configs stored yet -> empty list."""
    entries: list[EvalConfigEntry] = []
    for doc_id, data in get_store().read_all("eval", f"{project_dir.name}/"):
        local_id = doc_id.split("/", 1)[1]
        try:
            entries.append(EvalConfigEntry(config=EvalConfig.model_validate(data), id=local_id))
        except ValidationError as exc:
            entries.append(EvalConfigEntry(config=None, id=local_id, issues=format_errors(exc)))
    return entries


def load_eval_config(project_dir: Path, eval_id: str) -> EvalConfig:
    """Load one eval config by id. Raises `FileNotFoundError` if no such config is
    stored, `ValueError` if the stored document fails the EvalConfig contract --
    never returns a partial or best-guess config."""
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
    """Save `config` as the eval document `{project_dir.name}/{config.id}`.
    Overwrite allowed -- configs are mutable authored objects, unlike dataset
    uploads."""
    get_store().write(
        "eval", f"{project_dir.name}/{config.id}",
        config.model_dump(mode="json", exclude_none=True))


def save_eval_run(project_dir: Path, run: EvalRun) -> None:
    """Save `run` as the eval_run document `{project_dir.name}/{run.id}`. Runs
    are immutable results -- a run id is minted per execution, so this never
    overwrites a real prior run."""
    get_store().write("eval_run", f"{project_dir.name}/{run.id}", run.model_dump(mode="json"))


def load_eval_run(project_dir: Path, run_id: str) -> EvalRun:
    """Load one run by id. Raises `FileNotFoundError` if no such run is stored,
    `ValueError` if `run_id` isn't a valid slug (same format rule as dataset
    upload filenames -- bare, no path separators) or the stored document fails
    the EvalRun contract."""
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
    """All runs of `config_id`, newest-first by `(started_at or "", id)`. No runs
    stored yet -> empty list. Runs are written by `save_eval_run`. This reads and
    validates every run stored for the project, so one malformed run raises
    `ValidationError` rather than being silently dropped from the list."""
    runs = [EvalRun.model_validate(data)
            for _, data in get_store().read_all("eval_run", f"{project_dir.name}/")]
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
    """The id of the newest version overall (any published state), or None if
    the project has no version at all. Eval-scoped: used only by the eval
    runner's default-to-newest resolution and eval status display. Production
    runs use app.runtime.runner.resolve_version_id instead, which pins
    published versions only -- this function does not gate on publication, so
    it is not a substitute for that check."""
    versions = list_versions(project_dir)  # newest-first
    if not versions:
        return None
    return versions[0].version_id


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


# ── Directory layout (dataset uploads only — deferred to a later slice) ──────
def _resolve_eval_data_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "eval_data"
