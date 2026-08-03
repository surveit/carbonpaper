"""Production run seam: the one service module allowed to drive app.runtime.runner's
production run-lifecycle entry points (enforced by an import-linter contract). Every
other run driver goes through here rather than importing the runner directly."""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Any, Mapping

from app.core.errors import RunNotFoundError, RunVersionUnresolvableError
from app.models import Stage
from app.runtime.manifest import load_manifest_model
from app.runtime.runner import prepare_run, resume_run, run_prepared
from app.services.errors import WorkflowLoadError
from app.services.versioning import (
    WorkflowVersion,
    load_version,
    load_version_stages,
    resolve_version_id,
)
from app.services.workspace import repo_root, resolve_project_dir


def start_run(
    project: str,
    *,
    version_id: str | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bust_cache: bool = False,
) -> str:
    """Set up a run (writes the initial `running` manifest) and launch its
    execution on a background daemon thread, returning the run id immediately so
    a caller can redirect to the run page and poll. Resolves `project` (a project
    name) to its directory and the repo root internally — a caller hands a name,
    never a path. prepare_run does the version-resolution, binding, and preflight
    work up front, so its loud failures (NoVersionToRunError /
    MissingInputBindingError / ValueError / WorkflowLoadError) surface here,
    before any thread starts and before a run dir exists. See prepare_run for
    `version_id` / `bindings` / `limits` / `offsets` / `bust_cache` semantics —
    this seam adds none of its own."""
    prep = _prepare(project, version_id, bindings, limits, offsets, bust_cache)
    _run_in_background(run_prepared, prep)
    return str(prep["run_id"])


def execute(
    project: str,
    *,
    version_id: str | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    """start_run's synchronous twin, for a caller with nothing to poll from: returns
    the final manifest."""
    return run_prepared(
        _prepare(project, version_id, bindings, limits, offsets, bust_cache)
    )


def _prepare(
    project: str,
    version_id: str | None,
    bindings: Mapping[str, Mapping[str, Any]] | None,
    limits: dict[str, int] | None,
    offsets: dict[str, int] | None,
    bust_cache: bool,
) -> dict[str, Any]:
    """Resolve the version, load its frozen stages, hand both to the runner."""
    # Version resolution and snapshot loading are this layer's job: the runner
    # executes the stages it is given and reads no versions itself.
    project_dir = resolve_project_dir(project)
    workflow_version = resolve_version_id(project_dir, version_id)
    return prepare_run(
        project_dir,
        repo_root(),
        load_version_stages(project_dir, workflow_version),
        workflow_version,
        limits=limits,
        offsets=offsets,
        bindings=bindings,
        bust_cache=bust_cache,
    )


def resume(project: str, run_id: str) -> None:
    """Resume a halted or errored run on a background daemon thread. Re-runs
    every not-yet-complete stage and reuses completed upstream outputs (see
    resume_run); launched in the background so the caller can redirect and poll.
    Reloads the stages of the version the run PINNED — never the working copy —
    so the resumed run executes the workflow the halted one did. Resolves the
    project name to its directory and the repo root internally. The caller
    validates the run's existence synchronously first — this only handles the
    background launch."""
    project_dir = resolve_project_dir(project)
    workflow_version = read_pinned_version(project, run_id)
    _run_in_background(
        resume_run,
        project_dir,
        run_id,
        repo_root(),
        load_version_stages(project_dir, workflow_version),
        workflow_version,
    )


def read_pinned_version(project: str, run_id: str) -> str:
    """The workflow version a run is pinned to, off its manifest."""
    # A run carrying no workflow_version predates the version model; fail loudly
    # rather than guessing which snapshot it meant.
    run_dir = resolve_project_dir(project) / "runs" / run_id
    workflow_version = load_manifest_model(run_dir).workflow_version
    if not workflow_version:
        raise RunVersionUnresolvableError(
            f"Run '{run_id}' of '{project}' records no workflow version in its "
            f"manifest ({run_dir / 'manifest.json'}), so the workflow it executed "
            f"cannot be identified — it cannot be resumed."
        )
    return workflow_version


def read_run_status(project: str, run_id: str) -> dict[str, Any]:
    """A run's manifest.json as a dict, parsed through the typed `RunManifest`
    (so a legacy scalar `halted_at` is normalized to a list and unset optional
    fields stay omitted — the same shape the executor persisted). Raises
    RunNotFoundError if the run has no manifest — a bad/expired run id, surfaced
    loudly rather than as an empty or fabricated status."""
    run_dir = resolve_project_dir(project) / "runs" / run_id
    if not (run_dir / "manifest.json").exists():
        raise RunNotFoundError(
            f"no run '{run_id}' for project '{project}' "
            f"(no manifest at {run_dir / 'manifest.json'})"
        )
    return load_manifest_model(run_dir).to_dict()


def resolve_version(project: str, version_id: str | None) -> str:
    """The published workflow version a run would pin to (None -> newest
    published). Raises NoVersionToRunError if `version_id` names an unpublished
    or missing version, or if the project has no published version. A thin,
    side-effect-free pass-through to versioning's resolver, taking the project
    NAME so a caller holding only a name (e.g. the web layer's project listing)
    needs no project directory of its own."""
    return resolve_version_id(resolve_project_dir(project), version_id)


def load_run_version(project: str, manifest: dict[str, Any]) -> WorkflowVersion:
    """The frozen version this run pinned. Never falls back to `compiled/` — raises."""
    version_id = manifest.get("workflow_version")
    if not version_id:
        raise RunVersionUnresolvableError(
            f"This run of '{project}' records no workflow version in its "
            "manifest, so the workflow it executed cannot be identified."
        )
    try:
        return load_version(resolve_project_dir(project), str(version_id))
    except (FileNotFoundError, WorkflowLoadError) as exc:
        raise RunVersionUnresolvableError(
            f"This run of '{project}' pinned workflow version "
            f"'{version_id}', which could not be read: {exc}"
        ) from exc


def load_run_stages(project: str, manifest: dict[str, Any]) -> list[Stage]:
    """The stages of the version this run pinned — the definitions it executed."""
    return load_run_version(project, manifest).stages


@dataclass(frozen=True)
class RunStageDef:
    """`stage` is None both when the version could not be read and when it simply
    defines no such stage — `error` is the discriminator."""

    stage: Stage | None
    error: str | None


def load_pinned_stage_def(
    project: str, manifest: dict[str, Any], stage_id: str
) -> RunStageDef:
    try:
        stages = load_run_stages(project, manifest)
    except RunVersionUnresolvableError as exc:
        return RunStageDef(stage=None, error=str(exc))
    return RunStageDef(
        stage=next((s for s in stages if s.id == stage_id), None), error=None
    )


def _run_in_background(target: Any, *args: Any) -> None:
    """Run a (possibly slow, LLM-driven) execution off the caller's thread so a
    web request can return and poll live progress. Errors are recorded on the
    manifest by the runner itself; this just keeps a dying thread from failing
    silently by printing its traceback."""
    def _wrapped() -> None:
        try:
            target(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    threading.Thread(target=_wrapped, daemon=True).start()
