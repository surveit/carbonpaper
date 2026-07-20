"""Filesystem access for the web layer: read compiled stage JSON, stage
outputs, review decisions, and queue snapshots off disk, plus run manifests
from the document store, plus small pure helpers for the stage-dict shape
they return."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException

from app.core.errors import DocumentNotFound, NoVersionToRunError
from app.core.models import Stage
from app.core.models.records.workflow_run import StageRun, WorkflowRun
from app.runtime.runner import resolve_version_id
from app.services.loader import CompiledStageFile, load_compiled_dir
from app.services.versioning import list_versions, load_version_stages
from app.services.workspace import load_schemas
from app.web.config import EXAMPLES_DIR, REPO_ROOT


# ─── Projects & stages ──────────────────────────────────────────────────

def list_projects() -> list[dict[str, Any]]:
    """One project card per dir under examples/, in the shape the home dashboard
    renders. The card's headline question is binary — is the project still being
    SET UP, or is it READY TO RUN? — so alongside the authored-what flags
    (has_document / has_schemas / has_workflow) each card carries `is_ready`:
    True iff at least one version exists, because runs target versions and a
    project without one cannot be run yet. Sorted by name.

    Every flag and count is read off disk — a card never advertises a
    stage/schema/run/version that isn't there. A directory counts as a project
    from the moment creation writes its document.md (or project.json) — a
    just-created project whose data model is still being generated must show up,
    not appear only once generation finishes. A dir with none of those markers is
    not a project and is omitted. A run counts only if it has a document in the
    store's "workflow_run" collection (mirrors list_runs), so the count is real
    runs, never inflated."""
    if not EXAMPLES_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(EXAMPLES_DIR.iterdir()):
        if not p.is_dir():
            continue
        compiled_dir = p / "compiled"
        schemas_dir = p / "schemas"
        n_stages = len(list(compiled_dir.glob("*.json"))) if compiled_dir.is_dir() else 0
        has_workflow = n_stages > 0
        has_schemas = schemas_dir.is_dir() and any(schemas_dir.glob("*.json"))
        n_schemas = len(load_schemas(p)) if has_schemas else 0
        n_runs = len(WorkflowRun.list_for_project(p.name))
        has_document = (p / "document.md").is_file() or (p / "project.json").is_file()
        if not (has_workflow or has_schemas or has_document):
            continue
        out.append({
            "name": p.name,
            "has_document": has_document,
            "has_workflow": has_workflow,
            "has_schemas": has_schemas,
            "is_ready": len(list_versions(p)) > 0,
            "n_stages": n_stages,
            "n_schemas": n_schemas,
            "n_runs": n_runs,
        })
    return out


@dataclass
class StageListing:
    """Compiled stages for the viewer. All-or-nothing: if every file is valid,
    `stages` holds them and `issues` is empty; if ANY file is invalid, `stages`
    is empty and `issues` names the broken files. `order` maps stage id →
    filename order prefix (empty when there are issues)."""
    stages: list[Stage]
    issues: list[CompiledStageFile]
    order: dict[str, str]


def load_stages(project: str) -> StageListing:
    compiled_dir = EXAMPLES_DIR / project / "compiled"
    if not compiled_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No compiled stages for {project}")
    entries = load_compiled_dir(compiled_dir)
    issues = [e for e in entries if e.issues]
    if issues:
        # One invalid file breaks the whole workflow — its edges no longer
        # resolve, so the surviving stages form a workflow with holes. Rendering that
        # is "unusable but lies." Return no stages, only the issues, so the
        # viewer shows what's broken instead of a false graph.
        return StageListing(stages=[], issues=issues, order={})
    stages = [e.stage for e in entries if e.stage is not None]
    order = {e.stage.id: e.filename.split("_", 1)[0]
             for e in entries if e.stage is not None}
    return StageListing(stages=stages, issues=[], order=order)


def load_stages_or_empty(project: str) -> StageListing:
    """Like load_stages, but returns an EMPTY listing instead of 404 when the project
    has no compiled/ workflow yet. For the shell's workflow section, which renders the
    locked/empty page (not an error) for a project that has no workflow authored."""
    compiled_dir = EXAMPLES_DIR / project / "compiled"
    if not compiled_dir.is_dir():
        return StageListing(stages=[], issues=[], order={})
    return load_stages(project)


def find_stage(stages: list[Stage], stage_id: str) -> Stage | None:
    return next((s for s in stages if s.id == stage_id), None)


def list_file_inputs(project_dir: Path) -> list[dict[str, Any]]:
    """File-kind input stages of the version a triggered run will execute
    (resolve_version_id's choice), each with its workflow-authored absolute
    path ('' when the stage authors none — the run form must collect one).
    [] when the project has no versions yet."""
    try:
        version_id = resolve_version_id(project_dir, None)
    except NoVersionToRunError:
        return []
    stages = load_version_stages(project_dir, version_id)
    return [
        {"stage_id": s.id, "name": s.name,
         "path": str((s.connector.params or {}).get("path") or "")}
        for s in stages
        if s.type == "input_data" and s.connector is not None
        and s.connector.kind == "file"
    ]


# ─── Source & code reads ─────────────────────────────────────────────────────

def read_module_code(module_path: str) -> str | None:
    """Resolve module 'examples.lobbymap.code.foo' to a file path and read it."""
    if not module_path:
        return None
    parts = module_path.split(".")
    candidate = REPO_ROOT / Path(*parts).with_suffix(".py")
    if not candidate.exists():
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return None


def resolve_function_code(stage_def: Stage | None) -> str | None:
    """Python source for a stage's function handle: the module file for a module
    ref, or the inline code string. None if the stage has neither."""
    fn = stage_def.function if stage_def else None
    if fn and fn.kind == "module" and fn.module:
        return read_module_code(fn.module)
    if fn and fn.kind == "inline":
        return fn.code
    return None


# ─── Runs & manifests ────────────────────────────────────────────────────────

def runs_dir(project: str) -> Path:
    return EXAMPLES_DIR / project / "runs"


def load_manifest(run_dir: Path) -> WorkflowRun:
    """A run's WorkflowRun record, or 404 if the run doesn't exist. The
    manifest lives in the document store, not on disk; `project`/`run_id` are
    derived from `run_dir`'s layout (`<project_dir>/runs/<run_id>` — the
    stable convention every run dir follows)."""
    project = run_dir.parent.parent.name
    try:
        return WorkflowRun.load(f"{project}/{run_dir.name}")
    except DocumentNotFound:
        raise HTTPException(status_code=404, detail="Run not found") from None


def list_runs(project: str) -> list[WorkflowRun]:
    """Every run for this project, newest-first. No runs stored yet -> []."""
    return WorkflowRun.list_for_project(project)


# ─── Tabular output previews ─────────────────────────────────────────────────

# Hard cap on rows rendered in the full-table view of a stage output. The CSV
# download endpoint has no cap — it always serves the complete file.
MAX_TABLE_ROWS = 5000


def read_table(path: Path) -> pd.DataFrame:
    """Read a stage output file (parquet or csv) into a DataFrame."""
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)


def manifest_stage(run_dir: Path, stage_id: str) -> StageRun:
    """The stage record for one stage of a run; 404 if run or stage missing."""
    manifest = load_manifest(run_dir)
    stage_record = next((s for s in manifest.stages if s.stage_id == stage_id), None)
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")
    return stage_record


def read_output_df(run_dir: Path, rel_path: str | None) -> pd.DataFrame:
    """A stage output file as a DataFrame. 404 if the stage has no output, the
    path escapes the run directory, or the file is missing on disk."""
    if not rel_path:
        raise HTTPException(status_code=404, detail="Stage has no output file")
    path = (run_dir / rel_path).resolve()
    if not str(path).startswith(str(run_dir.resolve())) or not path.exists():
        raise HTTPException(
            status_code=404, detail=f"Output file missing on disk: {rel_path}"
        )
    try:
        return read_table(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Could not read output file: {exc}"
        ) from exc


def load_output_table(run_dir: Path, rel_path: str | None) -> dict[str, Any]:
    """Full (capped) table of a stage output: columns, total row count, up to
    MAX_TABLE_ROWS rows as strings, and whether the render was capped."""
    df = read_output_df(run_dir, rel_path)
    rows = df.head(MAX_TABLE_ROWS).fillna("").astype(str).to_dict(orient="records")
    return {
        "columns": list(df.columns),
        "rows": rows,
        "rows_total": len(df),
        "capped": len(df) > len(rows),
    }


def load_output_row(run_dir: Path, rel_path: str | None, row: int) -> dict[str, Any] | None:
    """Preview shape (columns, rows_total, preview) holding just row `row` of a
    stage output — the row-scoped variant of `load_output_preview`, used by the
    lineage-trimmed stage panel. None if no path; {"error": ...} if unreadable;
    an empty `preview` with `out_of_range` when the ordinal is past the end."""
    if not rel_path:
        return None
    path = run_dir / rel_path
    if not path.exists():
        return {"error": f"missing on disk: {rel_path}"}
    try:
        df = read_table(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if row < 0 or row >= len(df):
        return {"columns": list(df.columns), "rows_total": len(df),
                "preview": [], "row_index": row, "out_of_range": True}
    return {
        "columns": list(df.columns),
        "rows_total": len(df),
        "preview": df.iloc[[row]].fillna("").astype(str).to_dict(orient="records"),
        "row_index": row,
    }


def load_output_preview(run_dir: Path, rel_path: str | None) -> dict[str, Any] | None:
    """Small JSON-able preview of a stage output: columns, total row count, and
    the first 5 rows as strings. None if no path is given; {"error": ...} if the
    file is missing on disk or can't be read."""
    if not rel_path:
        return None
    path = run_dir / rel_path
    if not path.exists():
        return {"error": f"missing on disk: {rel_path}"}
    try:
        df = read_table(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {
        "columns": list(df.columns),
        "rows_total": len(df),
        "preview": df.head(5).fillna("").astype(str).to_dict(orient="records"),
    }


# ─── Review decisions & queue snapshots ──────────────────────────────────────

def decisions_path(project: str, stage_id: str) -> Path:
    d = EXAMPLES_DIR / project / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stage_id}.parquet"


def load_decisions_df(project: str, stage_id: str) -> pd.DataFrame:
    p = decisions_path(project, stage_id)
    if not p.exists():
        return pd.DataFrame(
            columns=["content_hash", "decision", "modified_score",
                     "reviewer", "reviewed_at", "source_run_id"]
        )
    return pd.read_parquet(p)


def queue_snapshot(project: str, run_id: str, stage_id: str) -> pd.DataFrame | None:
    run_dir = runs_dir(project) / run_id
    for ext in (".parquet", ".csv"):
        p = run_dir / "queue" / f"{stage_id}{ext}"
        if p.exists():
            return read_table(p)
    return None


def display_cell(v: Any) -> Any:
    """Scalar-safe cell formatting for the reviewer UI. pd.isna() raises on
    list/array-valued cells (e.g. an evidence_urls JSON column), so handle
    array-likes explicitly before the null check."""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if len(v) else ""
    if hasattr(v, "tolist") and not isinstance(v, str):  # numpy array from parquet
        seq = v.tolist()
        return ", ".join(str(x) for x in seq) if len(seq) else ""
    try:
        return "" if pd.isna(v) else v
    except (ValueError, TypeError):
        return v


# ─── LLM prompt example ──────────────────────────────────────────────────────

def build_llm_example(
    stage_def: Stage | None, input_previews: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Render the prompt_template with the first row of the first usable input.

    Returns {rendered, source_id} on success, {error} if no input or render
    fails, or None if the stage isn't an LLM stage.
    """
    template = stage_def.llm.prompt_template if stage_def and stage_def.llm else None
    if not template:
        return None
    for ip in input_previews:
        preview = ip.get("preview") or {}
        rows = preview.get("preview") or []
        if not rows:
            continue
        try:
            rendered = template.format(**rows[0])
        except (KeyError, IndexError, ValueError) as exc:
            return {
                "source_id": ip["id"],
                "error": f"could not render template: {type(exc).__name__}: {exc}",
            }
        return {"source_id": ip["id"], "rendered": rendered}
    return {"error": "no input rows available in this run to render an example"}
