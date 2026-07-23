"""Human-review queue: render the reviewer UI for one queue stage (recovering
the model input so the score is reviewable) and persist reviewer decisions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.models import RowReviewDecision, Stage
from app.runtime.llm import render_prompt
from app.web.config import templates
from app.web.loading import (
    decisions_path,
    display_cell,
    find_stage,
    load_decisions_df,
    load_manifest,
    load_stages,
    queue_snapshot,
    read_table,
    runs_dir,
)

router = APIRouter()


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
    decision_by_hash = _index_decisions_by_hash(load_decisions_df(project, stage_id))
    input_lookup, join_keys, prompt_template = _load_model_input_lookup(
        stage_def, stages, manifest, run_dir
    )
    items = _build_review_items(snapshot, decision_by_hash, input_lookup, join_keys, prompt_template)

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
    content_hash: str = Form(...),
    decision: str = Form(...),
    modified_score: str | None = Form(None),
):
    """Persist a reviewer's decision against a content_hash."""
    if decision not in (RowReviewDecision.approve, RowReviewDecision.reject,
                        RowReviewDecision.modify):
        raise HTTPException(status_code=400, detail=f"unknown decision '{decision}'")
    mod_val: float | None = None
    if decision == RowReviewDecision.modify:
        if not modified_score:
            raise HTTPException(status_code=400, detail="modify requires modified_score")
        try:
            mod_val = float(modified_score)
        except ValueError:
            raise HTTPException(status_code=400, detail="modified_score must be numeric")

    df = load_decisions_df(project, stage_id)
    df = df[df["content_hash"] != content_hash]  # upsert: drop prior row if any
    new_row = {
        "content_hash": content_hash,
        "decision": decision,
        "modified_score": mod_val,
        "reviewer": "local",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "source_run_id": run_id,
    }
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df.to_parquet(decisions_path(project, stage_id), index=False)
    return JSONResponse({"ok": True, "content_hash": content_hash, "decision": decision})


# --- queue_page helpers -------------------------------------------------------


def _index_decisions_by_hash(decisions: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Prior reviewer decisions, keyed by the content_hash they were recorded
    against, so a queue item can look up whether it was already decided."""
    decision_by_hash: dict[str, dict[str, Any]] = {}
    if len(decisions):
        for _, row in decisions.iterrows():
            decision_by_hash[row["content_hash"]] = {
                "decision": row.get("decision"),
                "modified_score": row.get("modified_score"),
                "reviewer": row.get("reviewer"),
                "reviewed_at": row.get("reviewed_at"),
            }
    return decision_by_hash


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
    decision_by_hash: dict[str, dict[str, Any]],
    input_lookup: dict[tuple[str, ...], dict[str, Any]],
    join_keys: list[str],
    prompt_template: str | None,
) -> dict[str, Any]:
    h = row["content_hash"]
    model_input = _find_model_input(row, input_lookup, join_keys)
    return {
        "content_hash": h,
        "row": {k: display_cell(v) for k, v in row.items()
                if k not in ("content_hash", "decision", "modified_score",
                             "reviewer", "reviewed_at")},
        "model_input": model_input,
        "rendered_prompt": _render_model_prompt(model_input, prompt_template),
        "prior_decision": decision_by_hash.get(h),
    }


def _build_review_items(
    snapshot: pd.DataFrame | None,
    decision_by_hash: dict[str, dict[str, Any]],
    input_lookup: dict[tuple[str, ...], dict[str, Any]],
    join_keys: list[str],
    prompt_template: str | None,
) -> list[dict[str, Any]]:
    if snapshot is None:
        return []
    return [
        _build_review_item(row, decision_by_hash, input_lookup, join_keys, prompt_template)
        for _, row in snapshot.iterrows()
    ]
