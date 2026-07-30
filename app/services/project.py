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
from app.core.persistence import PersistedModel, PersistenceScope
from app.core.run_status import RunStatus
from app.services import data_model, node_review, stage_edit, versioning, workspace
from app.services.loader import load_compiled_dir, stage_to_json, write_stage
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
    is genuinely unknown (a legacy project with no project.json to read it
    from) — never inferred from the record's own `created_at`."""

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
    """The project's data-model (named-schema) status: whether any schemas exist,
    how many, and the library's approval state — 'approved' | 'edited_stale' |
    'unreviewed', or 'none' when there is no data model to gate."""

    present: bool
    n_schemas: int
    state: str


class WorkflowStatus(BaseModel):
    """The project's compiled-workflow status: whether a workflow exists, how many
    stages, and its approval coverage — None (not a zero object) when there is no
    workflow to cover."""

    present: bool
    n_stages: int
    coverage: Coverage | None


class RunsSummary(BaseModel):
    """Summary of the project's runs/ dir: the count of manifest-backed runs, how
    many are halted awaiting review, and the newest run's status (None when no runs)."""

    n: int
    awaiting_review: int
    latest_status: str | None


class ProjectMeta(BaseModel):
    """A project's identity card. Legacy projects (no project.json) degrade
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
    """First existing source-document file in <pdir> (see _DOCUMENT_CANDIDATES),
    or None when the project has no document on disk. Returns the absolute Path so
    callers can read or link it; None is a truthful 'no document', not an error."""
    for name in _DOCUMENT_CANDIDATES:
        p = pdir / name
        if p.is_file():
            return p
    return None


# ─── Stage loading (counts / coverage) ────────────────────────────────────────


def _load_compiled_stages(pdir: Path) -> list[dict[str, Any]]:
    """Load the working copy's compiled/ stages as raw dicts, mirroring
    app.services.loader's on-disk convention (sorted glob of compiled/*.json,
    inject _filename/_order, surface a parse error as an _error stage rather than
    dropping it). Returns [] when there is no compiled/ workflow yet. Used for
    counting stages and computing approval coverage — so the count and coverage
    here match exactly what the workflow page loads."""
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
    """Summarise the project's runs/ dir into a RunsSummary (n / awaiting_review /
    latest_status) — NON-TEST runs only, so a workflow test (a real run under the
    same runs/ dir, but marked `is_test_run: true` — see RunManifest.is_test_run)
    never counts as, or masquerades as, the project's latest production run.

    Mirrors loading.list_runs exactly: a run is a child dir of runs/ WITH a readable
    manifest.json; dirs lacking one (partial / legacy-output-only) are not counted,
    so n is the count of real non-test runs, never inflated. `awaiting_review`
    counts non-test runs whose status is 'awaiting_review' (halted at a
    human_review_queue) — the driver of the "review the run" rung of the ladder.
    `latest_status` is the newest non-test run's status (runs are timestamp-id'd,
    so the max id is newest); None when there are no non-test runs. A corrupt
    manifest is counted (status 'corrupt') rather than hidden — a manifest this
    reader cannot parse carries no `is_test_run` to exclude it by, so it is
    treated as non-test, same as before this field existed."""
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


def write_project_meta(pdir: Path, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into examples/<name>/project.json and persist it, returning the
    written record. Reads any existing file first so a partial update doesn't drop
    other keys; only the keys passed are overwritten. None values ARE written when
    passed explicitly (so a caller can clear a field), but callers should omit a key
    rather than pass a placeholder — this module never invents a model/date itself."""
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
    """The single status object (ProjectState) the Overview page and the shell sidebar
    render.

    Fields:
      {
        name, meta,
        has_document, document_path,
        data_model: {present, n_schemas, state},
        workflow:   {present, n_stages, coverage|None},
        versions:   n,
        runs:       {n, awaiting_review, latest_status},
      }

    Every field is read off disk truthfully:
      - data_model.state uses node_review.data_model_state over the LIVE schemas
        (approved | edited_stale | unreviewed); 'none' when there is no data model
        (we report the absence, we do not run the gate on an empty list).
      - workflow.coverage is node_review.coverage_for over the COMPILED stages
        (approved/total/…); None — not a zero object — when there is no workflow.
      - runs comes from _runs_summary (manifest-backed runs only).

    The shell's "what to do next" CTA is NOT here: its label + section href are a
    UI/routing concern the web layer adds (app.web.project_view.shell_state).
    """
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
    """Create the examples/<name>/ working copy for a NEW project: sanitize the
    name, write document.md (the source of record) and project.json (real model +
    created_at + source — never fabricated), and record the project's identity
    (a Project) in the store. Returns the sanitized name.

    Raises ValueError on an empty document. Raises ProjectExistsError on a
    name clash — but a name clash means a Project record already exists for
    `safe_name`, NOT that examples/<name>/ happens to exist as a directory: an
    unrelated or empty directory (e.g. input files a user staged there by
    hand) is not a clash and is written into. examples/<name>/document.md
    existing IS a clash (it's a project's actual content) and is refused with
    a distinguishable message, so a caller can tell the two refusals apart."""
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
    """The names of every project in the workspace. The source of truth is the
    Project store — never directory existence, and never a filesystem scan."""
    return sorted(record.id for record in Project.list())


def describe_workflow(name: str) -> dict[str, Any]:
    """A compact summary of one project's workflow (stage ids/types/inputs/review
    state), read through the tolerant loader."""
    return workspace.project_workflow_summary(workspace.resolve_project_dir(name))


def read_stage(name: str, stage_id: str) -> str:
    """The on-disk JSON text of one stage in a project's workflow. Raises ValueError if
    the stage is not in the workflow."""
    project_dir = workspace.resolve_project_dir(name)
    stages = {c.stage.id: c.stage
              for c in load_compiled_dir(project_dir / "compiled") if c.stage is not None}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in project '{name}'")
    return stage_to_json(stage)


def edit_stage(name: str, stage_id: str, changes_json: str) -> EditStageResult:
    """Apply a JSON Merge Patch to one stage of a project's workflow (validated
    before it writes; nothing written on failure)."""
    return stage_edit.patch_stage_spec(_resolve_project_dir_to_write(name), stage_id, changes_json)


def add_stage(name: str, stage_json: str) -> EditStageResult:
    """Add a new stage to a project's workflow (validated before it writes; nothing
    written on failure). The first stage of a project starts its workflow."""
    return stage_edit.add_stage_spec(_resolve_project_dir_to_write(name), stage_json)


def add_stages(
    name: str, stages: Sequence[StageDraft]
) -> AddStagesResult:
    """Add several new stages to a project's workflow in one pass — ordered by
    their declared inputs, each validated against the whole graph, partial
    success kept. See `stage_edit.add_stage_specs`."""
    return stage_edit.add_stage_specs(_resolve_project_dir_to_write(name), stages)


def remove_stage(name: str, stage_id: str) -> EditStageResult:
    """Delete one stage from a project's workflow (the reduced workflow is validated
    first; nothing is deleted when another stage still inputs from it)."""
    return stage_edit.remove_stage_spec(_resolve_project_dir_to_write(name), stage_id)


def _resolve_project_dir_to_write(name: str) -> Path:
    """The directory of an EXISTING project, for the stage writers. A name with no
    project directory raises: writing a stage must never bring a project into being,
    now that the first stage creates the workflow's compiled/ dir."""
    project_dir = workspace.resolve_project_dir(name)
    if not project_dir.is_dir():
        raise ValueError(f"no project '{name}' in the workspace")
    return project_dir


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
    """Write `wf` into the workspace under `name` (default: `wf.name`) through the
    existing service writers, then mint one version when it carries stages.
    Import-if-absent only: create_project's own clash check (a Project record
    already exists, or examples/<name>/document.md already exists) raises
    ProjectExistsError — this function adds no clash check of its own, so a
    bare/incidental directory of the target name does not block import.
    Returns the sanitized name."""
    target = sanitize_project_name(name or wf.name)
    pdir = workspace.resolve_project_dir(target)
    create_project(target, wf.document, model=wf.model, source=wf.source)
    data_model.write_data_model(pdir, wf.data_model)
    for i, stage in enumerate(wf.stages, start=1):
        stage_path = pdir / "compiled" / f"{i:02d}_{stage.id}.json"
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        write_stage(stage_path, stage)
    if wf.stages:
        versioning.create_version_from_disk(pdir, message=f"Imported '{target}'", reviewer="import")
    return target


__all__ = [
    "Coverage",
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
    "WorkflowFile",
    "export_project",
    "import_project",
]
