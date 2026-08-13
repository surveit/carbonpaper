# Reviewer decisions are NOT read from disk here — they live in the stage-result
# cache (app.core.stage_cache).
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException

from app.core.errors import NoVersionToRunError, StageOutputMissing
from app.core.frames import list_rows, read_frame_file, render_frame_as_csv_text
from app.models import (
    StageType,
    Workflow,
    WorkflowNotFormed,
    WorkflowStage,
    build_workflow,
)
from app.models.stages.llm_transform import LLMTransformStage
from app.models.run_manifest import read_run_manifest
from app.runtime.manifest import resolve_output_path
from app.services.run import resolve_version
from app.services.loader import (
    StageEntry,
    exists as has_working_copy,
    find_file_issues,
    list_parsed_stages,
    load_stage_entries,
    read_stage_specs,
)
from app.services.versioning import list_versions, load_version_stages
from app.services.project import read_project_name
from app.services.terms import count_nouns
from app.services.workspace import resolve_project_dir
from app.web.config import projects_dir
from app.web.project_cards import ProjectCard, tally_runs


# ─── Projects & stages ──────────────────────────────────────────────────

def list_projects() -> list[ProjectCard]:
    if not projects_dir().exists():
        return []
    cards: list[ProjectCard] = []
    for p in sorted(projects_dir().iterdir()):
        if not p.is_dir():
            continue
        card = _build_project_card(p)
        if card is not None:
            cards.append(card)
    return cards


def _build_project_card(p: Path) -> ProjectCard | None:
    n_stages = len(read_stage_specs(p.name))
    has_workflow = n_stages > 0
    n_schemas = count_nouns(p.name)
    has_schemas = n_schemas > 0
    runs = tally_runs(p / "runs")
    has_document = (p / "document.md").is_file() or (p / "project.json").is_file()
    if not (has_workflow or has_schemas or has_document):
        return None
    return ProjectCard(
        id=p.name,
        label=read_project_name(p.name),
        has_document=has_document,
        has_workflow=has_workflow,
        has_schemas=has_schemas,
        is_ready=bool(list_versions(p)),
        n_stages=n_stages,
        n_schemas=n_schemas,
        n_runs=runs.real,
        n_test_runs=runs.tests,
        status=runs.headline,
    )


@dataclass
class StageListing:
    """All-or-nothing: one invalid stage empties `entries`, and `issues` names the broken ones."""
    entries: list[StageEntry]
    # A working copy mid-edit often forms no workflow; `WorkflowNotFormed` says why,
    # so no reader has to hold a reason beside a missing one.
    workflow: Workflow | WorkflowNotFormed
    issues: list[StageEntry]


def load_stages(project: str) -> StageListing:
    if not has_working_copy(project):
        raise HTTPException(status_code=404, detail=f"No workflow for {project}")
    entries = load_stage_entries(project)
    issues = [e for e in entries if e.issues]
    if issues:
        # One invalid stage breaks the whole workflow — its inputs no longer
        # resolve, so the surviving stages form a workflow with holes. Rendering that
        # is "unusable but lies." Return no stages, only the issues, so the
        # viewer shows what's broken instead of a false graph.
        return StageListing(
            entries=[], workflow=WorkflowNotFormed(issues=find_file_issues(issues)),
            issues=issues)
    return StageListing(
        entries=entries,
        workflow=build_workflow(list_parsed_stages(entries)),
        issues=[],
    )


def load_stages_or_empty(project: str) -> StageListing:
    if not has_working_copy(project):
        return StageListing(
            entries=[],
            workflow=WorkflowNotFormed(issues=["the project has no stages yet"]),
            issues=[])
    return load_stages(project)


def find_workflow_stage(
    workflow: Workflow | WorkflowNotFormed, stage_id: str
) -> WorkflowStage | None:
    """None where the stages form no workflow, exactly as for a stage id it does not define."""
    if isinstance(workflow, WorkflowNotFormed):
        return None
    return workflow.index_workflow_stages_by_id().get(stage_id)


def list_file_inputs(project: str, version_id: str | None = None) -> list[dict[str, Any]]:
    try:
        version_id = resolve_version(project, version_id)
    except NoVersionToRunError:
        return []
    stages = load_version_stages(resolve_project_dir(project), version_id)
    return [
        {"stage_id": s.id,
         "path": str((s.connector.params or {}).get("path") or "")}
        for s in stages
        if s.type == StageType.input_data and s.connector.kind == "file"
    ]


# ─── Runs & manifests ────────────────────────────────────────────────────────

def runs_dir(project: str) -> Path:
    return projects_dir() / project / "runs"


def load_manifest(run_dir: Path) -> dict[str, Any]:
    if not (run_dir / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return read_run_manifest(run_dir).to_dict()


# ─── Tabular output previews ─────────────────────────────────────────────────

# Hard cap on rows rendered in the full-table view of a stage output. The CSV
# download endpoint has no cap — it always serves the complete file. A caller
# that renders once to a file rather than per request may raise it (the review
# packet does), so load_output_table takes it as an argument.
MAX_TABLE_ROWS = 5000

# One budget for every truncated table the stage panel draws — the output preview,
# the input previews, the diff, the scratch re-run. They sit in the same panel and
# look alike, so a per-path number reads as a property of the data rather than of
# the surface: the same reader saw 100 rows under one stage and 5 under the next,
# with nothing on screen to say why. The full-rows page and the review packet are
# separate surfaces and keep their own, larger budgets.
PREVIEW_ROWS_SHOWN = 100


# Excel on Windows reads a .csv in the machine's legacy code page (cp1252 on a
# Western install) unless the file opens with a UTF-8 byte-order mark. Without
# the mark, a run whose rows hold French or Dutch text downloads clean and then
# renders as "mÃ©rite" for a Windows reviewer while the same file is fine on
# macOS — the reviewer reads mojibake and cannot judge the row. The mark costs
# the readers that do not need it nothing: pandas.read_csv strips a leading BOM
# on its default encoding, so re-importing a downloaded file through an
# `input_data` csv connector keeps its first column name intact.
_UTF8_BOM = "\ufeff"


def csv_download_body(df: pd.DataFrame) -> bytes:
    return (_UTF8_BOM + render_frame_as_csv_text(df)).encode("utf-8")


def manifest_stage(run_dir: Path, stage_id: str) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stage_records", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")
    return stage_record


def read_output_df(run_dir: Path, rel_path: str | None) -> pd.DataFrame:
    try:
        path = resolve_output_path(run_dir, rel_path)
    except StageOutputMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="Stage has no output file")
    if not path.exists():
        raise HTTPException(
            status_code=404, detail=f"Output file missing on disk: {rel_path}"
        )
    try:
        return read_frame_file(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Could not read output file: {exc}"
        ) from exc


def render_frame_as_text(frame: pd.DataFrame) -> pd.DataFrame:
    # fillna("") would raise on Int64/Float64/boolean.
    return frame.astype(str).where(frame.notna(), "")  # astype first: dtypes self-format


def render_cells_as_text(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return list_rows(render_frame_as_text(frame))


SELECTED_ORDINAL_KEY = "_row_ordinal"


def load_output_table(
    run_dir: Path, rel_path: str | None, max_rows: int | None = None
) -> dict[str, Any]:
    # Not a default arg: that binds MAX_TABLE_ROWS at import, past any later rebinding.
    df = read_output_df(run_dir, rel_path)
    rows = render_cells_as_text(df.head(MAX_TABLE_ROWS if max_rows is None else max_rows))
    return {
        "columns": list(df.columns),
        "rows": rows,
        "rows_total": len(df),
        "capped": len(df) > len(rows),
    }


def load_selected_output_rows(
    run_dir: Path, rel_path: str | None, ordinals: list[int]
) -> dict[str, Any]:
    df = read_output_df(run_dir, rel_path)
    kept = [o for o in ordinals if 0 <= o < len(df)][:MAX_TABLE_ROWS]
    rows = render_cells_as_text(df.iloc[kept])
    for ordinal, row in zip(kept, rows):
        # The reader arrived from a contributor chip, so the row's ORDINAL is the
        # thing they need back — a position in the filtered list means nothing.
        row[SELECTED_ORDINAL_KEY] = ordinal
    return {
        "columns": list(df.columns),
        "rows": rows,
        "rows_total": len(df),
        "capped": len([o for o in ordinals if 0 <= o < len(df)]) > len(rows),
        "selected_total": len(ordinals),
    }


def load_output_row(run_dir: Path, rel_path: str | None, row: int) -> dict[str, Any] | None:
    if not rel_path:
        return None
    try:
        path = resolve_output_path(run_dir, rel_path)
    except StageOutputMissing as exc:
        return {"error": str(exc)}
    if path is None or not path.exists():
        return {"error": f"missing on disk: {rel_path}"}
    try:
        df = read_frame_file(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if row < 0 or row >= len(df):
        return {"columns": list(df.columns), "rows_total": len(df),
                "preview": [], "row_index": row, "out_of_range": True}
    return {
        "columns": list(df.columns),
        "rows_total": len(df),
        "preview": render_cells_as_text(df.iloc[[row]]),
        "row_index": row,
    }


def load_output_preview(run_dir: Path, rel_path: str | None) -> dict[str, Any] | None:
    if not rel_path:
        return None
    try:
        path = resolve_output_path(run_dir, rel_path)
    except StageOutputMissing as exc:
        return {"error": str(exc)}
    if path is None or not path.exists():
        return {"error": f"missing on disk: {rel_path}"}
    try:
        df = read_frame_file(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {
        "columns": list(df.columns),
        "rows_total": len(df),
        "preview": render_cells_as_text(df.head(PREVIEW_ROWS_SHOWN)),
    }


# ─── Queue snapshots ──────────────────────────────────────────────────────────

def queue_snapshot(project: str, run_id: str, stage_id: str) -> pd.DataFrame | None:
    run_dir = runs_dir(project) / run_id
    for ext in (".parquet", ".csv"):
        p = run_dir / "queue" / f"{stage_id}{ext}"
        if p.exists():
            return read_frame_file(p)
    return None


@dataclass
class QueueFingerprints:
    """`input_fingerprints` and `row_ordinals` are POSITIONALLY aligned to the snapshot's rows."""
    stage_fingerprint: str
    input_fingerprints: list[str]
    row_ordinals: list[int] | None


def load_queue_fingerprints(project: str, run_id: str, stage_id: str) -> QueueFingerprints | None:
    run_dir = runs_dir(project) / run_id
    path = run_dir / "queue" / f"{stage_id}.fingerprints.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    ordinals = data.get("row_ordinals")
    fingerprints = QueueFingerprints(
        stage_fingerprint=data["stage_fingerprint"],
        input_fingerprints=data["input_fingerprints"],
        row_ordinals=None if ordinals is None else [int(o) for o in ordinals],
    )
    _validate_sidecar_alignment(fingerprints, queue_snapshot(project, run_id, stage_id),
                                stage_id, run_id)
    return fingerprints


def _validate_sidecar_alignment(
    fingerprints: QueueFingerprints, snapshot: pd.DataFrame | None,
    stage_id: str, run_id: str,
) -> None:
    expected = len(fingerprints.input_fingerprints)
    if snapshot is not None and len(snapshot) != expected:
        raise ValueError(
            f"queue fingerprints sidecar for stage '{stage_id}' in run '{run_id}' "
            f"names {expected} row(s) but the snapshot has {len(snapshot)} — "
            "alignment cannot be trusted"
        )
    ordinals = fingerprints.row_ordinals
    if ordinals is not None and len(ordinals) != expected:
        raise ValueError(
            f"queue fingerprints sidecar for stage '{stage_id}' in run '{run_id}' "
            f"names {expected} fingerprint(s) but {len(ordinals)} row ordinal(s) — "
            "alignment cannot be trusted"
        )


def display_cell(v: Any) -> Any:
    """pd.isna() raises on a list/array cell, so array-likes are handled before the null check."""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if len(v) else ""
    if isinstance(v, (pd.Timestamp, datetime, date)):  # not JSON-serializable for the UI's tojson
        return "" if pd.isna(v) else v.isoformat()
    if hasattr(v, "tolist") and not isinstance(v, str):  # numpy array from parquet
        seq = v.tolist()
        return ", ".join(str(x) for x in seq) if len(seq) else ""
    try:
        return "" if pd.isna(v) else v
    except (ValueError, TypeError):
        return v


# ─── LLM prompt example ──────────────────────────────────────────────────────

def build_llm_example(
    workflow_stage: WorkflowStage | None, input_previews: list[dict[str, Any]]
) -> dict[str, Any] | None:
    stage_def = None if workflow_stage is None else workflow_stage.stage
    template = (
        stage_def.llm.prompt_data_template
        if isinstance(stage_def, LLMTransformStage) else None
    )
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
