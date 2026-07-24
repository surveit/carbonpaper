"""Human-review queue: render the reviewer UI for one queue stage (recovering
the model input so the score is reviewable) and persist reviewer decisions
into the stage-result cache (app.core.stage_cache)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.errors import ReviewValidationError
from app.core.run_status import RunMode
from app.models import RowReviewDecision, Stage
from app.runtime.llm import render_prompt
from app.services import review
from app.core.stage_cache import StageCacheEntry
from app.web.config import templates
from app.web.loading import (
    QueueFingerprints,
    display_cell,
    find_stage,
    load_manifest,
    load_queue_fingerprints,
    load_stages,
    queue_snapshot,
    read_table,
    runs_dir,
)

router = APIRouter()


@dataclass(frozen=True)
class _DecisionDisplay:
    """One recorded reviewer decision, shaped for the queue template: the verdict
    label, the reviewer-entered score (only for a `modify`), and who reviewed it
    when. A tombstone entry carries no reviewer metadata, so those are None."""

    decision: str
    modified_score: float | None
    reviewer: str | None
    reviewed_at: str | None


@router.get("/project/{project}/runs/{run_id}/queue/{stage_id}", response_class=HTMLResponse)
async def queue_page(request: Request, project: str, run_id: str, stage_id: str):
    """Reviewer UI for one queue stage in one run."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)

    stages = load_stages(project).stages
    stage_def = find_stage(stages, stage_id)
    if stage_def is None or stage_def.type != "human_review_queue":
        raise HTTPException(status_code=404, detail=f"No queue stage '{stage_id}'")

    snapshot = queue_snapshot(project, run_id, stage_id)
    fingerprints = load_queue_fingerprints(project, run_id, stage_id)
    entries_by_fingerprint = (
        _load_decided_entries(project, stage_id, fingerprints.stage_fingerprint)
        if fingerprints else {}
    )
    input_lookup, join_keys, prompt_template = _load_model_input_lookup(
        stage_def, stages, manifest, run_dir
    )
    items = _build_review_items(
        snapshot, fingerprints, entries_by_fingerprint, input_lookup, join_keys, prompt_template
    )

    reviewed_count = sum(1 for i in items if i["prior_decision"] is not None)
    total = len(items)

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "project": project,
            "run_id": run_id,
            "stage_id": stage_id,
            "stage_def": stage_def,
            "items": items,
            "reviewed_count": reviewed_count,
            "total": total,
            "all_reviewed": total > 0 and reviewed_count == total,
            "manifest_status": manifest.get("status"),
        },
    )


@router.post("/project/{project}/runs/{run_id}/queue/{stage_id}/decide")
async def queue_decide(
    project: str,
    run_id: str,
    stage_id: str,
    input_fingerprint: str = Form(...),
    decision: RowReviewDecision = Form(...),
    modified_score: float | None = Form(None),
):
    """Persist a reviewer's decision as a `StageCacheEntry` keyed by this
    stage's definition fingerprint and this row's `input_fingerprint`. FastAPI
    coerces and 422s a malformed `decision`/`modified_score`; the row is
    resolved by POSITION in the halted-queue sidecar's fingerprint list — never
    recomputed from live stages — so a fingerprint the sidecar can't vouch for
    404s rather than being trusted."""
    stage_fingerprint, row = _resolve_queue_row(project, run_id, stage_id, input_fingerprint)
    try:
        review.record_decision(
            project=project, stage_id=stage_id,
            stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
            frozen_row={str(k): v for k, v in row.items()},
            verdict=decision, modified_score=modified_score,
            reviewer="local", reviewed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except ReviewValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(
        {"ok": True, "input_fingerprint": input_fingerprint, "decision": decision.value}
    )


# --- queue_decide helpers ------------------------------------------------------


def _resolve_queue_row(
    project: str, run_id: str, stage_id: str, input_fingerprint: str
) -> tuple[str, pd.Series]:
    """The `(stage_fingerprint, row)` a decision names: `input_fingerprint`'s
    POSITION in the sidecar's `input_fingerprints` list, read off the same
    position in the halted-queue snapshot — the only source a decision's
    fingerprints may come from. 404 if there's no snapshot/sidecar for this
    stage, or no position matches: never trust a fingerprint the sidecar
    can't vouch for."""
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


# --- queue_page helpers -------------------------------------------------------


def _load_decided_entries(
    project: str, stage_id: str, stage_fingerprint: str
) -> dict[str, StageCacheEntry]:
    """Cached decisions for this stage definition, keyed by `input_fingerprint`:
    the production cache's entries for (project, stage, stage_fingerprint)."""
    entries = StageCacheEntry.for_mode(RunMode.PRODUCTION).find_entries(
        project, stage_id, stage_fingerprint
    )
    return {entry.input_fingerprint: entry for entry in entries}


def _display_decision(entry: StageCacheEntry) -> _DecisionDisplay:
    """The reviewer decision one cached entry records, shaped for the queue
    template. A tombstone (`output_row is None`) is a `reject`; otherwise the
    verdict and reviewer metadata are read off the stage output columns the
    entry carries, and the modified score is shown only for a `modify`."""
    output = entry.output_row
    if output is None:
        return _DecisionDisplay(decision="reject", modified_score=None, reviewer=None, reviewed_at=None)
    decision = output["decision"]
    return _DecisionDisplay(
        decision=decision,
        modified_score=output["final_score"] if decision == "modify" else None,
        reviewer=output["reviewer_id"],
        reviewed_at=output["reviewed_at"],
    )


def _load_scored_stage(stages: list[Stage], stage_def: Stage) -> Stage | None:
    """The upstream stage whose OUTPUT this queue stage reviews — stage_def's
    declared input, or None if it declares none."""
    scored_ids = stage_def.input_ids
    return find_stage(stages, scored_ids[0]) if scored_ids else None


def _resolve_prompt_template(scored_def: Stage | None) -> str | None:
    return scored_def.llm.prompt_data_template if scored_def and scored_def.llm else None


def _read_table_or_none(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        return read_table(path)
    except Exception:  # noqa: BLE001
        return None


def _resolve_scored_input_frame(
    scored_def: Stage, manifest: dict[str, Any], run_dir: Path
) -> tuple[pd.DataFrame | None, list[str] | None]:
    """The scored stage's OWN input — DataFrame plus declared primary key — the
    frame the queue snapshot needs to join back against to recover the model
    input, or (None, pk) if that stage's output isn't on disk."""
    output_by_id = {s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])}
    scored_in_id = scored_def.input_ids[0]
    scored_in = scored_def.inputs[0] if scored_def.inputs else None
    pk = scored_in.table_schema.primary_key if scored_in and scored_in.table_schema else None
    in_path = output_by_id.get(scored_in_id)
    in_df = _read_table_or_none(run_dir / in_path) if in_path else None
    return in_df, pk


def _find_join_keys(primary_key: list[str] | None, columns: list[str]) -> list[str]:
    """Columns to join the queue snapshot back to the scored stage's input on:
    the declared primary key restricted to columns actually present, or a
    handful of common id-like column names as a fallback."""
    return [k for k in (primary_key or []) if k in columns] or \
        [c for c in ("evidence_id", "entity_id", "doc_id", "id") if c in columns]


def _index_rows_by_join_key(df: pd.DataFrame, join_keys: list[str]) -> dict[tuple[str, ...], dict[str, Any]]:
    return {
        tuple(str(r[k]) for k in join_keys): {str(k): display_cell(v) for k, v in r.items()}
        for _, r in df.iterrows()
    }


def _load_model_input_lookup(
    stage_def: Stage, stages: list[Stage], manifest: dict[str, Any], run_dir: Path
) -> tuple[dict[tuple[str, ...], dict[str, Any]], list[str], str | None]:
    """Recover the MODEL INPUT so the score is reviewable, not just visible.
    The queue snapshot holds the scoring stage's OUTPUT (score + reasoning + ids);
    the thing the model actually judged (the quote, the benchmark) lives in the
    scoring stage's INPUT, one stage upstream. Join it back + resolve the prompt
    template it was scored with."""
    scored_def = _load_scored_stage(stages, stage_def)
    prompt_template = _resolve_prompt_template(scored_def)

    input_lookup: dict[tuple[str, ...], dict[str, Any]] = {}
    join_keys: list[str] = []
    if scored_def and scored_def.input_ids:
        in_df, pk = _resolve_scored_input_frame(scored_def, manifest, run_dir)
        if in_df is not None:
            join_keys = _find_join_keys(pk, list(in_df.columns))
            if join_keys:
                input_lookup = _index_rows_by_join_key(in_df, join_keys)
    return input_lookup, join_keys, prompt_template


def _find_model_input(
    row: pd.Series, input_lookup: dict[tuple[str, ...], dict[str, Any]], join_keys: list[str]
) -> dict[str, Any] | None:
    if input_lookup and join_keys and all(k in row.index for k in join_keys):
        return input_lookup.get(tuple(str(row[k]) for k in join_keys))
    return None


def _render_model_prompt(model_input: dict[str, Any] | None, prompt_template: str | None) -> str | None:
    if not model_input or not prompt_template:
        return None
    try:
        return render_prompt(prompt_template, model_input)
    except Exception:  # noqa: BLE001
        return None


def _build_review_item(
    row: pd.Series,
    input_fingerprint: str,
    entries_by_fingerprint: dict[str, StageCacheEntry],
    input_lookup: dict[tuple[str, ...], dict[str, Any]],
    join_keys: list[str],
    prompt_template: str | None,
) -> dict[str, Any]:
    entry = entries_by_fingerprint.get(input_fingerprint)
    model_input = _find_model_input(row, input_lookup, join_keys)
    return {
        "input_fingerprint": input_fingerprint,
        "row": {k: display_cell(v) for k, v in row.items()},
        "model_input": model_input,
        "rendered_prompt": _render_model_prompt(model_input, prompt_template),
        "prior_decision": _display_decision(entry) if entry is not None else None,
    }


def _build_review_items(
    snapshot: pd.DataFrame | None,
    fingerprints: QueueFingerprints | None,
    entries_by_fingerprint: dict[str, StageCacheEntry],
    input_lookup: dict[tuple[str, ...], dict[str, Any]],
    join_keys: list[str],
    prompt_template: str | None,
) -> list[dict[str, Any]]:
    """One review item per snapshot row, zipped POSITIONALLY with the
    sidecar's `input_fingerprints` — the two lists are index-independent
    (the snapshot carries no fingerprint column), so position is the only
    correspondence between them."""
    if snapshot is None or fingerprints is None:
        return []
    return [
        _build_review_item(row, fp, entries_by_fingerprint, input_lookup, join_keys, prompt_template)
        for (_, row), fp in zip(snapshot.iterrows(), fingerprints.input_fingerprints)
    ]
