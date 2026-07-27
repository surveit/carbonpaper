"""Human-review queue: render the reviewer UI for one queue stage (recovering
the model input so the AI's values are reviewable) and persist reviewer
decisions into the stage-result cache (app.core.stage_cache)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.errors import ReviewValidationError
from app.models import Column, QueueConfig, ReviewVerdict, Stage
from app.models.stages.shared import resolve_input_schema
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
    verdict: str
    reviewed_values: dict[str, object]
    review_notes: str | None
    reviewer: str
    reviewed_at: str


@dataclass(frozen=True)
class _ReviewedField:
    """One reviewed column as the form renders it: `source` is the column the AI
    produced, `target` the column the reviewer's value lands in. `control` is the
    HTML input `type` verbatim, or "select", which the template renders as a
    `<select>` over `options`."""

    source: str
    target: str
    control: str
    nullable: bool
    step: str | None
    minimum: float | None
    maximum: float | None
    options: list[str] | None


@router.get("/project/{project}/runs/{run_id}/queue/{stage_id}", response_class=HTMLResponse)
async def queue_page(request: Request, project: str, run_id: str, stage_id: str):
    """Reviewer UI for one queue stage in one run."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)

    stages = load_stages(project).stages
    stage_def = _require_queue_stage(stages, stage_id)
    queue = _require_queue_config(stage_def)

    fingerprints = load_queue_fingerprints(project, run_id, stage_id)
    drift = (
        _find_definition_drift(stage_def, fingerprints.stage_fingerprint)
        if fingerprints is not None else None
    )
    fields = [] if drift else _build_reviewed_fields(stage_def, queue)
    items: list[dict[str, Any]] = []
    if fingerprints is not None and drift is None:
        input_lookup, join_keys, prompt_template = _load_model_input_lookup(
            stage_def, stages, manifest, run_dir
        )
        items = _build_review_items(
            queue_snapshot(project, run_id, stage_id), fingerprints,
            _load_decided_entries(project, stage_id, fingerprints.stage_fingerprint),
            queue, fields, input_lookup, join_keys, prompt_template,
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
            "definition_drift": drift,
            "reviewed_fields": fields,
            "review_notes_column": queue.review_notes_column,
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
    verdict: ReviewVerdict = Form(...),
    reviewer: str = Form(...),
    reviewed_values: str = Form(...),
    review_notes: str | None = Form(None),
):
    """Persist a reviewer's decision as a `StageCacheEntry` keyed by this
    stage's definition fingerprint and this row's `input_fingerprint`.
    `reviewed_values` is a JSON object keyed by reviewed TARGET column name,
    each value the reviewer's raw form text. The row is resolved by POSITION in
    the halted-queue sidecar's fingerprint list — never recomputed from live
    stages — so a fingerprint the sidecar can't vouch for 404s rather than being
    trusted."""
    stage_def = _require_queue_stage(load_stages(project).stages, stage_id)
    queue = _require_queue_config(stage_def)
    attributed_to = _require_reviewer_name(reviewer)
    supplied = _parse_reviewed_values(reviewed_values)
    stage_fingerprint, row = _resolve_queue_row(project, run_id, stage_id, input_fingerprint)
    _validate_stage_definition_unchanged(stage_def, stage_fingerprint)
    try:
        review.record_decision(
            project=project, stage=stage_def,
            stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
            frozen_row={str(k): v for k, v in row.items()},
            verdict=verdict,
            reviewed_values=_coerce_reviewed_values(stage_def, queue, supplied),
            review_notes=_normalise_review_notes(review_notes),
            reviewer=attributed_to,
            reviewed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except ReviewValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(
        {"ok": True, "input_fingerprint": input_fingerprint, "verdict": verdict.value}
    )


# --- shared by both routes -----------------------------------------------------


def _require_queue_stage(stages: list[Stage], stage_id: str) -> Stage:
    stage_def = find_stage(stages, stage_id)
    if stage_def is None or stage_def.type != "human_review_queue":
        raise HTTPException(status_code=404, detail=f"No queue stage '{stage_id}'")
    return stage_def


def _require_queue_config(stage_def: Stage) -> QueueConfig:
    queue = stage_def.queue
    assert queue is not None  # Stage._handle_for_type: human_review_queue carries queue
    return queue


def _find_definition_drift(stage_def: Stage, halted_fingerprint: str) -> str | None:
    """The run snapshotted its queue under `halted_fingerprint` and its decisions
    are keyed by it, while the columns those decisions are read and written
    through come from the LIVE definition. Edit a fingerprinted queue field
    (`reviewed_columns`, `verdict_column`, …) between the halt and the review and
    the two describe different column sets, so neither reading nor adding to the
    recorded decisions is meaningful. None when they still agree."""
    live_fingerprint = stage_def.compute_definition_fingerprint()
    if live_fingerprint == halted_fingerprint:
        return None
    return (
        f"stage '{stage_def.id}' has changed since this run halted for review "
        f"(halted under fingerprint {halted_fingerprint}, now {live_fingerprint}). "
        "Its recorded decisions describe the definition it halted under, so they "
        "cannot be shown or added to. Restore that definition, or start a new run."
    )


def _validate_stage_definition_unchanged(stage_def: Stage, halted_fingerprint: str) -> None:
    drift = _find_definition_drift(stage_def, halted_fingerprint)
    if drift is not None:
        raise HTTPException(status_code=409, detail=drift)


def _require_reviewed_column(stage_def: Stage, source: str, target: str) -> Column:
    """The declared column a reviewed value must satisfy: `output_schema`'s
    `target` column, or — for a stage that declares no `output_schema` — the
    input edge's `source` column, whose spec `target` is required to match
    (app.models.stages.human_review_queue._find_reviewed_target_issues). With
    neither declared the type is unknowable, so no value may be accepted."""
    if stage_def.output_schema is not None:
        declared = stage_def.output_schema.column_for_name(target)
        if declared is not None:
            return declared
    input_schema = resolve_input_schema(stage_def, 0) if stage_def.inputs else None
    source_column = input_schema.column_for_name(source) if input_schema else None
    if source_column is not None:
        return source_column.model_copy(update={"name": target})
    raise HTTPException(
        status_code=400,
        detail=(
            f"stage '{stage_def.id}': neither its output_schema nor its input edge "
            f"declares a column for reviewed value '{target}' (reviewing '{source}'), "
            "so there is no declaration to accept a value against"
        ),
    )


# --- queue_decide helpers ------------------------------------------------------


def _require_reviewer_name(reviewer: str) -> str:
    name = reviewer.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail="reviewer must be a non-blank name: no decision is recorded unattributed",
        )
    return name


def _parse_reviewed_values(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"reviewed_values is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail=f"reviewed_values must be a JSON object, got {type(parsed).__name__}",
        )
    return {str(name): value for name, value in parsed.items()}


def _coerce_reviewed_values(
    stage_def: Stage, queue: QueueConfig, supplied: Mapping[str, object]
) -> dict[str, object]:
    """Each supplied value parsed against its target column's whole declaration.
    A key the stage does not declare passes through untouched: the review service
    owns the exactly-the-declared-columns rule, and duplicating it here would
    give it two places to drift."""
    column_by_target = {
        target: _require_reviewed_column(stage_def, source, target)
        for source, target in queue.reviewed_columns.items()
    }
    return {
        target: (
            _coerce_reviewed_value(column_by_target[target], value)
            if target in column_by_target else value
        )
        for target, value in supplied.items()
    }


def _coerce_reviewed_value(column: Column, value: object) -> object:
    try:
        return column.coerce_text(_as_form_text(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _as_form_text(value: object) -> str:
    """A JSON reviewed value as the text `Column.coerce_text` parses. JSON null
    becomes blank text, which that method turns into None on a nullable column
    and refuses on a non-nullable one — the null is never assumed to be allowed."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise HTTPException(
        status_code=400,
        detail=(
            "a reviewed value must be a JSON string, number, boolean or null, got "
            f"{type(value).__name__}"
        ),
    )


def _normalise_review_notes(review_notes: str | None) -> str | None:
    """An HTML form posts an untouched notes box as "", not as an absent field:
    blank means no note, never an empty note."""
    stripped = (review_notes or "").strip()
    return stripped or None


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
    the read-only cache view's entries for (project, stage, stage_fingerprint)."""
    entries = StageCacheEntry.read_only().find_entries(
        project, stage_id, stage_fingerprint
    )
    return {entry.input_fingerprint: entry for entry in entries}


def _display_decision(entry: StageCacheEntry, queue: QueueConfig) -> _DecisionDisplay:
    """The reviewer decision one cached entry records, read off the column names
    the stage declares."""
    output = _require_recorded_output(entry, queue)
    notes_column = queue.review_notes_column
    return _DecisionDisplay(
        verdict=str(output[queue.verdict_column]),
        reviewed_values={target: output[target] for target in queue.reviewed_columns.values()},
        review_notes=(
            None if notes_column is None else _as_optional_text(output.get(notes_column))
        ),
        reviewer=str(output[queue.reviewer_column]),
        reviewed_at=str(output[queue.reviewed_at_column]),
    )


def _require_recorded_output(
    entry: StageCacheEntry, queue: QueueConfig
) -> Mapping[str, object]:
    """The review service writes every column named here, so an entry missing
    one was recorded under a different column vocabulary — the page states that
    rather than half-rendering it."""
    output = entry.output_row or {}
    missing = sorted(
        {queue.verdict_column, queue.reviewer_column, queue.reviewed_at_column,
         *queue.reviewed_columns.values()} - set(output)
    )
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"the cached decision for input fingerprint '{entry.input_fingerprint}' "
                f"records no {missing}: it was written under a different column "
                "vocabulary and cannot be displayed. Re-record a decision for this row."
            ),
        )
    return output


def _as_optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _build_reviewed_fields(stage_def: Stage, queue: QueueConfig) -> list[_ReviewedField]:
    return [
        _build_reviewed_field(source, target, _require_reviewed_column(stage_def, source, target))
        for source, target in queue.reviewed_columns.items()
    ]


# The HTML input `type` each scalar column type is entered through; a column
# declaring an `enum` overrides this with a select of its vocabulary. `bool` is a
# select rather than a checkbox because a checkbox has two states and a bool
# column has three — true, false, and (on a nullable column, or before anyone has
# supplied one) no value at all, which a checkbox would render as false.
_CONTROL_BY_COLUMN_TYPE: dict[str, str] = {
    "str": "text", "int": "number", "float": "number",
    "bool": "select", "date": "date", "datetime": "datetime-local",
}
_STEP_BY_COLUMN_TYPE: dict[str, str] = {"int": "1", "float": "any"}
# The select vocabulary of a column type that has one without declaring an `enum`.
_OPTIONS_BY_COLUMN_TYPE: dict[str, list[str]] = {"bool": ["true", "false"]}


def _build_reviewed_field(source: str, target: str, column: Column) -> _ReviewedField:
    control = "select" if column.enum is not None else _CONTROL_BY_COLUMN_TYPE.get(column.type)
    if control is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reviewed column '{target}' is declared type '{column.type}', which "
                "cannot be entered through a form field"
            ),
        )
    low, high = column.resolve_numeric_bounds()
    options = column.enum if column.enum is not None else _OPTIONS_BY_COLUMN_TYPE.get(column.type)
    return _ReviewedField(
        source=source, target=target, control=control, nullable=column.nullable,
        step=_STEP_BY_COLUMN_TYPE.get(column.type), minimum=low, maximum=high,
        options=None if options is None else list(options),
    )


def _load_upstream_stage(stages: list[Stage], stage_def: Stage) -> Stage | None:
    """The upstream stage whose OUTPUT this queue stage reviews — stage_def's
    declared input, or None if it declares none."""
    upstream_ids = stage_def.input_ids
    return find_stage(stages, upstream_ids[0]) if upstream_ids else None


def _resolve_prompt_template(upstream_def: Stage | None) -> str | None:
    return upstream_def.llm.prompt_data_template if upstream_def and upstream_def.llm else None


def _read_table_or_none(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        return read_table(path)
    except Exception:  # noqa: BLE001
        return None


def _resolve_upstream_input_frame(
    upstream_def: Stage, manifest: dict[str, Any], run_dir: Path
) -> tuple[pd.DataFrame | None, list[str] | None]:
    """The upstream stage's OWN input — DataFrame plus declared primary key — the
    frame the queue snapshot needs to join back against to recover the model
    input, or (None, pk) if that stage's output isn't on disk."""
    output_by_id = {s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])}
    upstream_in_id = upstream_def.input_ids[0]
    upstream_in = upstream_def.inputs[0] if upstream_def.inputs else None
    pk = upstream_in.table_schema.primary_key if upstream_in and upstream_in.table_schema else None
    in_path = output_by_id.get(upstream_in_id)
    in_df = _read_table_or_none(run_dir / in_path) if in_path else None
    return in_df, pk


def _find_join_keys(primary_key: list[str] | None, columns: list[str]) -> list[str]:
    """Columns to join the queue snapshot back to the upstream stage's input on:
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
    """Recover the MODEL INPUT so the AI's values are reviewable, not just
    visible. The queue snapshot holds the upstream stage's OUTPUT; the material
    the model actually judged lives in that stage's INPUT, one stage further
    up. Join it back + resolve the prompt template it was produced with."""
    upstream_def = _load_upstream_stage(stages, stage_def)
    prompt_template = _resolve_prompt_template(upstream_def)

    input_lookup: dict[tuple[str, ...], dict[str, Any]] = {}
    join_keys: list[str] = []
    if upstream_def and upstream_def.input_ids:
        in_df, pk = _resolve_upstream_input_frame(upstream_def, manifest, run_dir)
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
    queue: QueueConfig,
    fields: list[_ReviewedField],
    input_lookup: dict[tuple[str, ...], dict[str, Any]],
    join_keys: list[str],
    prompt_template: str | None,
) -> dict[str, Any]:
    entry = entries_by_fingerprint.get(input_fingerprint)
    model_input = _find_model_input(row, input_lookup, join_keys)
    prior = _display_decision(entry, queue) if entry is not None else None
    displayed_row = {str(k): display_cell(v) for k, v in row.items()}
    return {
        "input_fingerprint": input_fingerprint,
        "row": displayed_row,
        "model_input": model_input,
        "rendered_prompt": _render_model_prompt(model_input, prompt_template),
        "prior_decision": prior,
        "prefill": _build_field_prefills(fields, displayed_row, prior),
        "ai_text": _build_ai_texts(fields, displayed_row),
    }


def _build_ai_texts(
    fields: list[_ReviewedField], displayed_row: dict[str, Any]
) -> dict[str, str]:
    """The AI value per field as the text `Approve` posts for it — blank for a
    null, so an absent value is submitted as absent rather than as a value."""
    return {
        field.target: (
            "" if (ai := _blank_to_none(displayed_row.get(field.source))) is None
            else _as_option_text(ai)
        )
        for field in fields
    }


def _build_field_prefills(
    fields: list[_ReviewedField], displayed_row: dict[str, Any], prior: _DecisionDisplay | None
) -> dict[str, object]:
    """What each field opens with: on a row already decided, exactly what the
    reviewer recorded — including a recorded null, which does NOT fall back to
    the AI value — and on an undecided row the AI value it reviews. A blank on
    either side is None: the control renders explicitly unset rather than
    inventing a value of the column's type."""
    return {
        field.target: _resolve_prefill(
            field,
            displayed_row.get(field.source) if prior is None
            else prior.reviewed_values.get(field.target),
        )
        for field in fields
    }


def _resolve_prefill(field: _ReviewedField, value: object) -> object:
    """A select's prefill is the OPTION TEXT it opens on, so the template
    compares like with like: leaving a python `True` here for the template to
    stringify yields "True", which matches no option, and a select that
    pre-selects nothing opens on whichever option is first — a value nobody
    supplied. None when the value matches no option, which the template renders
    as an explicitly-selected unset."""
    resolved = _blank_to_none(value)
    if resolved is None or field.options is None:
        return resolved
    text = _as_option_text(resolved)
    return text if text in field.options else None


def _as_option_text(value: object) -> str:
    """A bool carries the same "true"/"false" spelling the options and
    `Column.coerce_text` use; everything else compares by its own text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _blank_to_none(value: object) -> object:
    return None if value is None or value == "" else value


def _build_review_items(
    snapshot: pd.DataFrame | None,
    fingerprints: QueueFingerprints | None,
    entries_by_fingerprint: dict[str, StageCacheEntry],
    queue: QueueConfig,
    fields: list[_ReviewedField],
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
        _build_review_item(
            row, fp, entries_by_fingerprint, queue, fields,
            input_lookup, join_keys, prompt_template,
        )
        for (_, row), fp in zip(snapshot.iterrows(), fingerprints.input_fingerprints)
    ]
