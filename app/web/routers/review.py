from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from app.core.errors import ReviewValidationError
from app.core.stage_cache import compute_row_fingerprint
from app.models import TableSchema, Workflow, WorkflowNotFormed, WorkflowStage
from app.models.stages.human_review_queue import QueueConfig, resolve_queue_config
from app.services import review
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import templates
from app.web.loading import (
    find_workflow_stage,
    load_queue_fingerprints,
    load_run_record,
    load_stages,
    queue_snapshot,
    queue_snapshot_rows,
)
from app.web.queue_view import (
    QueuePage,
    build_queue_page,
    find_definition_drift,
    find_positioned_item,
    find_review_closed_note,
    require_reviewed_column,
)

router = APIRouter()


@router.get("/project/{project_id}/runs/{run_id}/queue/{stage_id}", response_class=HTMLResponse)
async def queue_page(request: Request, project_id: str, run_id: str, stage_id: str):
    stage_def = _require_queue_stage(load_stages(project_id).workflow, stage_id)
    queue = _require_queue_config(stage_def)
    drift, page = _build_page(project_id, run_id, stage_id, stage_def, queue)

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "project": project_id,
            "crumbs": build_run_child_crumbs(project_id, run_id, label="Review queue"),
            "run_id": run_id,
            "stage_id": stage_id,
            "stage_def": stage_def.stage,
            "definition_drift": drift,
            "review_notes_column": queue.review_notes_column,
            "page": page,
        },
    )


@router.get(
    "/project/{project_id}/runs/{run_id}/queue/{stage_id}/card/{input_fingerprint}",
    response_class=HTMLResponse,
)
async def queue_card_partial(
    request: Request, project_id: str, run_id: str, stage_id: str, input_fingerprint: str
):
    stage_def = _require_queue_stage(load_stages(project_id).workflow, stage_id)
    queue = _require_queue_config(stage_def)
    _drift, page = _build_page(project_id, run_id, stage_id, stage_def, queue)
    positioned = find_positioned_item(page, input_fingerprint)
    if positioned is None:
        raise HTTPException(
            status_code=404,
            detail=f"No queued row with input_fingerprint '{input_fingerprint}'",
        )
    return templates.TemplateResponse(
        request,
        "_queue_card.html",
        {
            "project": project_id,
            "run_id": run_id,
            "stage_id": stage_id,
            "review_notes_column": queue.review_notes_column,
            "page": page,
            "item": positioned.item,
            "row_position": positioned.row_position,
        },
    )


@router.post("/project/{project_id}/runs/{run_id}/queue/{stage_id}/decide")
async def queue_decide(
    project_id: str,
    run_id: str,
    stage_id: str,
    input_fingerprint: str = Form(...),
    reviewer: str = Form(...),
    reviewed_values: str = Form(...),
    prefilled_values: str = Form(...),
    review_notes: str | None = Form(None),
):
    stage_def = _require_queue_stage(load_stages(project_id).workflow, stage_id)
    queue = _require_queue_config(stage_def)
    _refuse_a_closed_queue(project_id, run_id, stage_id)
    attributed_to = _require_reviewer_name(reviewer)
    supplied = _parse_posted_values(reviewed_values, "reviewed_values")
    prefilled = _parse_posted_values(prefilled_values, "prefilled_values")
    stage_fingerprint, row = _resolve_queue_row(project_id, run_id, stage_id, input_fingerprint)
    _validate_stage_definition_unchanged(stage_def, stage_fingerprint)
    try:
        verdict = review.resolve_verdict(supplied, prefilled)
        review.record_decision(
            project_id=project_id, stage=stage_def,
            stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
            frozen_row=row,
            verdict=verdict,
            reviewed_values=_validate_reviewed_values(stage_def, queue, supplied),
            review_notes=_normalise_review_notes(review_notes),
            reviewer=attributed_to,
            reviewed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except ReviewValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(
        {"ok": True, "input_fingerprint": input_fingerprint, "verdict": verdict.value}
    )


# --- the view model, shared by the page and the single-card routes -------------


def _build_page(
    project_id: str, run_id: str, stage_id: str, stage_def: WorkflowStage, queue: QueueConfig
) -> tuple[str | None, QueuePage]:
    """Returns the drift message beside the page: a drifted queue renders no items."""
    fingerprints = load_queue_fingerprints(project_id, run_id, stage_id)
    drift = (
        None if fingerprints is None
        else find_definition_drift(stage_def, fingerprints.stage_fingerprint)
    )
    page = build_queue_page(
        project_id, run_id, stage_def, queue,
        queue_snapshot(project_id, run_id, stage_id), fingerprints, drift,
        _find_closed_note(project_id, run_id, stage_id),
    )
    return drift, page


def _find_closed_note(project_id: str, run_id: str, stage_id: str) -> str | None:
    """The one authority on whether this queue still takes decisions: the run's own record."""
    return find_review_closed_note(
        load_run_record(project_id, run_id).find_stage_record(stage_id))


# --- stage lookup, shared by every route ---------------------------------------


def _require_queue_stage(
    workflow: Workflow | WorkflowNotFormed, stage_id: str
) -> WorkflowStage:
    workflow_stage = find_workflow_stage(workflow, stage_id)
    if workflow_stage is None or workflow_stage.stage.type != "human_review_queue":
        raise HTTPException(status_code=404, detail=f"No queue stage '{stage_id}'")
    return workflow_stage


def _require_queue_config(stage_def: WorkflowStage) -> QueueConfig:
    queue = resolve_queue_config(stage_def.stage)
    assert queue is not None  # _require_queue_stage admits only human_review_queue
    return queue


def _refuse_a_closed_queue(project_id: str, run_id: str, stage_id: str) -> None:
    """A decision recorded now would be cached against rows this run already emitted."""
    closed = _find_closed_note(project_id, run_id, stage_id)
    if closed is not None:
        raise HTTPException(status_code=409, detail=closed)


def _validate_stage_definition_unchanged(
    stage_def: WorkflowStage, halted_fingerprint: str
) -> None:
    drift = find_definition_drift(stage_def, halted_fingerprint)
    if drift is not None:
        raise HTTPException(status_code=409, detail=drift)


# --- the posted form ----------------------------------------------------------


def _require_reviewer_name(reviewer: str) -> str:
    name = reviewer.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail="reviewer must be a non-blank name: no decision is recorded unattributed",
        )
    return name


def _parse_posted_values(raw: str, field: str) -> dict[str, str | None]:
    """Keyed by reviewed TARGET column name, not the source column it was read from."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be a JSON object, got {type(parsed).__name__}",
        )
    return {str(name): _as_posted_text(value) for name, value in parsed.items()}


def _as_posted_text(value: object) -> str | None:
    """Blank and JSON null both become None; whether a null is allowed is the column's call."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip() or None
    raise HTTPException(
        status_code=400,
        detail=(
            "a reviewed value must be a JSON string, number, boolean or null, got "
            f"{type(value).__name__}"
        ),
    )


def _normalise_review_notes(review_notes: str | None) -> str | None:
    stripped = (review_notes or "").strip()
    return stripped or None


def _validate_reviewed_values(
    stage_def: WorkflowStage, queue: QueueConfig, supplied: Mapping[str, str | None]
) -> dict[str, object]:
    """A key the stage does not declare passes through untouched — the review service owns that."""
    declared = {
        target: require_reviewed_column(stage_def, target)
        for target in queue.reviewed_columns.values()
        if target in supplied
    }
    if not declared:
        return dict(supplied)
    model = TableSchema(columns=list(declared.values())).to_pydantic_model(
        f"{stage_def.id}_reviewed"
    )
    try:
        validated = model.model_validate({target: supplied[target] for target in declared})
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_describe_rejections(exc)) from exc
    return {**supplied, **validated.model_dump()}


def _describe_rejections(exc: ValidationError) -> str:
    return "; ".join(
        f"column {'.'.join(str(part) for part in error['loc'])!r}: "
        f"{error['msg']} (got {error['input']!r})"
        for error in exc.errors()
    )


def _resolve_queue_row(
    project_id: str, run_id: str, stage_id: str, input_fingerprint: str
) -> tuple[str, dict[str, object]]:
    """The row a decision is filed against, checked to be the row that fingerprint names."""
    fingerprints = load_queue_fingerprints(project_id, run_id, stage_id)
    # Not `queue_snapshot`: that pair is for presentation and renders a list cell
    # through numpy, where the run held — and hashed — a list.
    rows = queue_snapshot_rows(project_id, run_id, stage_id)
    if fingerprints is not None and rows is not None:
        if input_fingerprint in fingerprints.input_fingerprints:
            position = fingerprints.input_fingerprints.index(input_fingerprint)
            if position < len(rows):
                row = rows[position]
                _require_row_matches_its_fingerprint(row, input_fingerprint, position, stage_id)
                return fingerprints.stage_fingerprint, row
    raise HTTPException(
        status_code=404,
        detail=f"No queued row with input_fingerprint '{input_fingerprint}'",
    )


def _require_row_matches_its_fingerprint(
    row: Mapping[str, object], input_fingerprint: str, position: int, stage_id: str
) -> None:
    """The snapshot and its sidecar are two files; only this makes their alignment a fact."""
    recomputed = compute_row_fingerprint(row)
    if recomputed != input_fingerprint:
        raise ValueError(
            f"queue snapshot for stage '{stage_id}' disagrees with its fingerprint sidecar: "
            f"the row at position {position} hashes to {recomputed}, but the sidecar files it "
            f"under {input_fingerprint}. A decision recorded here would be attributed to a row "
            "the reviewer was not shown."
        )
