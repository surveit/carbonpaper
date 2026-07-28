"""Human-review queue: render the reviewer UI for one queue stage — the queued
rows themselves, described by the columns the stage's input edge declares, and
a lineage link per row — and persist reviewer decisions into the stage-result
cache (app.core.stage_cache)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.errors import ReviewValidationError
from app.models import Column, QueueConfig, ReviewVerdict, Stage
from app.models.stages.shared import resolve_input_schema
from app.runtime.trace_links import RowTraceLinker
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
    runs_dir,
)

router = APIRouter()


@dataclass(frozen=True)
class _DecisionDisplay:
    verdict: str
    # Each recorded value in the spelling its form control uses, so the page
    # never shows the same value two ways (a python `True` beside a "true"
    # option). None is a recorded null, which is NOT the string "None".
    reviewed_values: dict[str, str | None]
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
    # The declared description behind the field's tooltip; None where neither
    # the target nor the source column declares one — never invented.
    description: str | None


@dataclass(frozen=True)
class _QueuedColumn:
    """One column of the queued rows as the page describes it. `description` and
    `type` are None where the input edge declares nothing for that column —
    unknowable, never inferred from the values."""

    name: str
    description: str | None
    type: str | None
    in_primary_key: bool


@dataclass(frozen=True)
class _IdentityCell:
    column: str
    value: str


@dataclass(frozen=True)
class _DescribedColumns:
    columns: list[_QueuedColumn]
    # The declared primary key restricted to columns the queued rows carry;
    # empty whenever a card's identity cannot be built from a declaration.
    primary_key: list[str]
    schema_note: str | None
    identity_note: str | None


@dataclass(frozen=True)
class _Lineage:
    """The upstream stage a queued row's provenance is traced through, or None
    with `note` saying why no link can be built."""

    upstream_stage_id: str | None
    note: str | None


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
    snapshot = queue_snapshot(project, run_id, stage_id)
    described = _describe_queued_columns(stage_def, snapshot)
    lineage = _resolve_lineage(stage_def, fingerprints)

    fields = [] if drift else _build_reviewed_fields(stage_def, queue)
    items: list[dict[str, Any]] = []
    if fingerprints is not None and drift is None:
        items = _build_review_items(
            snapshot, fingerprints,
            _load_decided_entries(project, stage_id, fingerprints.stage_fingerprint),
            queue, fields, described.primary_key,
            _build_lineage_urls(project, run_id, lineage, fingerprints),
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
            "review_notes_label": (
                None if queue.review_notes_column is None
                else _resolve_notes_label(stage_def, queue.review_notes_column)
            ),
            "queued_columns": described.columns,
            "schema_note": described.schema_note,
            "identity_note": described.identity_note,
            "lineage_note": lineage.note,
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
    reviewer: str = Form(...),
    reviewed_values: str = Form(...),
    prefilled_values: str = Form(...),
    review_notes: str | None = Form(None),
):
    """Persist a reviewer's decision as a `StageCacheEntry` keyed by this
    stage's definition fingerprint and this row's `input_fingerprint`.
    `reviewed_values` and `prefilled_values` are JSON objects keyed by reviewed
    TARGET column name — what the reviewer submitted, and what the page they
    submitted from had pre-filled. The verdict is DERIVED from the two, so the
    reviewer chooses none. The row is resolved by POSITION in the halted-queue
    sidecar's fingerprint list — never recomputed from live stages — so a
    fingerprint the sidecar can't vouch for 404s rather than being trusted."""
    stage_def = _require_queue_stage(load_stages(project).stages, stage_id)
    queue = _require_queue_config(stage_def)
    attributed_to = _require_reviewer_name(reviewer)
    supplied = _parse_reviewed_values(reviewed_values, "reviewed_values")
    verdict = _derive_verdict(supplied, _parse_reviewed_values(prefilled_values, "prefilled_values"))
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


def _parse_reviewed_values(raw: str, field: str) -> dict[str, object]:
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
    return {str(name): value for name, value in parsed.items()}


def _derive_verdict(
    supplied: Mapping[str, object], prefilled: Mapping[str, object]
) -> ReviewVerdict:
    """`modify` iff a submitted value differs from what THE PAGE carried as its
    prefill for that column. Deliberately not compared against a server-side
    recompute of the prefill: the reviewer decided against what they were
    shown, and a decision landing between render and submit would change what
    a recompute produced. A reviewer who retypes an identical value records
    `approve` — `modify` means the value changed."""
    unmatched = sorted(set(supplied) ^ set(prefilled))
    if unmatched:
        raise HTTPException(
            status_code=400,
            detail=(
                "reviewed_values and prefilled_values must name the same columns — "
                f"the verdict is derived by comparing them; {unmatched} appears in only "
                "one of the two"
            ),
        )
    changed = any(
        _as_form_text(value) != _as_form_text(prefilled[target])
        for target, value in supplied.items()
    )
    return ReviewVerdict.modify if changed else ReviewVerdict.approve


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
        reviewed_values={
            target: _as_optional_option_text(output[target])
            for target in queue.reviewed_columns.values()
        },
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


def _as_optional_option_text(value: object) -> str | None:
    return None if value is None else _as_option_text(value)


def _build_reviewed_fields(stage_def: Stage, queue: QueueConfig) -> list[_ReviewedField]:
    return [
        _build_reviewed_field(
            source,
            target,
            _require_reviewed_column(stage_def, source, target),
            _resolve_source_description(stage_def, source),
        )
        for source, target in queue.reviewed_columns.items()
    ]


def _resolve_source_description(stage_def: Stage, source: str) -> str | None:
    input_schema = resolve_input_schema(stage_def, 0) if stage_def.inputs else None
    column = input_schema.column_for_name(source) if input_schema else None
    return None if column is None else column.description


def _resolve_notes_label(stage_def: Stage, column: str) -> str:
    """The reviewer-notes box's visible label. The notes column is written by
    this stage, so only its `output_schema` can describe it; with no
    description the column name is spelled out as a sentence."""
    declared = (
        stage_def.output_schema.column_for_name(column)
        if stage_def.output_schema is not None else None
    )
    if declared is not None and declared.description:
        return declared.description
    return column.replace("_", " ").capitalize()


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


def _build_reviewed_field(
    source: str, target: str, column: Column, source_description: str | None
) -> _ReviewedField:
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
        description=column.description or source_description,
    )


NO_SCHEMA_NOTE = (
    "This stage's input edge declares no schema, so the columns below are the "
    "queued rows' own and carry no declared description, type or primary key."
)
NO_PRIMARY_KEY_NOTE = (
    "No primary key is declared on this stage's input schema, so a queued row "
    "is identified only by its position in this queue."
)


def _describe_queued_columns(
    stage_def: Stage, snapshot: pd.DataFrame | None
) -> _DescribedColumns:
    """The queued rows' own columns, annotated from the input edge's declared
    schema. The SNAPSHOT is the spine: it holds the values there are to review,
    so a declared column the rows do not carry is reported in `schema_note`
    rather than rendered as an empty field."""
    names = [str(c) for c in snapshot.columns] if snapshot is not None else []
    schema = resolve_input_schema(stage_def, 0) if stage_def.inputs else None
    if schema is None:
        return _DescribedColumns(
            columns=[_QueuedColumn(n, None, None, False) for n in names],
            primary_key=[], schema_note=NO_SCHEMA_NOTE, identity_note=NO_SCHEMA_NOTE,
        )
    declared = {column.name: column for column in schema.columns}
    primary_key = list(schema.primary_key or [])
    return _DescribedColumns(
        columns=[
            _QueuedColumn(
                name=name,
                description=declared[name].description if name in declared else None,
                type=declared[name].type if name in declared else None,
                in_primary_key=name in primary_key,
            )
            for name in names
        ],
        primary_key=primary_key if all(k in names for k in primary_key) else [],
        schema_note=_find_schema_discrepancy(sorted(declared), names),
        identity_note=_find_identity_note(primary_key, names),
    )


def _find_schema_discrepancy(declared: list[str], present: list[str]) -> str | None:
    missing = [name for name in declared if name not in present]
    undeclared = [name for name in present if name not in declared]
    parts = []
    if missing:
        parts.append(
            f"the input schema declares column(s) {missing} that the queued rows "
            "do not carry, so they are not shown"
        )
    if undeclared:
        parts.append(
            f"the queued rows carry column(s) {undeclared} the input schema does "
            "not declare, so those have no description or type"
        )
    return None if not parts else f"Schema and queued rows disagree: {'; '.join(parts)}."


def _find_identity_note(primary_key: list[str], present: list[str]) -> str | None:
    if not primary_key:
        return NO_PRIMARY_KEY_NOTE
    missing = [k for k in primary_key if k not in present]
    if missing:
        return (
            f"The declared primary key {primary_key} names column(s) {missing} the "
            "queued rows do not carry, so no row identity can be shown."
        )
    return None


def _resolve_lineage(stage_def: Stage, fingerprints: QueueFingerprints | None) -> _Lineage:
    """The queue stage has produced no output at halt time, so its own rows
    cannot be traced — the link points at the UPSTREAM stage's row instead.
    A row-mapped stage takes exactly one input frame
    (app.runtime.stages.execution._run_row_mapper), so a row ordinal cannot be
    attributed to any one of several declared inputs; that case gets no link."""
    input_ids = stage_def.input_ids
    if not input_ids:
        return _Lineage(None, "This stage declares no input, so there is no upstream row to trace.")
    if len(input_ids) > 1:
        return _Lineage(None, (
            f"This stage declares {len(input_ids)} inputs ({input_ids}); a queued row's "
            "ordinal is its position in the single frame a row-mapped stage takes, so it "
            "cannot be attributed to one of them and no lineage link is offered."
        ))
    if fingerprints is not None and fingerprints.row_ordinals is None:
        return _Lineage(None, (
            "This run halted before the queue recorded each row's ordinal, so there is "
            "no exact row to link to upstream."
        ))
    return _Lineage(input_ids[0], None)


def _build_lineage_urls(
    project: str, run_id: str, lineage: _Lineage, fingerprints: QueueFingerprints
) -> list[str | None]:
    """One entry per queued row, POSITIONALLY aligned to
    `fingerprints.input_fingerprints`; None where no link can be built."""
    ordinals = fingerprints.row_ordinals
    if lineage.upstream_stage_id is None or ordinals is None:
        return [None] * len(fingerprints.input_fingerprints)
    linker = RowTraceLinker(project=project, run_id=run_id)
    return [linker.build_row_trace_url(lineage.upstream_stage_id, o) for o in ordinals]


def _build_review_item(
    row: pd.Series,
    input_fingerprint: str,
    entries_by_fingerprint: dict[str, StageCacheEntry],
    queue: QueueConfig,
    fields: list[_ReviewedField],
    primary_key: list[str],
    lineage_url: str | None,
) -> dict[str, Any]:
    entry = entries_by_fingerprint.get(input_fingerprint)
    prior = _display_decision(entry, queue) if entry is not None else None
    displayed_row = {str(k): display_cell(v) for k, v in row.items()}
    return {
        "input_fingerprint": input_fingerprint,
        "row": {name: _as_display_text(value) for name, value in displayed_row.items()},
        "identity": [
            _IdentityCell(column=k, value=str(displayed_row[k])) for k in primary_key
        ],
        "lineage_url": lineage_url,
        "prior_decision": prior,
        "prefill": _build_field_prefills(fields, displayed_row, prior),
        "ai_text": _build_ai_texts(fields, displayed_row),
    }


def _build_ai_texts(
    fields: list[_ReviewedField], displayed_row: dict[str, Any]
) -> dict[str, str]:
    """The upstream value per field as the card displays it beside the control —
    blank for a null, which the page shows as an explicit null rather than as a
    value of the column's type."""
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
    """The text the control opens on, in that control's own spelling. None when
    the value matches no option, which the template renders as an
    explicitly-selected unset."""
    resolved = _blank_to_none(value)
    if resolved is None:
        return None
    if field.options is None:
        return _as_control_value(field.control, resolved)
    text = _as_option_text(resolved)
    return text if text in field.options else None


# The controls whose value attribute is rejected unless it is ISO 8601. A
# recorded decision comes back from the stage cache stringified, where a
# datetime reads "2026-01-01 00:00:00" — space-separated, so the control would
# render blank and an untouched save would post that blank over a real value.
_DATE_ONLY_BY_ISO_CONTROL: dict[str, bool] = {"date": True, "datetime-local": False}


def _as_control_value(control: str, value: object) -> object:
    date_only = _DATE_ONLY_BY_ISO_CONTROL.get(control)
    if date_only is None or not isinstance(value, str):
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return moment.date().isoformat() if date_only else moment.isoformat()


def _as_display_text(value: object) -> str:
    """A queued value as the card prints it. A bool takes the same spelling its
    control's options use, so one value never reads two ways on one card."""
    return "" if value is None else _as_option_text(value)


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
    primary_key: list[str],
    lineage_urls: list[str | None],
) -> list[dict[str, Any]]:
    """One review item per snapshot row, zipped POSITIONALLY with the
    sidecar's `input_fingerprints` and the lineage URLs built from the same
    sidecar — the lists are index-independent (the snapshot carries no
    fingerprint column), so position is the only correspondence between them."""
    if snapshot is None or fingerprints is None:
        return []
    return [
        _build_review_item(
            row, fp, entries_by_fingerprint, queue, fields, primary_key, url,
        )
        for (_, row), fp, url in zip(
            snapshot.iterrows(), fingerprints.input_fingerprints, lineage_urls
        )
    ]
