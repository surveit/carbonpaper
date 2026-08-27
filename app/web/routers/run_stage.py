"""One stage inside one run: the per-stage detail panel, its full-rows table and
CSV download, and the simulate page plus the in-memory re-run it posts to. Split
out of app.web.routers.runs, which holds the run-level lifecycle routes and sat
at the import-graph fan-out ceiling."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from app.services.loader import resolve_function_code
from app.services import run as run_service
from app.runtime.errors import PreviewError
from app.runtime.preview import PREVIEWABLE_TYPES, run_stage_preview
from app.web import loading
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.column_order import order_preview_columns, order_written_columns_first
from app.web.config import EVENT_TAIL, label_stage_type, templates
from app.web.eval_coverage import find_eval_coverages
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH
from app.services.workspace import resolve_run_dir
from app.web.loading import (
    build_llm_example,
    csv_download_body,
    load_manifest,
    load_output_preview,
    load_output_table,
    manifest_stage,
    read_output_df,
    render_cells_as_text,
)
from app.web.panel_links import RectangleRequest, read_rectangle_query
from app.web.run_stage_panel import not_executed_panel, resolve_panel_links
from app.web.stage_diff import StageDiff, build_stage_diff

router = APIRouter()


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/partial",
    response_class=HTMLResponse,
)
async def run_stage_partial(
    request: Request, project_id: str, run_id: str, stage_id: str
):
    run_dir = resolve_run_dir(project_id, run_id)
    manifest = load_manifest(project_id, run_id)
    stage_record = next(
        (s for s in manifest.get("stage_records", []) if s.get("stage_id") == stage_id),
        None,
    )

    # The panel's Schema tier and Transform detail describe what THIS run
    # executed, so they read the version it pinned. With no resolvable version
    # there is no stage definition to show and the panel says why.
    pinned = run_service.load_pinned_stage_def(project_id, manifest, stage_id)
    stage_def = None if pinned.workflow_stage is None else pinned.workflow_stage.stage
    if stage_record is None:
        # A stage the graph draws but this run never executed (a workflow test
        # injects its input stages) — see app.web.run_stage_panel.
        return not_executed_panel(request, project_id, run_id, manifest, stage_id, pinned)

    # The stage's own columns lead; its inputs are drawn as the upstream stage
    # wrote them, since nothing on that frame is this stage's work.
    output_preview = order_preview_columns(
        load_output_preview(run_dir, stage_record.get("output_path")), pinned.workflow_stage)
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
            "project": project_id,
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
            # Judged against the version THIS run pinned, so each verdict is about the
            # code that produced this run's rows. Empty where no eval targets the stage.
            "eval_coverages": find_eval_coverages(
                project_id, stage_id, manifest.get("workflow_version")),
            "previewable": stage_def is not None and stage_def.type in PREVIEWABLE_TYPES,
            "links": resolve_panel_links(project_id, run_id),
            "event_tail": EVENT_TAIL,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/rows",
    response_class=HTMLResponse,
)
async def run_stage_rows(
    request: Request, project_id: str, run_id: str, stage_id: str, raw: bool = False,
    ordinals: str | None = None, rows: str | None = None,
    columns: list[str] | None = Query(default=None),
):
    run_dir = resolve_run_dir(project_id, run_id)
    manifest = load_manifest(project_id, run_id)
    stage_record = manifest_stage(project_id, run_id, stage_id)
    pinned = run_service.load_pinned_stage_def(project_id, manifest, stage_id)
    requested = _read_rectangle(ordinals, rows, columns)
    selected = _parse_ordinals(ordinals)
    if requested is not None:
        table = loading.load_output_rectangle(
            run_dir, stage_record.get("output_path"), requested)
    elif selected is not None:
        table = loading.load_selected_output_rows(
            run_dir, stage_record.get("output_path"), selected)
    else:
        table = load_output_table(run_dir, stage_record.get("output_path"))
    if requested is None:
        # A rectangle's column order is the one it was published in, not the stage's.
        table["columns"] = order_written_columns_first(pinned.workflow_stage, table["columns"])
    return templates.TemplateResponse(
        request,
        "run_stage_rows.html",
        {
            "project": project_id,
            "crumbs": build_run_child_crumbs(project_id, run_id, label=f"{stage_id} rows"),
            "run_id": run_id,
            "stage_id": stage_id,
            "stage": stage_record,
            "output_path": stage_record.get("output_path"),
            # Built even under ?raw=1: the raw view offers the diff view only
            # where one actually exists, so it has to know either way.
            # No diff over a filtered view: the diff aligns output rows to input
            # rows by position, which a subset cannot honour.
            "diff": (
                None if selected is not None or requested is not None
                else _build_full_rows_diff(manifest, pinned, run_dir, stage_record)
            ),
            "raw": raw,
            "links": resolve_panel_links(project_id, run_id),
            # The page's own treatments (row numbers, click-to-expand cells,
            # sticky-header scroll box) the shared diff partial renders on request.
            "full_rows": True,
            **table,
        },
    )


def _read_rectangle(
    ordinals: str | None, rows: str | None, columns: list[str] | None
) -> RectangleRequest | None:
    """Both name rows, so asking with both would leave which one won unstated."""
    if ordinals is not None and (rows is not None or columns):
        raise HTTPException(
            status_code=400,
            detail="Pass `ordinals` for named rows or `rows`/`columns` for a rectangle, "
                   "not both",
        )
    try:
        return read_rectangle_query(rows, columns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    manifest: dict[str, Any], pinned: run_service.RunStageDef,
    run_dir: Path, stage_record: dict[str, Any],
) -> StageDiff | None:
    return build_stage_diff(
        pinned.workflow_stage,
        run_dir,
        stage_record.get("output_path"),
        {s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])},
        rows_shown=loading.MAX_TABLE_ROWS,
    )


@router.get("/project/{project_id}/runs/{run_id}/stage/{stage_id}/rows.csv")
async def run_stage_rows_csv(
    project_id: str, run_id: str, stage_id: str, rows: str | None = None,
    columns: list[str] | None = Query(default=None),
):
    run_dir = resolve_run_dir(project_id, run_id)
    stage_record = manifest_stage(project_id, run_id, stage_id)
    requested = _read_rectangle(None, rows, columns)
    output_path = stage_record.get("output_path")
    body = (
        csv_download_body(read_output_df(run_dir, output_path)) if requested is None
        else loading.load_rectangle_csv_body(run_dir, output_path, requested)
    )
    filename = f"{project_id}__{run_id}__{stage_id}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/simulate",
    response_class=HTMLResponse,
)
async def run_stage_simulate(
    request: Request, project_id: str, run_id: str, stage_id: str
):
    run_dir = resolve_run_dir(project_id, run_id)
    manifest = load_manifest(project_id, run_id)
    # The page executes a stage under this run's name, so it offers only what the
    # run pinned. No resolvable version, or a type the runner cannot preview, and
    # there is nothing here to simulate — the panel links no page in either case.
    pinned = run_service.load_pinned_stage_def(project_id, manifest, stage_id)
    stage_def = None if pinned.workflow_stage is None else pinned.workflow_stage.stage
    if stage_def is None:
        raise HTTPException(
            status_code=404, detail=pinned.error or f"No stage '{stage_id}' in run"
        )
    if stage_def.type not in PREVIEWABLE_TYPES:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{label_stage_type(stage_def.type)} stages cannot be run one row at a time"
            ),
        )

    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])
    }
    return templates.TemplateResponse(
        request,
        "run_stage_simulate.html",
        {
            "project": project_id,
            "run_id": run_id,
            "stage_id": stage_id,
            "crumbs": build_run_child_crumbs(
                project_id, run_id, label=f"simulate {stage_id}"
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


@router.post("/project/{project_id}/runs/{run_id}/stage/{stage_id}/preview")
async def run_stage_scratch_preview(
    request: Request, project_id: str, run_id: str, stage_id: str
):
    run_dir = resolve_run_dir(project_id, run_id)
    manifest = load_manifest(project_id, run_id)

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
    pinned = run_service.load_pinned_stage_def(project_id, manifest, stage_id)
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
