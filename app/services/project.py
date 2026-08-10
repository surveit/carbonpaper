"""The project lifecycle service. A project's identity is the Project record (see
below), not its examples/<name>/ working-copy directory — a directory may exist
without a name clash. project_meta degrades TRUTHFULLY when no record can be
found or built — it never invents a model or a creation date. import_project is
import-if-absent: a name clash raises rather than replacing.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Sequence

from pydantic import BaseModel, field_validator

from app.core.errors import ProjectExistsError, RunManifestNotJson
from app.models import (
    SchemaLibrary,
    Stage,
    StageDraft,
    stage_to_json,
    stage_to_spec_dict,
)
from app.models.review_guide import ReviewGuideDraft
from app.models.run_manifest import (
    find_manifest_backed_run_dirs,
    read_run_manifest_json,
    records_a_test_run,
)
from app.services.versioning import ReviewGuide
from app.core.persistence import PersistedModel, PersistenceScope
from app.core.run_status import RunStatus
from app.services import data_model, methodology, stage_edit, versioning, workspace
from app.services import loader
from app.services.errors import WorkflowLoadError
from app.services.stage_edit import AddStagesResult, EditStageResult


# ─── Project identity record ───────────────────────────────────────────────────


class Project(PersistedModel):
    """A project's identity record, stored in the "project" collection. `id` is
    the sanitized project name (see sanitize_project_name) — the source of
    truth for "does this project exist", not examples/<name>/ existing on
    disk. `authored_at` is the project's OWN creation date as a domain fact —
    distinct from PersistedModel's `created_at`/`updated_at`, which stamp
    when this RECORD was written (e.g. by a migration backfill, possibly long
    after the project itself was made). `authored_at` is None when that date
    is genuinely unknown (a legacy project whose date was never recorded) —
    never inferred from the record's own `created_at`."""

    collection: ClassVar[str] = "project"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    title: str | None = None
    model: str | None = None
    source: str | None = None
    authored_at: str | None = None


# ─── Status models ────────────────────────────────────────────────────────────
# The typed shapes project_meta / project_state return. Every field is read off
# disk truthfully (see project_state); an unknown fact is None / 0, never a
# fabricated placeholder.


class DataModelStatus(BaseModel):
    """The project's data-model (named-schema) status: whether any schemas exist
    and how many."""

    present: bool
    n_schemas: int


class WorkflowStatus(BaseModel):
    """The project's compiled-workflow status: whether a workflow exists and how
    many stages it has."""

    present: bool
    n_stages: int


class RunsSummary(BaseModel):
    """Summary of the project's runs/ dir: the count of manifest-backed runs, how
    many are halted awaiting review, and the newest run's status (None when no runs)."""

    n: int
    awaiting_review: int
    latest_status: str | None


class ProjectMeta(BaseModel):
    """A project's identity card. Legacy projects (no stored record) degrade
    truthfully: title / created_at / model / source are None ("unknown") rather than
    fabricated. name is always the directory name."""

    name: str
    title: str | None
    created_at: str | None
    model: str | None
    source: str | None


class ProjectState(BaseModel):
    """The status snapshot the Overview page and shell sidebar render. Every field is
    read off disk truthfully; the web layer adds the "what to do next" CTA on top
    (app.web.project_view.shell_state) — it is not part of this domain snapshot."""

    name: str
    meta: ProjectMeta
    has_document: bool
    data_model: DataModelStatus
    workflow: WorkflowStatus
    versions: int
    runs: RunsSummary


# ─── Stage loading (counts / coverage) ────────────────────────────────────────


def load_stage_specs(project: str) -> list[dict[str, Any]]:
    """Raw specs, valid or not — the draft graph the workflow page falls back to."""
    return loader.read_stage_specs(project)


# ─── Run summary ──────────────────────────────────────────────────────────────


def _runs_summary(pdir: Path) -> RunsSummary:
    """Non-test runs only: a workflow test must never masquerade as the latest production run."""
    statuses: list[tuple[str, str]] = []  # (run_id, status)
    awaiting = 0
    for run in find_manifest_backed_run_dirs(pdir / "runs"):
        try:
            manifest = read_run_manifest_json(run)
        except RunManifestNotJson:
            # Counted, not hidden: a manifest this reader cannot parse carries no
            # `is_test_run` to exclude it by, so it is treated as non-test, same
            # as every run was before that field existed.
            status, is_test_run = "corrupt", False
        else:
            status = manifest.get("status", "unknown")
            is_test_run = records_a_test_run(manifest)
        if is_test_run:
            continue
        statuses.append((run.name, status))
        if status == RunStatus.AWAITING_REVIEW:
            awaiting += 1
    if not statuses:
        return RunsSummary(n=0, awaiting_review=0, latest_status=None)
    # Newest run by id (run ids are strftime timestamps → lexical max is chronological).
    latest_status = max(statuses, key=lambda t: t[0])[1]
    return RunsSummary(n=len(statuses), awaiting_review=awaiting, latest_status=latest_status)


# ─── Project identity (meta) ──────────────────────────────────────────────────


def project_meta(pdir: Path) -> ProjectMeta:
    """The project's identity card (ProjectMeta): name / title / created_at / model /
    source, read from the Project record.

    For a directory with no record, degrades TRUTHFULLY rather than fabricate:
    only `name` (the directory name, always known) is set; title / created_at /
    model / source are all None."""
    pdir = Path(pdir)
    name = pdir.name
    record = Project.load_or_none(name)
    if record is None:
        return ProjectMeta(name=name, title=None, created_at=None, model=None, source=None)
    return ProjectMeta(
        name=name,
        title=record.title,
        created_at=record.authored_at,
        model=record.model,
        source=record.source,
    )


# ─── The status snapshot ──────────────────────────────────────────────────────


def project_state(pdir: Path) -> ProjectState:
    """The single status object (ProjectState) the Overview page and the shell sidebar
    render.

    Fields:
      {
        name, meta,
        has_document,
        data_model: {present, n_schemas},
        workflow:   {present, n_stages},
        versions:   n,
        runs:       {n, awaiting_review, latest_status},
      }

    Every field is read off disk truthfully; runs comes from _runs_summary
    (manifest-backed runs only).

    The shell's "what to do next" CTA is NOT here: its label + section href are a
    UI/routing concern the web layer adds (app.web.project_view.shell_state).
    """
    pdir = Path(pdir)
    name = pdir.name
    meta = project_meta(pdir)

    # ── Data model (named schemas) ──
    schemas = data_model.load_schemas(name)
    data_model_status = DataModelStatus(present=bool(schemas), n_schemas=len(schemas))

    # ── Workflow (compiled stages) ──
    stages = load_stage_specs(name)
    workflow = WorkflowStatus(present=bool(stages), n_stages=len(stages))

    # ── Versions + runs ──
    n_versions = len(versioning.list_versions(pdir))
    runs = _runs_summary(pdir)

    return ProjectState(
        name=name,
        meta=meta,
        has_document=methodology.exists(name),
        data_model=data_model_status,
        workflow=workflow,
        versions=n_versions,
        runs=runs,
    )


# ─── Editing-agent service surface (name-based) ───────────────────────────────
# Thin wrappers the editing agent's tools call. Each takes a project NAME, resolves
# <projects root>/<name> internally, and returns in-memory objects (never a Path) via
# the loader/services — so the agent tools never build a filesystem path. The name
# comes from the model, so it is validated to stay inside the workspace.


def sanitize_project_name(name: str) -> str:
    """Normalize `name` to the safe examples/<name>/ directory form
    create_project uses: lowercased, every run of non-[a-z0-9_] characters
    collapsed to a single `_`, defaulting to "project" if that leaves
    nothing. A pure function so callers that need to know a project's
    directory NAME before calling create_project (e.g. to decide whether one
    already exists) can compute the same name it will use."""
    return re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "project"


def create_project(
    name: str,
    document: str,
    *,
    model: str = "sonnet",
    source: str,
) -> str:
    """Store a NEW project's methodology and identity, and make its working-copy
    directory (which holds input data, not source). Returns the sanitized name.

    Raises ValueError on an empty document. Raises ProjectExistsError on a name
    clash — a stored Project record, or a stored methodology, under `safe_name`.
    An unrelated or empty directory of that name (e.g. input files a user staged
    there by hand) is NOT a clash and is written into. The two refusals carry
    distinguishable messages so a caller can tell them apart."""
    safe_name = sanitize_project_name(name)
    doc = document.strip()
    if not doc:
        raise ValueError("The methodology document is empty.")
    if Project.exists(safe_name):
        raise ProjectExistsError(
            f"project '{safe_name}' already exists — choose a different name."
        )
    project_dir = workspace.projects_dir() / safe_name
    if methodology.exists(safe_name):
        raise ProjectExistsError(
            f"project '{safe_name}' already has a methodology — choose a different name."
        )
    project_dir.mkdir(parents=True, exist_ok=True)
    methodology.write_methodology(safe_name, doc)
    created_at = datetime.now().isoformat(timespec="seconds")
    Project(id=safe_name, title=None, model=model, source=source, authored_at=created_at).save()
    return safe_name


def list_projects() -> list[str]:
    """The names of every project in the workspace. The source of truth is the
    Project store — never directory existence, and never a filesystem scan."""
    return sorted(record.id for record in Project.list())


def describe_workflow(name: str) -> dict[str, Any]:
    """A compact summary of one project's workflow (stage ids/types/inputs/review
    state), read through the tolerant loader."""
    workspace.resolve_project_dir(name)
    return workspace.project_workflow_summary(name)


def read_stage(name: str, stage_id: str) -> str:
    """One stage's stored spec as JSON text; ValueError if it is not in the workflow."""
    workspace.resolve_project_dir(name)
    stage = loader.find_stage(name, stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in project '{name}'")
    return stage_to_json(stage)


def edit_stage(name: str, stage_id: str, changes_json: str) -> EditStageResult:
    """Apply a JSON Merge Patch to one stage of a project's workflow (validated
    before it writes; nothing written on failure)."""
    return stage_edit.patch_stage_spec(_project_to_write(name), stage_id, changes_json)


def add_stage(name: str, stage_json: str) -> EditStageResult:
    """Add a new stage to a project's workflow (validated before it writes; nothing
    written on failure). The first stage of a project starts its workflow."""
    return stage_edit.add_stage_spec(_project_to_write(name), stage_json)


def save_working_copy_as_version(
    project_dir: Path,
    *,
    message: str,
    reviewer: str,
    parent_version: str | None = None,
) -> versioning.WorkflowVersion:
    """Strict-loads first, so an invalid working copy raises WorkflowLoadError and writes
    nothing."""
    project_dir = Path(project_dir)
    if not loader.exists(project_dir.name):
        raise FileNotFoundError(
            f"Cannot create a version: project '{project_dir.name}' has no workflow"
        )
    stages = loader.load_workflow(project_dir.name)
    return versioning.create_version_from_stages(
        project_dir,
        [stage_to_spec_dict(s) for s in stages],
        message=message,
        reviewer=reviewer,
        parent_version=parent_version,
    )


def add_stages_reporting_drops(
    name: str, stages: Sequence[StageDraft]
) -> dict[str, Any]:
    """Partial success kept; warns per stored stage that echoed back server-owned fields."""
    try:
        outcome = add_stages(name, stages)
    except (WorkflowLoadError, FileNotFoundError) as exc:
        outcome = AddStagesResult(batch_issues=[str(exc)])
    result: dict[str, Any] = {
        "ok": not (outcome.failed or outcome.batch_issues),
        "added": outcome.added,
        "failed": [{"id": f.id, "issues": f.issues} for f in outcome.failed],
        "skipped": [{"id": s.id, "because": s.because} for s in outcome.skipped],
        "issues": outcome.batch_issues + [i for f in outcome.failed for i in f.issues],
    }
    warnings = _find_dropped_field_warnings(stages, outcome.added)
    if warnings:
        result["warnings"] = warnings
    return result


def _find_dropped_field_warnings(
    stages: Sequence[StageDraft], added: list[str]
) -> list[str]:
    """Only STORED stages are warned about: nothing was dropped on an unstored stage's
    behalf."""
    stored = set(added)
    named = [
        f"`{s.id}`: ignored server-owned fields: {', '.join(s.dropped_server_owned_fields)}"
        for s in stages
        if s.id in stored and s.dropped_server_owned_fields
    ]
    if not named:
        return []
    return named + [
        "only the server writes these: tests come from generate_stage_tests, "
        "review is human-only."
    ]


def add_stages(
    name: str, stages: Sequence[StageDraft]
) -> AddStagesResult:
    """Add several new stages to a project's workflow in one pass — ordered by
    their declared inputs, each validated against the whole graph, partial
    success kept. See `stage_edit.add_stage_specs`."""
    return stage_edit.add_stage_specs(_project_to_write(name), stages)


def remove_stage(name: str, stage_id: str) -> EditStageResult:
    """Delete one stage from a project's workflow (the reduced workflow is validated
    first; nothing is deleted when another stage still inputs from it)."""
    return stage_edit.remove_stage_spec(_project_to_write(name), stage_id)


def read_review_guide(name: str, version_id: str) -> ReviewGuide | None:
    """The newest guide written for one version, or None — never a stand-in."""
    # Loaded for its existence check alone: None has to mean "no guide written",
    # never "no such version", which raises FileNotFoundError from here.
    versioning.load_version(workspace.resolve_project_dir(name), version_id)
    return versioning.find_latest_review_guide(name, version_id)


def write_review_guide(
    name: str, version_id: str, draft: ReviewGuideDraft
) -> ReviewGuide:
    """Append a guide to one version; a mismatch raises and nothing is written."""
    guide = ReviewGuide(
        project=name, version_id=version_id,
        steps=draft.steps, unnarrated=draft.unnarrated,
    )
    # save_version_guide validates before writing and raises otherwise, so past this line
    # `guide` is what a reader of that version now gets.
    versioning.save_version_guide(
        workspace.resolve_project_dir(_project_to_write(name)), version_id, guide)
    return guide


def _project_to_write(name: str) -> str:
    """Raises for an unknown name: writing a stage never brings a project into being."""
    if not workspace.resolve_project_dir(name).is_dir():
        raise ValueError(f"no project '{name}' in the workspace")
    return name


# ─── Portable WorkflowFile: project export / import ──────────────────────────

class WorkflowFile(BaseModel):
    """A portable project — methodology + data model + workflow stages — as one
    pydantic-serialized document. Not review state, not input data (a run-time
    concern per #135). Serialize with `to_json`, load with `model_validate_json`."""

    name: str
    document: str
    model: str
    source: str
    data_model: SchemaLibrary
    stages: list[Stage]

    @field_validator("stages", mode="before")
    @classmethod
    def _drop_null_stage_keys(cls, v: Any) -> Any:
        """A bundle written before stages became per-type models carries every
        config block, null for the ones its type does not use (`"llm": null` on an
        input_data stage). Those keys are now unknown on the stage they land in, so
        drop them here rather than fail an import of a file already on disk. Only
        nulls: a NON-null block belonging to another type is a real error and still
        raises."""
        if not isinstance(v, list):
            return v
        return [
            {key: value for key, value in stage.items() if value is not None}
            if isinstance(stage, dict) else stage
            for stage in v
        ]

    def to_json(self) -> str:
        """Omits nulls: a stage model declares only the config blocks its own
        type carries, so a null block of some other type would be an unknown key
        on the way back in."""
        return self.model_dump_json(indent=2, exclude_none=True)


def export_project(name: str) -> WorkflowFile:
    """Read project `name`'s working copy through the loaders into a WorkflowFile —
    read-only. Raises FileNotFoundError if no such project; ValueError if it has no
    recorded model/source/document (never fabricated)."""
    pdir = workspace.resolve_project_dir(name)
    meta = project_meta(pdir)
    if meta.model is None or meta.source is None:
        raise ValueError(
            f"project '{name}' has no recorded model/source — cannot export"
        )
    document = methodology.read_methodology(name)
    if document is None:
        raise ValueError(f"project '{name}' has no document — cannot export")
    library = data_model.load_data_model(name) or SchemaLibrary(schemas=[])
    stages = [e.stage for e in loader.load_stage_entries(name) if e.stage is not None]
    return WorkflowFile(
        name=name,
        document=document,
        model=meta.model,
        source=meta.source,
        data_model=library,
        stages=stages,
    )


def import_project(
    wf: WorkflowFile, *, name: str | None = None,
) -> str:
    """Write `wf` into the workspace under `name` (default: `wf.name`) through the
    existing service writers, then mint one version when it carries stages.
    Import-if-absent only: create_project's own clash check (a Project record
    already exists, or a methodology is already stored) raises
    ProjectExistsError — this function adds no clash check of its own, so a
    bare/incidental directory of the target name does not block import.
    Returns the sanitized name."""
    target = sanitize_project_name(name or wf.name)
    pdir = workspace.resolve_project_dir(target)
    create_project(target, wf.document, model=wf.model, source=wf.source)
    data_model.write_data_model(target, wf.data_model)
    if wf.stages:
        loader.save_stages(target, list(wf.stages))
        save_working_copy_as_version(
            pdir, message=f"Imported '{target}'", reviewer="import"
        )
    return target
