"""Production run seam: the one service module allowed to drive
app.runtime.runner's production run-lifecycle entry points (prepare_run /
run_prepared / resume_run / resolve_version_id).

The web run UI and any other run driver reach production runs through these
functions rather than importing the runner directly, so "what starts a
production run" has a single named door (enforced by the import-linter contract
"production run entry points reached only by the run service"). The runner still
owns the run mechanics; this seam adds the background-thread launch, the manifest
status read, and the resolution of what a run pinned (its version, its stages,
one stage's definition) that the drivers need."""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Any, Mapping

from app.core.errors import RunNotFoundError, RunVersionUnresolvableError
from app.models import Stage
from app.runtime.manifest import load_manifest_model
from app.runtime.runner import (
    prepare_run,
    resolve_version_id,
    resume_run,
    run_prepared,
)
from app.services.errors import WorkflowLoadError
from app.services.versioning import load_version_stages
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
    prep = prepare_run(
        resolve_project_dir(project),
        repo_root(),
        version_id=version_id,
        limits=limits,
        offsets=offsets,
        bindings=bindings,
        bust_cache=bust_cache,
    )
    _run_in_background(run_prepared, prep)
    return str(prep["run_id"])


def resume(project: str, run_id: str) -> None:
    """Resume a halted or errored run on a background daemon thread. Re-runs
    every not-yet-complete stage and reuses completed upstream outputs (see
    resume_run); launched in the background so the caller can redirect and poll.
    Resolves the project name to its directory and the repo root internally. The
    caller validates the workflow / run existence synchronously first — this only
    handles the background launch."""
    _run_in_background(resume_run, resolve_project_dir(project), run_id, repo_root())


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
    side-effect-free pass-through to the runner's resolver (resolving the project
    name to its directory) so callers outside the runtime (e.g. the web layer's
    project listing) never import the runner."""
    return resolve_version_id(resolve_project_dir(project), version_id)


def load_run_stages(project: str, manifest: dict[str, Any]) -> list[Stage]:
    """The stages of the version this run pinned, from that version's frozen
    document — never `compiled/`, which drifts as the working copy is edited.
    Raises RunVersionUnresolvableError rather than falling back to it."""
    version_id = manifest.get("workflow_version")
    if not version_id:
        raise RunVersionUnresolvableError(
            f"This run of '{project}' records no workflow version in its "
            "manifest, so the workflow it executed cannot be identified."
        )
    try:
        return load_version_stages(resolve_project_dir(project), str(version_id))
    except (FileNotFoundError, WorkflowLoadError) as exc:
        raise RunVersionUnresolvableError(
            f"This run of '{project}' pinned workflow version "
            f"'{version_id}', which could not be read: {exc}"
        ) from exc


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
