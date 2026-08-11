"""The project lifecycle service. A project's identity is the Project record's minted
id (app.services.project_record), never its name or its examples/<name>/ directory —
a directory may exist without a name clash. project_meta degrades TRUTHFULLY when no
record can be found or built — it never invents a model or a creation date.
import_project is import-if-absent: a name clash raises rather than replacing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

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
from app.core.run_status import RunStatus
from app.services import data_model, stage_edit, versioning, workspace
from app.services.project_record import (
    Project as Project,
    find_project_by_name,
    mint_project_id,
)
from app.services.loader import (
    load_compiled_dir,
    load_workflow,
    write_stage,
)
from app.services.errors import WorkflowLoadError
from app.services.stage_edit import AddStagesResult, EditStageResult


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


class ProjectMeta(BaseModel):
    name: str
    title: str | None
    created_at: str | None
    model: str | None
    source: str | None


class ProjectState(BaseModel):
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
    pdir = Path(pdir)
    name = pdir.name
    record = find_project_by_name(name)
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
    data_model = DataModelStatus(present=bool(schemas), n_schemas=len(schemas))

    # ── Workflow (compiled stages) ──
    stages = _load_compiled_stages(pdir)
    workflow = WorkflowStatus(present=bool(stages), n_stages=len(stages))

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
    return re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "project"


def create_project(
    name: str,
    document: str,
    *,
    model: str = "sonnet",
    source: str,
) -> str:
    """A bare directory of the same name is not a clash — it is written into."""
    safe_name = sanitize_project_name(name)
    doc = document.strip()
    if not doc:
        raise ValueError("The methodology document is empty.")
    if find_project_by_name(safe_name) is not None:
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
    Project(
        id=mint_project_id(), name=safe_name, title=None,
        model=model, source=source, authored_at=created_at,
    ).save()
    return safe_name


def project_exists(project_id: str) -> bool:
    try:
        return workspace.resolve_project_dir(project_id).is_dir()
    except ValueError:
        return False


def list_projects() -> list[str]:
    return sorted(record.name for record in Project.list())


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
    return stage_edit.patch_stage_spec(_resolve_project_dir_to_write(name), stage_id, changes_json)


def add_stage(name: str, stage_json: str) -> EditStageResult:
    return stage_edit.add_stage_spec(_resolve_project_dir_to_write(name), stage_json)


def save_working_copy_as_version(
    project_dir: Path,
    *,
    message: str,
    reviewer: str,
    parent_version: str | None = None,
) -> versioning.WorkflowVersion:
    """Strict-loads the working copy, so an invalid one raises and no version is written."""
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


def add_stages_reporting_drops(
    name: str, stages: Sequence[StageDraft]
) -> dict[str, Any]:
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
    return stage_edit.add_stage_specs(_resolve_project_dir_to_write(name), stages)


def remove_stage(name: str, stage_id: str) -> EditStageResult:
    return stage_edit.remove_stage_spec(_resolve_project_dir_to_write(name), stage_id)


def read_review_guide(name: str, version_id: str) -> ReviewGuide | None:
    """Loads the version for its existence check alone, so None means "no guide", not "no version"."""
    versioning.load_version(workspace.resolve_project_dir(name), version_id)
    return versioning.find_latest_review_guide(name, version_id)


def write_review_guide(
    name: str, version_id: str, draft: ReviewGuideDraft
) -> ReviewGuide:
    guide = ReviewGuide(
        project=name, version_id=version_id,
        steps=draft.steps, unnarrated=draft.unnarrated,
    )
    versioning.save_version_guide(_resolve_project_dir_to_write(name), version_id, guide)
    return guide


def _resolve_project_dir_to_write(name: str) -> Path:
    project_dir = workspace.resolve_project_dir(name)
    if not project_dir.is_dir():
        raise ValueError(f"no project '{name}' in the workspace")
    return project_dir


# ─── Portable WorkflowFile: project export / import ──────────────────────────

class WorkflowFile(BaseModel):
    name: str
    document: str
    model: str
    source: str
    data_model: SchemaLibrary
    stages: list[Stage]

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
