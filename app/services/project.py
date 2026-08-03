"""The project lifecycle service. A project's identity is the Project record (see
below), not its examples/<name>/ working-copy directory — a directory may exist
without a name clash. project_meta degrades TRUTHFULLY when no record can be
found or built — it never invents a model or a creation date. import_project is
import-if-absent: a name clash raises rather than replacing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Sequence

from pydantic import BaseModel, field_validator

from app.core.errors import ProjectExistsError
from app.models import Coverage, SchemaLibrary, Stage, StageDraft
from app.models.review_guide import ReviewGuide
from app.core.persistence import PersistedModel, PersistenceScope
from app.core.run_status import RunStatus
from app.services import data_model, node_review, stage_edit, versioning, workspace
from app.services.loader import (
    load_compiled_dir,
    load_workflow,
    stage_to_json,
    stage_to_spec_dict,
    write_stage,
)
from app.services.stage_edit import AddStagesResult, EditStageResult


# ─── Project identity record ───────────────────────────────────────────────────


class Project(PersistedModel):
    """authored_at is the project's own creation date; None when unknown, never the record's."""

    collection: ClassVar[str] = "project"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    title: str | None = None
    model: str | None = None
    source: str | None = None
    authored_at: str | None = None


# ─── Status models ────────────────────────────────────────────────────────────
# The typed shapes project_meta / project_state return. Every field is read off
# disk truthfully (see project_state); an unknown fact is None / 0 / a "none" state,
# never a fabricated placeholder. Coverage itself lives in app.models — the
# shared shape versioning.WorkflowVersion also embeds.


class DataModelStatus(BaseModel):
    """state: approved | edited_stale | unreviewed, or none when there is no data model to gate."""

    present: bool
    n_schemas: int
    state: str


class WorkflowStatus(BaseModel):
    """coverage is None — not a zero object — when there is no workflow to cover."""

    present: bool
    n_stages: int
    coverage: Coverage | None


class RunsSummary(BaseModel):
    n: int
    awaiting_review: int
    latest_status: str | None


class ProjectMeta(BaseModel):
    """None means unknown (a legacy project with no record), never a fabricated stand-in."""

    name: str
    title: str | None
    created_at: str | None
    model: str | None
    source: str | None


class ProjectState(BaseModel):
    """No "what to do next" CTA here — the web layer adds it (app.web.project_view.shell_state)."""

    name: str
    meta: ProjectMeta
    has_document: bool
    document_path: str | None
    data_model: DataModelStatus
    workflow: WorkflowStatus
    versions: int
    runs: RunsSummary


# ─── Document discovery ───────────────────────────────────────────────────────
# A project's source document is the pasted methodology it was authored from. The
# create flow writes document.md; legacy/imported projects may carry
# methodology_raw.md or the older methodology_raw.txt. Probe in that order and
# report the first that exists (a truthful path, never a fabricated one).
#
# LEGACY: probing a fixed candidate list is a migration accommodation. The intended
# direction is for project.json to record the document's path explicitly, so a
# project references a real file rather than inferring it by filename — at which
# point this probe (and _DOCUMENT_CANDIDATES) can be retired.
_DOCUMENT_CANDIDATES = ("document.md", "methodology_raw.md", "methodology_raw.txt")


def find_document_path(pdir: Path) -> Path | None:
    for name in _DOCUMENT_CANDIDATES:
        p = pdir / name
        if p.is_file():
            return p
    return None


# ─── Stage loading (counts / coverage) ────────────────────────────────────────


def _load_compiled_stages(pdir: Path) -> list[dict[str, Any]]:
    """Mirrors app.services.loader's on-disk convention, so counts/coverage match the workflow page."""
    compiled_dir = pdir / "compiled"
    if not compiled_dir.is_dir():
        return []
    stages: list[dict[str, Any]] = []
    for json_file in sorted(compiled_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as exc:
            data = {
                "id": json_file.stem,
                "name": f"[JSON ERROR] {json_file.name}",
                "type": "python_transform",
                "compiler_notes": [f"JSON parse error: {exc}"],
                "_error": True,
            }
        data["_filename"] = json_file.name
        data["_order"] = json_file.stem.split("_", 1)[0]
        stages.append(data)
    return stages


# ─── Run summary ──────────────────────────────────────────────────────────────


def _runs_summary(pdir: Path) -> RunsSummary:
    """Mirrors loading.list_runs: a run is a child dir of runs/ with a readable manifest.json."""
    runs_dir = pdir / "runs"
    if not runs_dir.is_dir():
        return RunsSummary(n=0, awaiting_review=0, latest_status=None)
    statuses: list[tuple[str, str]] = []  # (run_id, status)
    awaiting = 0
    for run in runs_dir.iterdir():
        if not run.is_dir():
            continue
        manifest_path = run / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            status = manifest.get("status", "unknown")
            is_test_run = manifest.get("is_test_run", False)
        except json.JSONDecodeError:
            status = "corrupt"
            is_test_run = False
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


def write_project_meta(pdir: Path, **fields: Any) -> dict[str, Any]:
    """Merge-writes project.json: omit a key rather than pass a placeholder value for it."""
    pdir = Path(pdir)
    pdir.mkdir(parents=True, exist_ok=True)
    pj = pdir / "project.json"
    record: dict[str, Any] = {}
    if pj.is_file():
        try:
            loaded = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                record = loaded
        except (json.JSONDecodeError, OSError):
            record = {}
    record.update(fields)
    pj.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ─── The status snapshot ──────────────────────────────────────────────────────


def project_state(pdir: Path) -> ProjectState:
    pdir = Path(pdir)
    name = pdir.name
    meta = project_meta(pdir)

    # ── Document ──
    doc_path = find_document_path(pdir)
    has_document = doc_path is not None

    # ── Data model (named schemas) ──
    schemas = workspace.load_schemas(pdir)
    dm_present = bool(schemas)
    if dm_present:
        dm_state = node_review.data_model_state(pdir, schemas)["state"]
    else:
        # No data model authored yet — report the absence; do NOT run the gate over
        # an empty schema set (that would manufacture an 'unreviewed' verdict for a
        # thing that doesn't exist).
        dm_state = "none"
    data_model = DataModelStatus(present=dm_present, n_schemas=len(schemas), state=dm_state)

    # ── Workflow (compiled stages) ──
    stages = _load_compiled_stages(pdir)
    wf_present = bool(stages)
    coverage: Coverage | None
    if wf_present:
        decisions = node_review.load_node_decisions(pdir)
        coverage = Coverage.model_validate(node_review.coverage_for(stages, decisions))
    else:
        # No workflow → coverage is None (the absence), not a fabricated 0/0 object.
        coverage = None
    workflow = WorkflowStatus(present=wf_present, n_stages=len(stages), coverage=coverage)

    # ── Versions + runs ──
    n_versions = len(versioning.list_versions(pdir))
    runs = _runs_summary(pdir)

    return ProjectState(
        name=name,
        meta=meta,
        has_document=has_document,
        # Absolute path string (or None) — a link target, never fabricated.
        document_path=str(doc_path) if doc_path else None,
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
    """Pure, so a caller can compute the directory name create_project will use before calling it."""
    return re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "project"


def create_project(
    name: str,
    document: str,
    *,
    model: str = "sonnet",
    source: str,
) -> str:
    """A clash is a Project record or a document.md; a bare directory is not, and is written into."""
    safe_name = sanitize_project_name(name)
    doc = document.strip()
    if not doc:
        raise ValueError("The methodology document is empty.")
    if Project.exists(safe_name):
        raise ProjectExistsError(
            f"project '{safe_name}' already exists — choose a different name."
        )
    project_dir = workspace.projects_dir() / safe_name
    if (project_dir / "document.md").is_file():
        raise ProjectExistsError(
            f"{safe_name}/document.md already exists — choose a different name."
        )
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "document.md").write_text(doc, encoding="utf-8")
    created_at = datetime.now().isoformat(timespec="seconds")
    write_project_meta(
        project_dir, name=safe_name, title=None, created_at=created_at, model=model, source=source,
    )
    Project(id=safe_name, title=None, model=model, source=source, authored_at=created_at).save()
    return safe_name


def list_projects() -> list[str]:
    return sorted(record.id for record in Project.list())


def describe_workflow(name: str) -> dict[str, Any]:
    return workspace.project_workflow_summary(workspace.resolve_project_dir(name))


def read_stage(name: str, stage_id: str) -> str:
    project_dir = workspace.resolve_project_dir(name)
    stages = {c.stage.id: c.stage
              for c in load_compiled_dir(project_dir / "compiled") if c.stage is not None}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in project '{name}'")
    return stage_to_json(stage)


def edit_stage(name: str, stage_id: str, changes_json: str) -> EditStageResult:
    """Applies a JSON Merge Patch, validated before it writes; nothing is written on failure."""
    return stage_edit.patch_stage_spec(_resolve_project_dir_to_write(name), stage_id, changes_json)


def add_stage(name: str, stage_json: str) -> EditStageResult:
    """Validated before it writes; nothing written on failure. The first stage starts the workflow."""
    return stage_edit.add_stage_spec(_resolve_project_dir_to_write(name), stage_json)


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
    compiled_src = project_dir / "compiled"
    if not compiled_src.is_dir():
        raise FileNotFoundError(
            f"Cannot create a version: no compiled/ workflow at {compiled_src}"
        )
    stages = load_workflow(project_dir)
    return versioning.create_version_from_stages(
        project_dir,
        [stage_to_spec_dict(s) for s in stages],
        message=message,
        reviewer=reviewer,
        parent_version=parent_version,
    )


def add_stages(
    name: str, stages: Sequence[StageDraft]
) -> AddStagesResult:
    """Ordered by their declared inputs, each validated against the whole graph, partial success kept."""
    return stage_edit.add_stage_specs(_resolve_project_dir_to_write(name), stages)


def remove_stage(name: str, stage_id: str) -> EditStageResult:
    """Validated first; nothing is deleted while another stage still inputs from it."""
    return stage_edit.remove_stage_spec(_resolve_project_dir_to_write(name), stage_id)


def read_review_guide(name: str, version_id: str) -> ReviewGuide | None:
    project_dir = workspace.resolve_project_dir(name)
    return versioning.load_version(project_dir, version_id).guide


def write_review_guide(name: str, version_id: str, guide: ReviewGuide) -> ReviewGuide:
    """Store `guide` on one version, replacing any earlier one; a mismatch raises, unwritten."""
    # save_version_guide validates before writing and raises otherwise, so past this line
    # `guide` is what the version carries.
    versioning.save_version_guide(_resolve_project_dir_to_write(name), version_id, guide)
    return guide


def _resolve_project_dir_to_write(name: str) -> Path:
    """Raises for a nonexistent project: writing a stage must never bring a project into being."""
    project_dir = workspace.resolve_project_dir(name)
    if not project_dir.is_dir():
        raise ValueError(f"no project '{name}' in the workspace")
    return project_dir


# ─── Portable WorkflowFile: project export / import ──────────────────────────

class WorkflowFile(BaseModel):
    """Methodology + data model + stages only — not review state, and not input data."""

    name: str
    document: str
    model: str
    source: str
    data_model: SchemaLibrary
    stages: list[Stage]

    @field_validator("stages", mode="before")
    @classmethod
    def _drop_null_stage_keys(cls, v: Any) -> Any:
        """Legacy bundles carry null config blocks a stage's type does not use; only nulls drop."""
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


def export_project(name: str) -> WorkflowFile:
    pdir = workspace.resolve_project_dir(name)
    meta = project_meta(pdir)
    if meta.model is None or meta.source is None:
        raise ValueError(
            f"project '{name}' has no recorded model/source in project.json — cannot export"
        )
    document_path = project_state(pdir).document_path
    if document_path is None:
        raise ValueError(f"project '{name}' has no document — cannot export")
    library = data_model.load_data_model(pdir) or SchemaLibrary(schemas=[])
    stages = [c.stage for c in load_compiled_dir(pdir / "compiled") if c.stage is not None]
    return WorkflowFile(
        name=name,
        document=Path(document_path).read_text(encoding="utf-8"),
        model=meta.model,
        source=meta.source,
        data_model=library,
        stages=stages,
    )


def import_project(
    wf: WorkflowFile, *, name: str | None = None,
) -> str:
    """Import-if-absent: only create_project's clash check applies, so a bare directory never blocks."""
    target = sanitize_project_name(name or wf.name)
    pdir = workspace.resolve_project_dir(target)
    create_project(target, wf.document, model=wf.model, source=wf.source)
    data_model.write_data_model(pdir, wf.data_model)
    for i, stage in enumerate(wf.stages, start=1):
        stage_path = pdir / "compiled" / f"{i:02d}_{stage.id}.json"
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        write_stage(stage_path, stage)
    if wf.stages:
        save_working_copy_as_version(
            pdir, message=f"Imported '{target}'", reviewer="import"
        )
    return target


__all__ = [
    "Coverage",
    "save_working_copy_as_version",
    "Project",
    "DataModelStatus",
    "WorkflowStatus",
    "RunsSummary",
    "ProjectMeta",
    "ProjectState",
    "project_meta",
    "write_project_meta",
    "project_state",
    "find_document_path",
    "sanitize_project_name",
    "create_project",
    "list_projects",
    "describe_workflow",
    "read_stage",
    "edit_stage",
    "add_stage",
    "read_review_guide",
    "write_review_guide",
    "WorkflowFile",
    "export_project",
    "import_project",
]
