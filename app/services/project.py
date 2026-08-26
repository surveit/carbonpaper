"""The project lifecycle service. A project IS its id, which is also the name of its
directory under the projects root; `name` is a label two projects may share, so
nothing is ever refused for repeating one. project_meta degrades TRUTHFULLY when no
record is found — it never invents a model, a creation date, or a label.
"""

from __future__ import annotations

import shutil
import re
from datetime import datetime
from typing import Any, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.timestamp_ids import mint_timestamp_id
from app.models import (
    SchemaLibrary,
    Stage,
    StageDraft,
    StageEdit,
    Terms,
    Verb,
    stage_to_json,
    stage_to_spec_dict,
    validate_one_meaning_per_word,
)
from app.models.records.eval_config import EvalConfig
from app.models.review_guide import ReviewGuideDraft
from app.models.run_manifest import (
    records_a_test_run,
)
from app.models.records.project import Project as Project
from app.models.records.review_guide import ReviewGuide
from app.core.run_status import RunStatus
from app.services import stage_edit, terms, versioning, workspace
from app.services import loader
from app.services import methodology
from app.services import run as run_service
from app.services.errors import WorkflowLoadError
from app.services.project_record import read_project_name as read_project_name
from app.services.stage_edit import AddStagesResult, EditStageResult


# ─── Project identity record ──────────────────────────────────────────────────


def mint_project_id() -> str:
    return mint_timestamp_id()


def find_projects_by_name(name: str) -> list[Project]:
    """Plural because a label is not unique — reads every record, so never call it in a loop."""
    return [record for record in Project.list() if record.label() == name]


def has_document(project_id: str) -> bool:
    return methodology.exists(project_id)


# ─── Status models ────────────────────────────────────────────────────────────
# The typed shapes project_meta / project_state return. Every field is read off
# disk truthfully (see project_state); an unknown fact is None / 0, never a
# fabricated placeholder.


class DataModelStatus(BaseModel):
    present: bool
    n_schemas: int


class WorkflowStatus(BaseModel):
    present: bool
    n_stages: int


class RunsSummary(BaseModel):
    n: int
    awaiting_review: int
    latest_status: str | None


class ProjectListing(BaseModel):
    """What a tool hands back: `id` is what every other call takes, `name` is only shown."""

    id: str
    name: str


class ProjectMeta(BaseModel):
    """`name` is the slug a bundle exports under; `display_name` is what a screen shows."""

    name: str
    display_name: str
    title: str | None
    created_at: str | None
    model: str | None
    source: str | None
    private: bool


class ProjectState(BaseModel):
    """`id` addresses the project and every link is built from it; SHOW `meta.name`."""

    id: str
    meta: ProjectMeta
    has_document: bool
    data_model: DataModelStatus
    workflow: WorkflowStatus
    versions: int
    runs: RunsSummary


# ─── Stage loading (counts / coverage) ────────────────────────────────────────


def load_stage_specs(project_id: str) -> list[dict[str, Any]]:
    """Raw specs, valid or not — the draft graph the workflow page falls back to."""
    return loader.read_stage_specs(project_id)


# ─── Run summary ──────────────────────────────────────────────────────────────


def _runs_summary(project_id: str) -> RunsSummary:
    statuses: list[tuple[str, str]] = []  # (run_id, status)
    awaiting = 0
    for entry in run_service.list_run_entries(project_id):
        if entry.raw is None:
            # Counted, not hidden: a record this reader cannot read at all carries
            # no `is_test_run` to exclude it by, so it is treated as non-test, the
            # same as every run was before that field existed.
            status, is_test_run = "corrupt", False
        else:
            # Off the RAW payload, so a run written before a field was renamed
            # still reports the status it actually reached.
            status = entry.raw.get("status", "unknown")
            is_test_run = records_a_test_run(entry.raw)
        if is_test_run:
            continue
        statuses.append((entry.run_id, status))
        if status == RunStatus.AWAITING_REVIEW:
            awaiting += 1
    if not statuses:
        return RunsSummary(n=0, awaiting_review=0, latest_status=None)
    # Newest run by id (run ids are strftime timestamps → lexical max is chronological).
    latest_status = max(statuses, key=lambda t: t[0])[1]
    return RunsSummary(n=len(statuses), awaiting_review=awaiting, latest_status=latest_status)


# ─── Project identity (meta) ──────────────────────────────────────────────────


def project_meta(project_id: str) -> ProjectMeta:
    # A project created before ids were minted reads as a slug of its title.
    record = Project.load_or_none(project_id)
    if record is None:
        # No record: the id is the only name this project has, and it is not invented.
        return ProjectMeta(name=project_id, display_name=project_id, title=None,
                           created_at=None, model=None, source=None, private=False)
    return ProjectMeta(
        name=record.label(),
        display_name=record.display_name(),
        title=record.title,
        created_at=record.authored_at,
        model=record.model,
        source=record.source,
        private=record.private,
    )


# ─── The status snapshot ──────────────────────────────────────────────────────


def project_state(project_id: str) -> ProjectState:
    meta = project_meta(project_id)

    # ── Document ──

    # ── Data model (the noun half of the project's terms) ──
    n_nouns = terms.count_nouns(project_id)
    data_model = DataModelStatus(present=bool(n_nouns), n_schemas=n_nouns)

    # ── Workflow (compiled stages) ──
    stages = load_stage_specs(project_id)
    workflow = WorkflowStatus(present=bool(stages), n_stages=len(stages))

    # ── Versions + runs ──
    n_versions = len(versioning.list_versions(project_id))
    runs = _runs_summary(project_id)

    return ProjectState(
        id=project_id,
        meta=meta,
        has_document=has_document(project_id),
        # Absolute path string (or None) — a link target, never fabricated.
        data_model=data_model,
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
    return re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "project"


def create_project(
    name: str,
    document: str,
    *,
    model: str = "sonnet",
    source: str,
) -> Project:
    """The record's `id` is the project, and it is not the name: a name is a label and may repeat."""
    label = sanitize_project_name(name)
    doc = document.strip()
    if not doc:
        raise ValueError("The methodology document is empty.")
    # The id is minted, so it cannot collide and there is nothing to refuse. A second
    # project called `venezuela_lda_lobbying` is a legitimate thing to want — the
    # first one's directory, versions and cached rows are addressed by id, not by
    # what either of them is called.
    project_id = mint_project_id()
    workspace.resolve_project_dir(project_id).mkdir(parents=True, exist_ok=True)
    methodology.write_methodology(project_id, doc)
    created_at = datetime.now().isoformat(timespec="seconds")
    record = Project(
        id=project_id, name=label, title=None,
        model=model, source=source, authored_at=created_at,
    )
    record.save()
    return record


def delete_project(project_id: str) -> None:
    """The working copy only — store documents survive a re-created project."""
    shutil.rmtree(workspace.resolve_project_dir(project_id), ignore_errors=True)


def project_exists(project_id: str) -> bool:
    try:
        return workspace.resolve_project_dir(project_id).is_dir()
    except ValueError:
        return False


def list_projects() -> list[str]:
    """Ids, not names — a name identifies nothing, and two projects may share one."""
    return [listing.id for listing in list_project_listings()]


def list_project_listings() -> list[ProjectListing]:
    return [
        ProjectListing(id=record.id, name=record.display_name())
        for record in list_visible_projects()
    ]


def list_visible_projects() -> list[Project]:
    """Every project a reader may see, and the only place `private` is applied."""
    return [
        record
        # delete_project keeps the record and drops the working copy; project_exists sees that.
        for record in sorted(Project.list(), key=lambda r: r.id)
        if not record.private and project_exists(record.id)
    ]


def set_project_private(project_id: str, private: bool) -> None:
    record = Project.load(project_id)
    record.private = private
    record.save()


def read_workflow_summary(name: str) -> workspace.WorkflowSummary:
    return workspace.project_workflow_summary(workspace.validate_project_id(name))


def read_stage(name: str, stage_id: str) -> str:
    workspace.validate_project_id(name)
    stage = loader.find_parsed_stage(loader.load_stage_entries(name), stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in project '{name}'")
    return stage_to_json(stage)


def edit_stages(name: str, edits: Sequence[StageEdit]) -> EditStageResult:
    return stage_edit.patch_stage_specs(_project_to_write(name), edits)


def add_stage(name: str, stage_json: str) -> EditStageResult:
    return stage_edit.add_stage_spec(_project_to_write(name), stage_json)


def save_working_copy_as_version(
    project_id: str,
    *,
    message: str,
    reviewer: str,
    parent_version: str | None = None,
) -> versioning.WorkflowVersion:
    """Strict-loads the working copy, so an invalid one raises and no version is written."""
    if not loader.exists(project_id):
        raise FileNotFoundError(
            f"Cannot create a version: project '{project_id}' has no workflow"
        )
    stages = loader.load_workflow(project_id)
    return versioning.create_version_from_stages(
        project_id,
        [stage_to_spec_dict(s) for s in stages],
        message=message,
        reviewer=reviewer,
        parent_version=parent_version,
    )


def add_stages_reporting_outcome(
    name: str, stages: Sequence[StageDraft]
) -> dict[str, Any]:
    try:
        outcome = add_stages(name, stages)
    except (WorkflowLoadError, FileNotFoundError) as exc:
        outcome = AddStagesResult(batch_issues=[str(exc)])
    return {
        "ok": not (outcome.failed or outcome.batch_issues),
        "added": outcome.added,
        "failed": [{"id": f.id, "issues": f.issues} for f in outcome.failed],
        "skipped": [{"id": s.id, "because": s.because} for s in outcome.skipped],
        "issues": outcome.batch_issues + [i for f in outcome.failed for i in f.issues],
    }


def add_stages(
    name: str, stages: Sequence[StageDraft]
) -> AddStagesResult:
    return stage_edit.add_stage_specs(_project_to_write(name), stages)


def delete_stage(name: str, stage_id: str) -> EditStageResult:
    return stage_edit.delete_stage_spec(_project_to_write(name), stage_id)


def read_review_guide(name: str, version_id: str) -> ReviewGuide | None:
    """Loads the version for its existence check alone, so None means "no guide", not "no version"."""
    versioning.load_version(name, version_id)
    return versioning.find_latest_review_guide(name, version_id)


def write_review_guide(
    name: str, version_id: str, draft: ReviewGuideDraft
) -> ReviewGuide:
    guide = ReviewGuide(
        project=name, version_id=version_id,
        steps=draft.steps, unnarrated=draft.unnarrated,
    )
    versioning.save_version_guide(_project_to_write(name), version_id, guide)
    return guide


# What PersistedModel adds; a caller hands in the eval's own fields alone.
_STORE_FIELDS = {"id", "created_at", "updated_at"}


def write_eval_config(name: str, config: EvalConfig) -> None:
    """Writes what app.evals.store reads back; that package is a leaf app.tools cannot import."""
    EvalConfig(
        id=EvalConfig.compose_id(name, config.eval_id),
        **config.model_dump(exclude=_STORE_FIELDS),
    ).save()


def _project_to_write(name: str) -> str:
    """Raises for an unknown name: writing a stage never brings a project into being."""
    if not workspace.resolve_project_dir(name).is_dir():
        raise ValueError(f"no project '{name}' in the workspace")
    return name


# ─── Portable WorkflowFile: project export / import ──────────────────────────

class WorkflowFile(BaseModel):
    name: str
    document: str
    model: str
    source: str
    data_model: SchemaLibrary
    # The two halves ride as separate fields, not as one `Terms`: a bundle written
    # before verbs existed carries no key for them, and defaulting is what lets it in.
    verbs: list[Verb] = Field(default_factory=list)
    stages: list[Stage]

    @model_validator(mode="after")
    def _one_meaning_per_word(self) -> "WorkflowFile":
        validate_one_meaning_per_word(self.data_model, self.verbs)
        return self

    @field_validator("stages", mode="before")
    @classmethod
    def _drop_null_stage_keys(cls, v: Any) -> Any:
        """Old bundles carry null config blocks the stage's type does not define; non-null raises."""
        if not isinstance(v, list):
            return v
        return [
            {key: value for key, value in stage.items() if value is not None}
            if isinstance(stage, dict) else stage
            for stage in v
        ]

    def to_json(self) -> str:
        """Omits nulls: a null config block of another type is an unknown key on the way back in."""
        return self.model_dump_json(indent=2, exclude_none=True)


def export_project(project_id: str) -> WorkflowFile:
    """The bundle carries the LABEL, not the id: importing it elsewhere mints a fresh id."""
    meta = project_meta(project_id)
    if meta.model is None or meta.source is None:
        raise ValueError(
            f"project '{project_id}' has no recorded model/source — cannot export"
        )
    document = methodology.read_methodology(project_id)
    if document is None:
        raise ValueError(f"project '{project_id}' has no document — cannot export")
    project_terms = terms.load_terms(project_id)
    stages = loader.list_parsed_stages(loader.load_stage_entries(project_id))
    return WorkflowFile(
        name=meta.name,
        document=document,
        model=meta.model,
        source=meta.source,
        data_model=project_terms.nouns,
        verbs=project_terms.verbs,
        stages=stages,
    )


def import_project(
    wf: WorkflowFile, *, name: str | None = None,
) -> str:
    """Returns the project ID. Importing the same bundle twice makes two projects, not a clash."""
    label = sanitize_project_name(name or wf.name)
    project_id = create_project(label, wf.document, model=wf.model, source=wf.source).id
    terms.write_terms(project_id, Terms(nouns=wf.data_model, verbs=wf.verbs))
    if wf.stages:
        loader.save_stages(project_id, list(wf.stages))
        save_working_copy_as_version(
            project_id, message=f"Imported '{label}'", reviewer="import"
        )
    return project_id
