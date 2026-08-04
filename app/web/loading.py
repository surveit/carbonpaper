# Reviewer decisions are NOT read from disk here — they live in the stage-result
# cache (app.core.stage_cache).
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException

from app.core.errors import NoVersionToRunError
from app.core.frames import PARQUET_SUFFIX, list_rows
from app.models import Stage, StageType
from app.models.stages.llm_transform import LLMTransformStage
from app.runtime.manifest import load_manifest_model
from app.services.run import resolve_version
from app.services.loader import CompiledStageFile, load_compiled_dir
from app.services.versioning import list_versions, load_version_stages
from app.services.workspace import load_schemas, resolve_project_dir
from app.web.config import projects_dir


# ─── Projects & stages ──────────────────────────────────────────────────

def list_projects() -> list[dict[str, Any]]:
    """One project card per dir under examples/, in the shape the home dashboard
    renders. The card's headline question is binary — is the project still being
    SET UP, or is it READY TO RUN? — so alongside the authored-what flags
    (has_document / has_schemas / has_workflow) each card carries `is_ready`:
    True iff at least one PUBLISHED version exists, because a run pins a
    published version (app.services.run.resolve_version) and an
    unpublished, agent-minted draft is not runnable. Sorted by name.

    Every flag and count is read off disk — a card never advertises a
    stage/schema/run/version that isn't there. A directory counts as a project
    from the moment creation writes its document.md (or project.json) — a
    just-created project whose data model is still being generated must show up,
    not appear only once generation finishes. A dir with none of those markers is
    not a project and is omitted. A run counts only if it has a manifest.json
    (mirrors the runs index), so the count is real runs, never inflated."""
    if not projects_dir().exists():
        return []
    cards: list[dict[str, Any]] = []
    for p in sorted(projects_dir().iterdir()):
        if not p.is_dir():
            continue
        card = _build_project_card(p)
        if card is not None:
            cards.append(card)
    return cards


def _build_project_card(p: Path) -> dict[str, Any] | None:
    """One project dir's dashboard card, or None if `p` carries none of the
    creation markers (document/workflow/schemas) and so is not a project."""
    compiled_dir = p / "compiled"
    schemas_dir = p / "schemas"
    n_stages = len(list(compiled_dir.glob("*.json"))) if compiled_dir.is_dir() else 0
    has_workflow = n_stages > 0
    has_schemas = schemas_dir.is_dir() and any(schemas_dir.glob("*.json"))
    n_schemas = len(load_schemas(p)) if has_schemas else 0
    n_runs = _count_runs_with_manifest(p / "runs")
    has_document = (p / "document.md").is_file() or (p / "project.json").is_file()
    if not (has_workflow or has_schemas or has_document):
        return None
    return {
        "name": p.name,
        "has_document": has_document,
        "has_workflow": has_workflow,
        "has_schemas": has_schemas,
        "is_ready": any(v.published for v in list_versions(p)),
        "n_stages": n_stages,
        "n_schemas": n_schemas,
        "n_runs": n_runs,
    }


def _count_runs_with_manifest(rdir: Path) -> int:
    """Non-test runs only: a run dir counts iff it carries a manifest.json
    (mirrors the runs index) AND that manifest is not a test run's, so an
    in-progress/abandoned run dir, or a workflow test's run, is never counted."""
    if not rdir.is_dir():
        return 0
    return sum(
        1 for r in rdir.iterdir()
        if r.is_dir() and _manifest_counts_as_run(r / "manifest.json")
    )


def _manifest_counts_as_run(manifest_path: Path) -> bool:
    """Whether `manifest_path` exists and records a run that is not a test
    (default: not a test, for a manifest with no `is_test_run` key — every run
    before that field existed). A missing or unparseable manifest is not a run
    at all here (the caller only calls this after confirming existence, or wants
    False either way), so a parse failure also reports False."""
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return not manifest.get("is_test_run", False)


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
    compiled_dir = projects_dir() / project / "compiled"
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
    compiled_dir = projects_dir() / project / "compiled"
    if not compiled_dir.is_dir():
        return StageListing(stages=[], issues=[], order={})
    return load_stages(project)


def find_stage(stages: list[Stage], stage_id: str) -> Stage | None:
    return next((s for s in stages if s.id == stage_id), None)


def list_file_inputs(project: str, version_id: str | None = None) -> list[dict[str, Any]]:
    """File-kind input stages of the version a triggered run will execute, each
    with its workflow-authored absolute path ('' when the stage authors none —
    the run form must collect one). `version_id` selects which version to read;
    None resolves to the latest (resolve_version's default), so the run form's
    prefill and the run's binding provenance both speak about the SAME version.
    [] when the project has no versions yet."""
    try:
        version_id = resolve_version(project, version_id)
    except NoVersionToRunError:
        return []
    stages = load_version_stages(resolve_project_dir(project), version_id)
    return [
        {"stage_id": s.id, "name": s.name,
         "path": str((s.connector.params or {}).get("path") or "")}
        for s in stages
        if s.type == StageType.input_data and s.connector.kind == "file"
    ]


# ─── Uploaded run-input files ────────────────────────────────────────────────

def _safe_component(raw: str, fallback: str) -> str:
    """A single, traversal-safe path component from untrusted input: the basename
    with any directory parts stripped, rejecting the specials that would still
    escape ('', '.', '..' — note Path('../..').name is '..', not '')."""
    name = Path(raw).name
    return fallback if name in ("", ".", "..") else name


def save_uploaded_input(project_dir: Path, stage_id: str, filename: str, src) -> Path:
    """Save a browser-uploaded run-input file under the project's
    uploads/<stage_id>/ dir and return its absolute path.

    A run reads its inputs off the SERVER's disk by absolute path, but a browser
    `<input type=file>` hands over only bytes, never a path (every OS hides it) —
    so the cross-platform Browse uploads the file and the run then reads THIS
    saved copy by path, exactly like any other input. `src` is a readable binary
    stream (the UploadFile's file); it's streamed to disk, so large files don't
    have to sit in memory. Both the stage id and filename are reduced to a single
    safe component (no directory traversal); the per-stage subdir keeps two
    stages' same-named files from colliding, and re-uploading the same stage/name
    overwrites in place (a fresh pick replaces the old copy)."""
    safe_stage = _safe_component(stage_id, "input")
    safe_name = _safe_component(filename, "upload.dat")
    dest_dir = project_dir / "uploads" / safe_stage
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    with dest.open("wb") as out:
        shutil.copyfileobj(src, out)
    return dest.resolve()


# ─── Runs & manifests ────────────────────────────────────────────────────────

def runs_dir(project: str) -> Path:
    return projects_dir() / project / "runs"


def load_manifest(run_dir: Path) -> dict[str, Any]:
    """A run's manifest.json as a dict, or 404 if the run doesn't exist.

    Parses through the typed `RunManifest`, so every consumer sees one shape:
    the model normalizes a legacy (pre-fork-aware) scalar `halted_at` stage-id
    string into a one-element list (a template `{% for %}` would otherwise
    iterate a bare string character-by-character), and re-serializes with unset
    optional fields omitted — the same shape the executor persisted."""
    if not (run_dir / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return load_manifest_model(run_dir).to_dict()


# ─── Tabular output previews ─────────────────────────────────────────────────

# Hard cap on rows rendered in the full-table view of a stage output. The CSV
# download endpoint has no cap — it always serves the complete file.
MAX_TABLE_ROWS = 5000


def read_table(path: Path) -> pd.DataFrame:
    """Read a stage output file (parquet or csv) into a DataFrame."""
    return pd.read_parquet(path) if path.suffix == PARQUET_SUFFIX else pd.read_csv(path)


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
    """`df` as CSV download bytes: UTF-8 behind a byte-order mark (see `_UTF8_BOM`)."""
    return (_UTF8_BOM + df.to_csv(index=False)).encode("utf-8")


def manifest_stage(run_dir: Path, stage_id: str) -> dict[str, Any]:
    """The manifest record for one stage of a run; 404 if run or stage missing."""
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stage_records", []) if s.get("stage_id") == stage_id),
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


def render_frame_as_text(frame: pd.DataFrame) -> pd.DataFrame:
    """`frame` with every cell a display string and every null "" ."""
    # astype(str) first so each dtype formats itself (a datetime renders 2026-01-01,
    # not 2026-01-01 00:00:00), then blank the nulls off the ORIGINAL frame's mask.
    # Blanking via fillna("") instead would RAISE on pandas' masked dtypes
    # (Int64/Float64/boolean) — what a declared-nullable int/float/bool arrives as.
    return frame.astype(str).where(frame.notna(), "")


def render_cells_as_text(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Every cell as a display string, with nulls as ""."""
    return list_rows(render_frame_as_text(frame))


def load_output_table(run_dir: Path, rel_path: str | None) -> dict[str, Any]:
    """Full (capped) table of a stage output: columns, total row count, up to
    MAX_TABLE_ROWS rows as strings, and whether the render was capped."""
    df = read_output_df(run_dir, rel_path)
    rows = render_cells_as_text(df.head(MAX_TABLE_ROWS))
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
        "preview": render_cells_as_text(df.iloc[[row]]),
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
        "preview": render_cells_as_text(df.head(5)),
    }


# ─── Queue snapshots ──────────────────────────────────────────────────────────

def queue_snapshot(project: str, run_id: str, stage_id: str) -> pd.DataFrame | None:
    run_dir = runs_dir(project) / run_id
    for ext in (".parquet", ".csv"):
        p = run_dir / "queue" / f"{stage_id}{ext}"
        if p.exists():
            return read_table(p)
    return None


@dataclass
class QueueFingerprints:
    """The bookkeeping a halted queue stage's snapshot carries off to the
    side, never as snapshot columns: `stage_fingerprint` (shared by every
    pending row of that halt), `input_fingerprints` and `row_ordinals` (one per
    row each, POSITIONALLY aligned to the snapshot's row order).
    `row_ordinals` is None for a sidecar written before the runtime recorded
    them — an unknowable position, never a guessed one."""
    stage_fingerprint: str
    input_fingerprints: list[str]
    row_ordinals: list[int] | None


def load_queue_fingerprints(project: str, run_id: str, stage_id: str) -> QueueFingerprints | None:
    """The sidecar `<stage_id>.fingerprints.json` a halted human_review_queue
    stage writes beside its snapshot (app.runtime.stages.human_review_queue).
    None if no run has halted at this stage yet (no such sidecar).

    Raises ValueError if the snapshot exists but its row count doesn't match
    `input_fingerprints`' length, or if `row_ordinals` is present with a
    different length: positional alignment between these lists is not
    something to guess at silently when it can't be verified."""
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
    """Scalar-safe cell formatting for the reviewer UI. pd.isna() raises on
    list/array-valued cells (e.g. an evidence_urls JSON column), so handle
    array-likes explicitly before the null check."""
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
    stage_def: Stage | None, input_previews: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Render the prompt_data_template with the first row of the first usable input.

    Returns {rendered, source_id} on success, {error} if no input or render
    fails, or None if the stage isn't an LLM stage.
    """
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
