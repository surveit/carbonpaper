"""Filesystem access for the web layer: read compiled stage JSON, run
manifests, stage outputs, review decisions, and queue snapshots off disk, plus
small pure helpers for the stage-dict shape they return."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException

from app.models import Stage
from app.services.loader import CompiledStageFile, load_compiled_dir
from app.web.config import EXAMPLES_DIR, REPO_ROOT


# ─── Methodologies & stages ──────────────────────────────────────────────────

def list_methodologies() -> list[str]:
    if not EXAMPLES_DIR.exists():
        return []
    return [
        p.name
        for p in sorted(EXAMPLES_DIR.iterdir())
        if p.is_dir() and (p / "compiled").is_dir()
    ]


@dataclass
class StageListing:
    """Compiled stages for the viewer. All-or-nothing: if every file is valid,
    `stages` holds them and `issues` is empty; if ANY file is invalid, `stages`
    is empty and `issues` names the broken files. `order` maps stage id →
    filename order prefix (empty when there are issues)."""
    stages: list[Stage]
    issues: list[CompiledStageFile]
    order: dict[str, str]


def load_stages(methodology: str) -> StageListing:
    compiled_dir = EXAMPLES_DIR / methodology / "compiled"
    if not compiled_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No compiled stages for {methodology}")
    entries = load_compiled_dir(compiled_dir)
    issues = [e for e in entries if e.issues]
    if issues:
        # One invalid file breaks the whole workflow — its edges no longer
        # resolve, so the surviving stages form a DAG with holes. Rendering that
        # is "unusable but lies." Return no stages, only the issues, so the
        # viewer shows what's broken instead of a false graph.
        return StageListing(stages=[], issues=issues, order={})
    stages = [e.stage for e in entries if e.stage is not None]
    order = {e.stage.id: e.filename.split("_", 1)[0]
             for e in entries if e.stage is not None}
    return StageListing(stages=stages, issues=[], order=order)


def find_stage(stages: list[Stage], stage_id: str) -> Stage | None:
    return next((s for s in stages if s.id == stage_id), None)


# ─── Source & code reads ─────────────────────────────────────────────────────

def read_prose_excerpt(stage: Stage, methodology: str) -> str | None:
    doc = stage.source.doc if stage.source else None
    if not doc:
        return None
    candidate = REPO_ROOT / doc
    if not candidate.exists():
        candidate = EXAMPLES_DIR / methodology / "stages" / Path(doc).name
        if not candidate.exists():
            return None
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return None


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

def runs_dir(methodology: str) -> Path:
    return EXAMPLES_DIR / methodology / "runs"


def load_manifest(run_dir: Path) -> dict[str, Any]:
    """A run's manifest.json as a dict, or 404 if the run doesn't exist."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def list_runs(methodology: str) -> list[dict[str, Any]]:
    rdir = runs_dir(methodology)
    if not rdir.is_dir():
        return []
    entries = []
    for run in sorted(rdir.iterdir(), reverse=True):
        if not run.is_dir():
            continue
        manifest_path = run / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {"run_id": run.name, "status": "corrupt"}
            entries.append({
                "run_id": run.name,
                "status": manifest.get("status", "unknown"),
                "started_at": manifest.get("started_at"),
                "finished_at": manifest.get("finished_at"),
                # None for legacy (pre-versioning) runs; the template renders
                # "(unversioned)" — a displayed truth, not a fabricated id.
                "dag_version": manifest.get("dag_version"),
                "stages_total": len(manifest.get("stages", [])),
                "stages_ok": sum(1 for s in manifest.get("stages", []) if s.get("status") == "ok"),
                "stages_error": sum(1 for s in manifest.get("stages", []) if s.get("status") == "error"),
            })
    return entries


# ─── Tabular output previews ─────────────────────────────────────────────────

# Hard cap on rows rendered in the full-table view of a stage output. The CSV
# download endpoint has no cap — it always serves the complete file.
MAX_TABLE_ROWS = 5000


def read_table(path: Path) -> pd.DataFrame:
    """Read a stage output file (parquet or csv) into a DataFrame."""
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)


def manifest_stage(run_dir: Path, stage_id: str) -> dict[str, Any]:
    """The manifest record for one stage of a run; 404 if run or stage missing."""
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stages", []) if s.get("stage_id") == stage_id),
        None,
    )
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

def decisions_path(methodology: str, stage_id: str) -> Path:
    d = EXAMPLES_DIR / methodology / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stage_id}.parquet"


def load_decisions_df(methodology: str, stage_id: str) -> pd.DataFrame:
    p = decisions_path(methodology, stage_id)
    if not p.exists():
        return pd.DataFrame(
            columns=["content_hash", "decision", "modified_score",
                     "reviewer", "reviewed_at", "source_run_id"]
        )
    return pd.read_parquet(p)


def queue_snapshot(methodology: str, run_id: str, stage_id: str) -> pd.DataFrame | None:
    run_dir = runs_dir(methodology) / run_id
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
