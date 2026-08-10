from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from app.core.errors import ReviewValidationError
from app.models import Stage, TableSchema
from app.models.stages.human_review_queue import QueueConfig, resolve_queue_config
from app.services import review
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import templates
from app.web.loading import (
    find_stage,
    load_manifest,
    load_queue_fingerprints,
    load_stages,
    queue_snapshot,
    runs_dir,
)
from app.web.queue_view import build_queue_page, find_definition_drift, require_reviewed_column

router = APIRouter()


@router.get("/project/{project}/runs/{run_id}/queue/{stage_id}", response_class=HTMLResponse)
async def queue_page(request: Request, project: str, run_id: str, stage_id: str):
    """Reviewer UI for one queue stage in one run."""
    manifest = load_manifest(runs_dir(project) / run_id)
    stage_def = _require_queue_stage(load_stages(project).stages, stage_id)
    queue = _require_queue_config(stage_def)

    fingerprints = load_queue_fingerprints(project, run_id, stage_id)
    drift = (
        None if fingerprints is None
        else find_definition_drift(stage_def, fingerprints.stage_fingerprint)
    )
    page = build_queue_page(
        project, run_id, stage_def, queue,
        queue_snapshot(project, run_id, stage_id), fingerprints, drift,
    )

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "project": project,
            "crumbs": build_run_child_crumbs(project, run_id, label="Review queue"),
            "run_id": run_id,
            "stage_id": stage_id,
            "stage_def": stage_def,
            "definition_drift": drift,
            "review_notes_column": queue.review_notes_column,
            "page": page,
            "manifest_status": manifest.get("status"),
        },
    )


@router.post("/project/{project}/runs/{run_id}/queue/{stage_id}/decide")
async def queue_decide(
    project: str,
    run_id: str,
    stage_id: str,
    input_fingerprint: str = Form(...),
    reviewer: str = Form(...),
    reviewed_values: str = Form(...),
    prefilled_values: str = Form(...),
    review_notes: str | None = Form(None),
):
    # Persist a reviewer's decision as a `StageCacheEntry` keyed by this stage's
    # definition fingerprint and this row's `input_fingerprint`. `reviewed_values` and
    # `prefilled_values` are JSON objects keyed by reviewed TARGET column name — what the
    # reviewer submitted, and what the page they submitted from had pre-filled. The
    # verdict follows from the two, so the reviewer chooses none. The row is resolved by
    # POSITION in the halted-queue sidecar's fingerprint list — never recomputed from live
    # stages — so a fingerprint the sidecar can't vouch for 404s rather than being
    # trusted.
    stage_def = _require_queue_stage(load_stages(project).stages, stage_id)
    queue = _require_queue_config(stage_def)
    attributed_to = _require_reviewer_name(reviewer)
    supplied = _parse_posted_values(reviewed_values, "reviewed_values")
    prefilled = _parse_posted_values(prefilled_values, "prefilled_values")
    stage_fingerprint, row = _resolve_queue_row(project, run_id, stage_id, input_fingerprint)
    _validate_stage_definition_unchanged(stage_def, stage_fingerprint)
    try:
        verdict = review.resolve_verdict(supplied, prefilled)
        review.record_decision(
            project=project, stage=stage_def,
            stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
            frozen_row={str(k): v for k, v in row.items()},
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


# --- stage lookup, shared by both routes ---------------------------------------


def _require_queue_stage(stages: list[Stage], stage_id: str) -> Stage:
    stage_def = find_stage(stages, stage_id)
    if stage_def is None or stage_def.type != "human_review_queue":
        raise HTTPException(status_code=404, detail=f"No queue stage '{stage_id}'")
    return stage_def


def _require_queue_config(stage_def: Stage) -> QueueConfig:
    queue = resolve_queue_config(stage_def)
    assert queue is not None  # _require_queue_stage admits only human_review_queue
    return queue


def _validate_stage_definition_unchanged(stage_def: Stage, halted_fingerprint: str) -> None:
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
    """A posted JSON value map as its form controls carry it, keyed by reviewed TARGET column."""
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
    """JSON null and blank alike become None — never assumed to be an allowed null."""
    # None validates as a null only where the column declares one.
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
    """An HTML form posts an untouched notes box as "": blank means no note, not an empty one."""
    stripped = (review_notes or "").strip()
    return stripped or None


def _validate_reviewed_values(
    stage_def: Stage, queue: QueueConfig, supplied: Mapping[str, str | None]
) -> dict[str, object]:
    """Each supplied value validated against its target column's whole declaration."""
    # Type, nullability, enum vocabulary and numeric range alike, by compiling those
    # columns to a Pydantic model. A key the stage does not declare passes through
    # untouched: the review service owns the exactly-the-declared-columns rule, and
    # duplicating it here would give it two places to drift.
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
    project: str, run_id: str, stage_id: str, input_fingerprint: str
) -> tuple[str, pd.Series]:
    # The `(stage_fingerprint, row)` a decision names: `input_fingerprint`'s POSITION in
    # the sidecar's `input_fingerprints` list, read off the same position in the halted-
    # queue snapshot — the only source a decision's fingerprints may come from. 404 if
    # there's no snapshot/sidecar for this stage, or no position matches: never trust a
    # fingerprint the sidecar can't vouch for.
    fingerprints = load_queue_fingerprints(project, run_id, stage_id)
    snapshot = queue_snapshot(project, run_id, stage_id)
    if fingerprints is not None and snapshot is not None:
        if input_fingerprint in fingerprints.input_fingerprints:
            position = fingerprints.input_fingerprints.index(input_fingerprint)
            if position < len(snapshot):
                row = snapshot.iloc[position]
                assert isinstance(row, pd.Series)
                return fingerprints.stage_fingerprint, row
    raise HTTPException(
        status_code=404,
        detail=f"No queued row with input_fingerprint '{input_fingerprint}'",
    )
