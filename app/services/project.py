"""
project.py — the PROJECT MODEL: one status object per project working copy.

A "project" is a directory under examples/<name>/. Over its life it accumulates,
in order: a source DOCUMENT (the pasted methodology), a DATA MODEL (named
schemas/), a WORKFLOW (the compiled/ stages), immutable VERSIONS, and RUNS. The
unified app shows ONE project at a time with five sections (Overview / Document /
Data model / Workflow / Runs); every section route and the shell that frames them
need the same status snapshot. This module computes that snapshot —
`project_state(pdir)` — so the routes layer never recomputes counts ad hoc and
never disagrees with the sidebar about what's done and what to do next.

Two functions are the public surface:
  - project_meta(pdir)  : the project's identity card (name/title/created/model/
                          source). Reads examples/<name>/project.json when present;
                          for LEGACY projects with none, degrades TRUTHFULLY — never
                          invents a model or a creation date.
  - project_state(pdir) : the status object the Overview + shell render (document /
                          data model / workflow / versions / runs). The "what to do
                          next" CTA is added on top by the web layer, not here.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.errors import ProjectExistsError
from app.services import node_review, stage_edit, versioning, workspace
from app.services.loader import load_compiled_dir, stage_to_json
from app.services.stage_edit import EditStageResult


# ─── Status models ────────────────────────────────────────────────────────────
# The typed shapes project_meta / project_state return. Every field is read off
# disk truthfully (see project_state); an unknown fact is None / 0 / a "none" state,
# never a fabricated placeholder.


class Coverage(BaseModel):
    """Approval coverage over a workflow's compiled stages (mirrors
    node_review.coverage_for): how many stages sit in each belief state, the total,
    and the approved percentage (over total; 0.0 when there are no stages)."""

    approved: int
    rejected: int
    edited_stale: int
    unreviewed: int
    total: int
    approved_pct: float


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
    latest_status).

    Mirrors loading.list_runs exactly: a run is a child dir of runs/ WITH a readable
    manifest.json; dirs lacking one (partial / legacy-output-only) are not counted,
    so n is the count of real runs, never inflated. `awaiting_review` counts runs
    whose status is 'awaiting_review' (halted at a human_review_queue) — the driver
    of the "review the run" rung of the ladder. `latest_status` is the newest run's
    status (runs are timestamp-id'd, so the max id is newest); None when there are
    no runs. A corrupt manifest is counted (status 'corrupt') rather than hidden."""
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
        except json.JSONDecodeError:
            status = "corrupt"
        statuses.append((run.name, status))
        if status == "awaiting_review":
            awaiting += 1
    if not statuses:
        return RunsSummary(n=0, awaiting_review=0, latest_status=None)
    # Newest run by id (run ids are strftime timestamps → lexical max is chronological).
    latest_status = max(statuses, key=lambda t: t[0])[1]
    return RunsSummary(n=len(statuses), awaiting_review=awaiting, latest_status=latest_status)


# ─── Project identity (meta) ──────────────────────────────────────────────────


def project_meta(pdir: Path) -> ProjectMeta:
    """The project's identity card (ProjectMeta): name / title / created_at / model /
    source.

    Reads examples/<name>/project.json when present (the record a gated-authored
    project will carry). For a LEGACY project with no project.json, degrade
    TRUTHFULLY rather than fabricate:
      - name       : the directory name (always known).
      - title      : project.json's title, else None (no invented prose title).
      - created_at : project.json's value, else None. The create flow always writes
                     it, so a None here means a legacy project that predates it —
                     reported as unknown, never an inferred date.
      - model      : project.json's value, else None — "unknown". We do NOT guess a
                     default model; a wrong provenance is worse than an honest gap.
      - source     : project.json's value, else None.

    Always returns name from the dir even if project.json is malformed, so a corrupt
    file degrades to legacy behaviour instead of raising."""
    pdir = Path(pdir)
    name = pdir.name

    raw: dict[str, Any] = {}
    pj = pdir / "project.json"
    if pj.is_file():
        try:
            loaded = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (json.JSONDecodeError, OSError):
            raw = {}

    return ProjectMeta(
        name=raw.get("name") or name,
        title=raw.get("title"),
        # project.json's value, else None — the create flow always sets it; a None is
        # an honest "unknown" for a legacy project, never an inferred date.
        created_at=raw.get("created_at"),
        # model is None ("unknown") for legacy — never a fabricated default.
        model=raw.get("model"),
        source=raw.get("source"),
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
# EXAMPLES_DIR/<name> internally, and returns in-memory objects (never a Path) via
# the loader/services — so the agent tools never build a filesystem path. The name
# comes from the model, so it is validated to stay inside the workspace.


def create_project(
    name: str,
    document: str,
    *,
    model: str = "sonnet",
    source: str,
    examples_dir: Path | None = None,
) -> str:
    """Create the examples/<name>/ working copy for a NEW project: sanitize the
    name, write document.md (the source of record) and project.json (real model +
    created_at + source — never fabricated). Returns the sanitized name. Raises
    ValueError on an empty document and ProjectExistsError on a name clash."""
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "project"
    doc = document.strip()
    if not doc:
        raise ValueError("The methodology document is empty.")
    root = Path(examples_dir) if examples_dir is not None else workspace.EXAMPLES_DIR
    project_dir = root / safe_name
    if project_dir.exists():
        raise ProjectExistsError(
            f"examples/{safe_name}/ already exists — choose a different name."
        )
    project_dir.mkdir(parents=True)
    (project_dir / "document.md").write_text(doc, encoding="utf-8")
    write_project_meta(
        project_dir,
        name=safe_name,
        title=None,
        created_at=datetime.now().isoformat(timespec="seconds"),
        model=model,
        source=source,
    )
    return safe_name


def list_projects(examples_dir: Path | None = None) -> list[str]:
    """The names of every authored project in the workspace."""
    return workspace.list_project_names(Path(examples_dir) if examples_dir is not None else workspace.EXAMPLES_DIR)


def describe_workflow(name: str, examples_dir: Path | None = None) -> dict[str, Any]:
    """A compact summary of one project's workflow (stage ids/types/inputs/review
    state), read through the tolerant loader."""
    return workspace.project_workflow_summary(workspace.resolve_project_dir(name, examples_dir))


def read_stage(name: str, stage_id: str, examples_dir: Path | None = None) -> str:
    """The canonical JSON of one stage in a project's workflow. Raises ValueError if
    the stage is not in the workflow."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    stages = {c.stage.id: c.stage
              for c in load_compiled_dir(project_dir / "compiled") if c.stage is not None}
    stage = stages.get(stage_id)
    if stage is None:
        raise ValueError(f"no stage '{stage_id}' in project '{name}'")
    return stage_to_json(stage)


def edit_stage(name: str, stage_id: str, changes_json: str, examples_dir: Path | None = None) -> EditStageResult:
    """Apply a JSON Merge Patch to one stage of a project's workflow (validated
    before it writes; nothing written on failure)."""
    return stage_edit.patch_stage_spec(workspace.resolve_project_dir(name, examples_dir), stage_id, changes_json)


def add_stage(name: str, stage_json: str, examples_dir: Path | None = None) -> EditStageResult:
    """Add a new stage to a project's workflow (validated before it writes; nothing
    written on failure)."""
    return stage_edit.add_stage_spec(workspace.resolve_project_dir(name, examples_dir), stage_json)


__all__ = [
    "Coverage",
    "DataModelStatus",
    "WorkflowStatus",
    "RunsSummary",
    "ProjectMeta",
    "ProjectState",
    "project_meta",
    "write_project_meta",
    "project_state",
    "find_document_path",
    "create_project",
    "list_projects",
    "describe_workflow",
    "read_stage",
    "edit_stage",
    "add_stage",
]
