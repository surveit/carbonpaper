"""Human-review queue: render the reviewer UI for one queue stage (recovering
the model input so the score is reviewable) and persist reviewer decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
    decisions = load_decisions_df(project, stage_id)
    decision_by_hash: dict[str, dict[str, Any]] = {}
    if len(decisions):
        for _, row in decisions.iterrows():
            decision_by_hash[row["content_hash"]] = {
                "decision": row.get("decision"),
                "modified_score": row.get("modified_score"),
                "reviewer": row.get("reviewer"),
                "reviewed_at": row.get("reviewed_at"),
            }

    # ── Recover the MODEL INPUT so the score is reviewable, not just visible. ──
    # The queue snapshot holds the scoring stage's OUTPUT (score + reasoning + ids);
    # the thing the model actually judged (the quote, the benchmark) lives in the
    # scoring stage's INPUT, one stage upstream. Join it back + render the prompt.
    output_by_id = {s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])}
    scored_ids = stage_def.input_ids
    scored_def = find_stage(stages, scored_ids[0]) if scored_ids else None
    # Only llm_transform stages carry an `llm` block; read it off the union defensively.
    scored_llm = getattr(scored_def, "llm", None) if scored_def else None
    prompt_template = scored_llm.prompt_template if scored_llm else None

    input_lookup: dict[tuple, dict[str, Any]] = {}
    join_keys: list[str] = []
    if scored_def and scored_def.input_ids:
        scored_in_id = scored_def.input_ids[0]
        scored_in = scored_def.inputs[0] if scored_def.inputs else None
        pk = scored_in.table_schema.primary_key if scored_in and scored_in.table_schema else None
        in_path = output_by_id.get(scored_in_id)
        in_df = None
        if in_path:
            p = run_dir / in_path
            if p.exists():
                try:
                    in_df = read_table(p)
                except Exception:  # noqa: BLE001
                    in_df = None
        if in_df is not None:
            cols = list(in_df.columns)
            join_keys = [k for k in (pk or []) if k in cols] or \
                [c for c in ("evidence_id", "entity_id", "doc_id", "id") if c in cols]
            if join_keys:
                for _, r in in_df.iterrows():
                    key = tuple(str(r[k]) for k in join_keys)
                    input_lookup[key] = {str(k): display_cell(v) for k, v in r.items()}

    items: list[dict[str, Any]] = []
    if snapshot is not None:
        for _, row in snapshot.iterrows():
            h = row["content_hash"]
            existing = decision_by_hash.get(h)
            model_input = None
            rendered_prompt = None
            if input_lookup and join_keys and all(k in row.index for k in join_keys):
                model_input = input_lookup.get(tuple(str(row[k]) for k in join_keys))
                if model_input and prompt_template:
                    try:
                        rendered_prompt = render_prompt(prompt_template, model_input)
                    except Exception:  # noqa: BLE001
                        rendered_prompt = None
            items.append({
                "content_hash": h,
                "row": {k: display_cell(v) for k, v in row.items()
                        if k not in ("content_hash", "decision", "modified_score",
                                     "reviewer", "reviewed_at")},
                "model_input": model_input,
                "rendered_prompt": rendered_prompt,
                "prior_decision": existing,
            })

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
    if decision not in ("approve", "reject", "modify"):
        raise HTTPException(status_code=400, detail=f"unknown decision '{decision}'")
    mod_val: float | None = None
    if decision == "modify":
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
