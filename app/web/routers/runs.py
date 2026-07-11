"""Run lifecycle: trigger a run, list runs, poll live status, render a run's
detail + per-stage panel, the scratch in-memory re-run, artifact serving, and
resume."""

from __future__ import annotations

import threading
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.errors import NoVersionToRunError, RowOutOfRange, StageNotInRun
from app.services.loader import WorkflowLoadError, load_workflow
from app.runtime.preview import PREVIEWABLE_TYPES, PreviewError, run_stage_preview
from app.runtime.runner import prepare_run, resume_run, run_prepared
from app.runtime.trace import trace_row, trace_to_dict
from app.web.trace_view import build_trace_view
from app.web.config import EXAMPLES_DIR, REPO_ROOT, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import (
    build_llm_example,
    find_stage,
    list_runs,
    load_manifest,
    load_output_preview,
    load_output_row,
    load_output_table,
    load_stages,
    manifest_stage,
    read_output_df,
    resolve_function_code,
    runs_dir,
)

router = APIRouter()


def run_in_background(target, *args) -> None:
    """Run a (possibly slow, LLM-driven) execution off the event loop so the
    run page stays responsive and can poll live progress. Errors are recorded
    on the manifest by the runner itself; this just keeps the thread from dying
    silently."""
    def _wrapped():
        try:
            target(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=_wrapped, daemon=True).start()


@router.post("/project/{project}/run")
async def trigger_run(project: str):
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # Set up the run (writes an initial `running` manifest), kick off execution
    # in a background thread, and redirect immediately. The run page polls.
    try:
        prep = prepare_run(project_dir, REPO_ROOT)
    except NoVersionToRunError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "compiled workflow failed validation",
                             "issues": exc.issues}, status_code=400)
    run_in_background(run_prepared, prep)
    return RedirectResponse(
        url=f"/project/{project}/runs/{prep['run_id']}",
        status_code=303,
    )


@router.get("/project/{project}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, project: str):
    return templates.TemplateResponse(
        request,
        "runs_index.html",
        {"project": project, "runs": list_runs(project)},
    )


@router.get("/project/{project}/runs/{run_id}/status")
async def run_status(project: str, run_id: str):
    """Lightweight JSON for the live poller: current status, per-stage statuses,
    counts, and a freshly-built mermaid graph. Lets the run page update progress
    in place (no full-page reload) so it stays clickable while running."""
    manifest = load_manifest(runs_dir(project) / run_id)
    mstages = manifest.get("stages", [])
    status_by_id = {s["stage_id"]: s.get("status", "") for s in mstages}
    mermaid = build_mermaid_graph(load_stages(project).stages, project, status_by_id=status_by_id)

    def _count(st: str) -> int:
        return sum(1 for s in mstages if s.get("status") == st)

    return JSONResponse({
        "status": manifest.get("status"),
        "terminal": manifest.get("status") != "running",
        "halted_at": manifest.get("halted_at"),
        "finished_at": manifest.get("finished_at"),
        "counts": {"ok": _count("ok"), "warn": _count("validation_warnings"),
                   "err": _count("error"), "total": len(mstages),
                   "done": _count("ok") + _count("validation_warnings"),
                   "running": _count("running"), "pending": _count("pending"),
                   "awaiting": _count("awaiting_review")},
        "stages": [{"stage_id": s["stage_id"], "status": s.get("status")} for s in mstages],
        "mermaid": mermaid,
    })


@router.get("/project/{project}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, project: str, run_id: str):
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stages = load_stages(project).stages
    status_by_id = {s["stage_id"]: s.get("status", "") for s in manifest.get("stages", [])}
    mermaid = build_mermaid_graph(stages, project, status_by_id=status_by_id)

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "project": project,
            "run_id": run_id,
            "manifest": manifest,
            "mermaid": mermaid,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/partial",
    response_class=HTMLResponse,
)
async def run_stage_partial(
    request: Request, project: str, run_id: str, stage_id: str
):
    """Per-run stage detail panel — status, validation, preview, error trace."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stages", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")

    output_preview = load_output_preview(run_dir, stage_record.get("output_path"))

    # Build input previews from upstream stages' outputs in this run.
    stages_static = load_stages(project).stages
    stage_def = find_stage(stages_static, stage_id)
    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])
    }
    input_previews: list[dict[str, Any]] = []
    if stage_def is not None:
        for input_id in stage_def.input_ids:
            input_previews.append(
                {
                    "id": input_id,
                    "preview": load_output_preview(run_dir, output_by_id.get(input_id)),
                }
            )

    function_code = resolve_function_code(stage_def)
    llm_example = build_llm_example(stage_def, input_previews) if stage_def else None

    return templates.TemplateResponse(
        request,
        "_run_stage_panel.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "preview": output_preview,
            "input_previews": input_previews,
            "function_code": function_code,
            "llm_example": llm_example,
            "previewable": stage_def is not None and stage_def.type in PREVIEWABLE_TYPES,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/rows",
    response_class=HTMLResponse,
)
async def run_stage_rows(
    request: Request, project: str, run_id: str, stage_id: str
):
    """Full table of one stage's output, capped at MAX_TABLE_ROWS rendered rows.
    The page links to the uncapped CSV download."""
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    table = load_output_table(run_dir, stage_record.get("output_path"))
    return templates.TemplateResponse(
        request,
        "run_stage_rows.html",
        {
            "project": project,
            "run_id": run_id,
            "stage_id": stage_id,
            "stage": stage_record,
            "output_path": stage_record.get("output_path"),
            **table,
        },
    )


@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/rows.csv")
async def run_stage_rows_csv(project: str, run_id: str, stage_id: str):
    """One stage's complete output as a CSV download (no row cap)."""
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    df = read_output_df(run_dir, stage_record.get("output_path"))
    filename = f"{project}__{run_id}__{stage_id}.csv"
    return Response(
        content=df.to_csv(index=False),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/lineage_panel",
    response_class=HTMLResponse,
)
async def run_stage_lineage_panel(
    request: Request, project: str, run_id: str, stage_id: str, row: int
):
    """Minimal stage view for the lineage page: the transform, the output
    schema, and the output trimmed to `row`. Reuses `_stage_executable.html`
    and `schema_table` — not the whole run-detail panel."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stages", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")
    # Transform detail needs the compiled stage; if it's unavailable the output
    # table still renders (stage_def None → the template says so).
    try:
        stages = load_stages(project).stages
    except HTTPException:
        stages = []
    stage_def = find_stage(stages, stage_id)
    return templates.TemplateResponse(
        request,
        "_lineage_stage.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "function_code": resolve_function_code(stage_def),
            "preview": load_output_row(run_dir, stage_record.get("output_path"), row),
            "scoped_row": row,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace")
async def run_stage_row_trace(project: str, run_id: str, stage_id: str, row: int):
    """Show-your-work for one output row: its ancestry through row-preserving
    stages, as JSON. 404 if the run/stage is absent, 400 if the row ordinal is
    out of range."""
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    try:
        trace = trace_row(run_dir, stage_id, row)
    except StageNotInRun as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RowOutOfRange as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(trace_to_dict(trace))


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
    response_class=HTMLResponse,
)
async def run_stage_row_trace_view(
    request: Request, project: str, run_id: str, stage_id: str, row: int
):
    """The row's show-your-work as a read-only HTML page: a numbered story and a
    graph toggle on top; clicking a stage loads the row-trimmed panel below."""
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)
    try:
        trace = trace_row(run_dir, stage_id, row)
    except StageNotInRun as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RowOutOfRange as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Node detail comes from the compiled stages; if they can't be loaded the
    # trace still renders (transform shows as "unknown"). NB: load_stages raising
    # HTTPException from a loader is a layering wart worth fixing separately.
    try:
        stages = load_stages(project).stages
    except HTTPException:
        stages = []
    stages_by_id = {s.id: s for s in stages}

    view = build_trace_view(trace_to_dict(trace), stages_by_id)
    ordered = [stages_by_id[n["stage_id"]] for n in view["nodes"]
               if n["stage_id"] in stages_by_id]
    mermaid = build_mermaid_graph(ordered, project) if len(ordered) == len(view["nodes"]) else ""
    return templates.TemplateResponse(
        request,
        "lineage.html",
        {
            "title": f"{view['start_stage']} · row {view['start_row']}",
            "view": view,
            "project": project,
            "mermaid": mermaid,
        },
    )


@router.post("/project/{project}/runs/{run_id}/stage/{stage_id}/preview")
async def run_stage_scratch_preview(
    request: Request, project: str, run_id: str, stage_id: str
):
    """SCRATCH in-memory re-run of one stage on a few selected input rows.

    Reads the chosen rows from this run's upstream outputs, runs the stage's
    handler in memory, and returns the output rows as JSON. Nothing is
    persisted: no manifest change, no output file, no artifact. Used by the
    node-detail panel's "Run transform on selected" button.

    Body (JSON): {"indices": [int, ...]}  — positional row indices into the
    stage's first upstream input.
    """
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    indices_raw = (body or {}).get("indices", [])
    indices: list[int] = []
    for i in indices_raw:
        try:
            indices.append(int(i))
        except (TypeError, ValueError):
            continue

    stages_static = load_stages(project).stages
    stage_def = find_stage(stages_static, stage_id)
    if stage_def is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}'")

    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])
    }

    try:
        result = run_stage_preview(
            stage_def=stage_def,
            run_dir=run_dir,
            repo_root=REPO_ROOT,
            project_dir=EXAMPLES_DIR / project,
            output_by_id=output_by_id,
            selected_indices=indices,
        )
    except PreviewError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — surface the real failure
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}",
             "traceback": traceback.format_exc(limit=8)},
            status_code=500,
        )

    return JSONResponse({"ok": True, **result})


@router.get("/project/{project}/runs/{run_id}/artifact/{filename:path}", response_class=HTMLResponse)
async def run_artifact(project: str, run_id: str, filename: str):
    """Serve generated HTML artifacts (per-org profiles etc.) inline."""
    run_dir = runs_dir(project) / run_id
    candidate = (run_dir / "artifacts" / filename).resolve()
    if not candidate.exists() or not str(candidate).startswith(str(run_dir.resolve())):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return HTMLResponse(content=candidate.read_text(encoding="utf-8"))


@router.post("/project/{project}/runs/{run_id}/resume")
async def resume_run_route(project: str, run_id: str):
    """Resume/continue a run from where it stopped, re-running any stage that is
    NOT already complete (so this serves BOTH: a halted run after its review
    decisions, AND an ERRORED run after the bug is fixed — it re-runs the failed
    stage + downstream and reuses completed upstream outputs)."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    run_dir = runs_dir(project) / run_id
    if not (run_dir / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="Run not found")
    # Validate the compiled workflow synchronously so load errors surface as a 400
    # here rather than being swallowed on the background thread below.
    try:
        load_workflow(project_dir)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "compiled workflow failed validation",
                             "issues": exc.issues}, status_code=400)
    # Resume re-runs the queue stage + downstream (LLM-heavy) — do it in the
    # background and redirect immediately so the page can poll progress.
    run_in_background(resume_run, project_dir, run_id, REPO_ROOT)
    return RedirectResponse(
        url=f"/project/{project}/runs/{run_id}",
        status_code=303,
    )
