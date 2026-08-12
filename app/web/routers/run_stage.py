"""One stage inside one run: the per-stage detail panel, its full-rows table and
CSV download, and the simulate page plus the in-memory re-run it posts to. Split
out of app.web.routers.runs, which holds the run-level lifecycle routes and sat
at the import-graph fan-out ceiling."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from app.services.loader import resolve_function_code
from app.services import run as run_service
from app.runtime.errors import PreviewError
from app.runtime.preview import PREVIEWABLE_TYPES, run_stage_preview
from app.web import loading
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import EVENT_TAIL, REPO_ROOT, templates
from app.web.eval_coverage import find_eval_coverage
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH
from app.web.loading import (
    build_llm_example,
    csv_download_body,
    load_manifest,
    load_output_preview,
    load_output_table,
    manifest_stage,
    read_output_df,
    render_cells_as_text,
    runs_dir,
)
from app.web.run_stage_panel import not_executed_panel, resolve_panel_links
from app.web.stage_diff import StageDiff, build_stage_diff

router = APIRouter()


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/partial",
    response_class=HTMLResponse,
)
async def run_stage_partial(
    request: Request, project: str, run_id: str, stage_id: str
):
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stage_records", []) if s.get("stage_id") == stage_id),
        None,
    )

    # The panel's Schema tier and Transform detail describe what THIS run
    # executed, so they read the version it pinned. With no resolvable version
    # there is no stage definition to show and the panel says why.
    pinned = run_service.load_pinned_stage_def(project, manifest, stage_id)
    stage_def = None if pinned.workflow_stage is None else pinned.workflow_stage.stage
    if stage_record is None:
        # A stage the graph draws but this run never executed (a workflow test
        # injects its input stages) — see app.web.run_stage_panel.
        return not_executed_panel(request, project, run_id, manifest, stage_id, pinned)

    output_preview = load_output_preview(run_dir, stage_record.get("output_path"))
    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])
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
    llm_example = build_llm_example(pinned.workflow_stage, input_previews)

    return templates.TemplateResponse(
        request,
        "_run_stage_panel.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "workflow_stage": pinned.workflow_stage,
            "stage_def_error": pinned.error,
            "preview": output_preview,
            # None for every stage type outside the diff's scope, and for any
            # stage whose alignment can't be verified — the pane then shows the
            # plain output view (app.web.stage_diff).
            "diff": build_stage_diff(
                pinned.workflow_stage, run_dir,
                stage_record.get("output_path"), output_by_id
            ),
            "input_previews": input_previews,
            "function_code": function_code,
            "llm_example": llm_example,
            "test_views": (views := shape_test_views(pinned.workflow_stage)),
            "certification": build_certification(pinned.workflow_stage, views),
            # Judged against the version THIS run pinned, so the verdict is about the
            # code that produced the rows above it. None where no eval targets the stage.
            "eval_coverage": find_eval_coverage(
                project, stage_id, manifest.get("workflow_version")),
            "previewable": stage_def is not None and stage_def.type in PREVIEWABLE_TYPES,
            "links": resolve_panel_links(project, run_id),
            "event_tail": EVENT_TAIL,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/rows",
    response_class=HTMLResponse,
)
async def run_stage_rows(
    request: Request, project: str, run_id: str, stage_id: str, raw: bool = False,
    ordinals: str | None = None,
):
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    selected = _parse_ordinals(ordinals)
    table = (
        loading.load_selected_output_rows(run_dir, stage_record.get("output_path"), selected)
        if selected is not None
        else load_output_table(run_dir, stage_record.get("output_path"))
    )
    return templates.TemplateResponse(
        request,
        "run_stage_rows.html",
        {
            "project": project,
            "crumbs": build_run_child_crumbs(project, run_id, label=f"{stage_id} rows"),
            "run_id": run_id,
            "stage_id": stage_id,
            "stage": stage_record,
            "output_path": stage_record.get("output_path"),
            # Built even under ?raw=1: the raw view offers the diff view only
            # where one actually exists, so it has to know either way.
            # No diff over a filtered view: the diff aligns output rows to input
            # rows by position, which a subset cannot honour.
            "diff": (
                None if selected is not None
                else _build_full_rows_diff(project, run_dir, stage_id, stage_record)
            ),
            "raw": raw,
            "links": resolve_panel_links(project, run_id),
            # The page's own treatments (row numbers, click-to-expand cells,
            # sticky-header scroll box) the shared diff partial renders on request.
            "full_rows": True,
            **table,
        },
    )


def _parse_ordinals(ordinals: str | None) -> list[int] | None:
    if ordinals is None:
        return None
    # A malformed entry is skipped rather than failing the page: the parameter
    # arrives from a link, and showing the rows it did name beats a 422.
    parsed = []
    for part in ordinals.split(","):
        try:
            parsed.append(int(part))
        except ValueError:
            continue
    return parsed


def _build_full_rows_diff(
    project: str, run_dir: Path, stage_id: str, stage_record: dict[str, Any]
) -> StageDiff | None:
    manifest = load_manifest(run_dir)
    return build_stage_diff(
        run_service.load_pinned_stage_def(project, manifest, stage_id).workflow_stage,
        run_dir,
        stage_record.get("output_path"),
        {s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])},
        rows_shown=loading.MAX_TABLE_ROWS,
    )


@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/rows.csv")
async def run_stage_rows_csv(project: str, run_id: str, stage_id: str):
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    df = read_output_df(run_dir, stage_record.get("output_path"))
    filename = f"{project}__{run_id}__{stage_id}.csv"
    return Response(
        content=csv_download_body(df),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/simulate",
    response_class=HTMLResponse,
)
async def run_stage_simulate(
    request: Request, project: str, run_id: str, stage_id: str
):
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    # The page executes a stage under this run's name, so it offers only what the
    # run pinned. No resolvable version, or a type the runner cannot preview, and
    # there is nothing here to simulate — the panel links no page in either case.
    pinned = run_service.load_pinned_stage_def(project, manifest, stage_id)
    stage_def = None if pinned.workflow_stage is None else pinned.workflow_stage.stage
    if stage_def is None:
        raise HTTPException(
            status_code=404, detail=pinned.error or f"No stage '{stage_id}' in run"
        )
    if stage_def.type not in PREVIEWABLE_TYPES:
        raise HTTPException(
            status_code=404,
            detail=f"'{stage_def.type}' stages cannot be run one row at a time",
        )

    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])
    }
    return templates.TemplateResponse(
        request,
        "run_stage_simulate.html",
        {
            "project": project,
            "run_id": run_id,
            "stage_id": stage_id,
            "crumbs": build_run_child_crumbs(
                project, run_id, label=f"simulate {stage_id}"
            ),
            "stage": stage_def,
            "input_previews": [
                {"id": input_id, "preview": load_output_preview(run_dir, output_by_id.get(input_id))}
                for input_id in stage_def.input_ids
            ],
            "function_code": resolve_function_code(stage_def),
            # _stage_executable.html reads both; this page shows the transform to
            # say what is about to run, not to certify or illustrate it.
            "llm_example": None,
            "certification": None,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.post("/project/{project}/runs/{run_id}/stage/{stage_id}/preview")
async def run_stage_scratch_preview(
    request: Request, project: str, run_id: str, stage_id: str
):
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

    # This executes a stage against THIS run's rows and is read as "what that
    # stage did here", so it runs the version the run pinned. With no resolvable
    # version it refuses: executing the working copy would answer a question
    # nobody asked, under the label of this run.
    pinned = run_service.load_pinned_stage_def(project, manifest, stage_id)
    if pinned.error is not None:
        return JSONResponse({"ok": False, "error": pinned.error}, status_code=409)
    workflow_stage = pinned.workflow_stage
    if workflow_stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}'")

    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])
    }

    try:
        result = run_stage_preview(
            workflow_stage=workflow_stage,
            run_dir=run_dir,
            repo_root=REPO_ROOT,
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

    return JSONResponse({
        "ok": True,
        "columns": list(result.frame.columns),
        "rows_total": int(len(result.frame)),
        "input_rows": result.input_rows,
        "selected_indices": result.selected_indices,
        # rows_total above is the whole scratch frame; this is the window drawn.
        # A fan-out stage can return far more rows than it was handed.
        "preview": render_cells_as_text(result.frame.head(loading.PREVIEW_ROWS_SHOWN)),
    })
