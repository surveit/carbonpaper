# The reviewer queue page's view model: everything queue.html renders, built
# from the stage's declared schemas, the halted snapshot and the cached
# decisions. Sits beside the router, which keeps only request/response work.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import math
import pandas as pd
from fastapi import HTTPException

from app.core.stage_cache import StageCacheEntry
from app.models import Column, Stage
from app.models.stages.human_review_queue import QueueConfig
from app.runtime.trace_links import RowTraceLinker
from app.web.loading import QueueFingerprints, display_cell

@dataclass(frozen=True)
class ReviewedField:
    # `source` is the column this stage received the value in, `target` where the reviewer's lands.

    source: str
    target: str
    control: str
    nullable: bool
    step: str | None
    minimum: float | None
    maximum: float | None
    options: list[str] | None
    description: str | None


@dataclass(frozen=True)
class QueuedColumn:
    name: str
    description: str | None


@dataclass(frozen=True)
class DescribedColumns:
    columns: list[QueuedColumn]
    schema_note: str | None


@dataclass(frozen=True)
class Lineage:
    upstream_stage_id: str | None
    note: str | None


@dataclass(frozen=True)
class DecisionDisplay:
    verdict: str
    # Each recorded value in the spelling its form control uses, so the page
    # never shows the same value two ways (a python `True` beside a "true"
    # option). None is a recorded null, which is NOT the string "None".
    reviewed_values: dict[str, str | None]
    review_notes: str | None
    reviewer: str
    reviewed_at: str


@dataclass(frozen=True)
class ReviewItem:
    input_fingerprint: str
    row: dict[str, str | None]
    lineage_url: str | None
    prior_decision: DecisionDisplay | None
    prefill: dict[str, object]
    upstream_text: dict[str, str]


@dataclass(frozen=True)
class PositionedItem:
    item: ReviewItem
    # 1-based, matching the "Row N of total" the card shows.
    row_position: int


@dataclass(frozen=True)
class QueuePage:
    reviewed_fields: list[ReviewedField]
    review_notes_label: str | None
    context_columns: list[QueuedColumn]
    schema_note: str | None
    lineage_note: str | None
    items: list[ReviewItem]
    reviewed_count: int
    total: int
    all_reviewed: bool


def build_queue_page(
    project: str, run_id: str, stage_def: Stage, queue: QueueConfig,
    snapshot: pd.DataFrame | None, fingerprints: QueueFingerprints | None,
    drift: str | None,
) -> QueuePage:
    described = describe_queued_columns(stage_def, snapshot)
    lineage = resolve_lineage(stage_def, fingerprints)
    fields = [] if drift else build_reviewed_fields(stage_def, queue)
    items: list[ReviewItem] = []
    if snapshot is not None and fingerprints is not None and drift is None:
        entries = _load_decided_entries(project, stage_def.id, fingerprints.stage_fingerprint)
        items = _build_review_items(
            snapshot, fingerprints, entries, queue, fields,
            build_lineage_urls(project, run_id, lineage, fingerprints),
        )
    reviewed_count = sum(1 for item in items if item.prior_decision is not None)
    return QueuePage(
        reviewed_fields=fields,
        review_notes_label=(
            None if queue.review_notes_column is None
            else resolve_notes_label(stage_def, queue.review_notes_column)
        ),
        context_columns=_subtract_reviewed_columns(described.columns, queue),
        schema_note=described.schema_note,
        lineage_note=lineage.note,
        items=items,
        reviewed_count=reviewed_count,
        total=len(items),
        all_reviewed=len(items) > 0 and reviewed_count == len(items),
    )


def find_positioned_item(page: QueuePage, input_fingerprint: str) -> PositionedItem | None:
    return next(
        (
            PositionedItem(item=item, row_position=position)
            for position, item in enumerate(page.items, start=1)
            if item.input_fingerprint == input_fingerprint
        ),
        None,
    )


def find_definition_drift(stage_def: Stage, halted_fingerprint: str) -> str | None:
    # Decisions are keyed by the halted fingerprint but read through the LIVE columns.
    live_fingerprint = stage_def.compute_definition_fingerprint()
    if live_fingerprint == halted_fingerprint:
        return None
    return (
        f"stage '{stage_def.id}' has changed since this run halted for review "
        f"(halted under fingerprint {halted_fingerprint}, now {live_fingerprint}). "
        "Its recorded decisions describe the definition it halted under, so they "
        "cannot be shown or added to. Restore that definition, or start a new run."
    )


def require_reviewed_column(stage_def: Stage, target: str) -> Column:
    output_schema = stage_def.resolve_output_schema()
    assert output_schema is not None  # _schemas_declared: an outer is stored or resolves
    declared = output_schema.column_for_name(target)
    assert declared is not None  # find_queue_column_issues: every target is declared
    return declared


def build_reviewed_fields(stage_def: Stage, queue: QueueConfig) -> list[ReviewedField]:
    source_schema = stage_def.inputs[0].table_schema
    fields = []
    for source, target in queue.reviewed_columns.items():
        declared_source = source_schema.column_for_name(source)
        assert declared_source is not None  # find_queue_column_issues: every source is declared
        fields.append(_build_reviewed_field(
            source, target, require_reviewed_column(stage_def, target),
            declared_source.description,
        ))
    return fields


def describe_queued_columns(
    stage_def: Stage, snapshot: pd.DataFrame | None
) -> DescribedColumns:
    names = [str(c) for c in snapshot.columns] if snapshot is not None else []
    schema = stage_def.inputs[0].table_schema
    declared = {column.name: column for column in schema.columns}
    return DescribedColumns(
        columns=[
            QueuedColumn(
                name=name,
                description=declared[name].description if name in declared else None,
            )
            for name in names
        ],
        schema_note=_find_schema_discrepancy(sorted(declared), names),
    )


def resolve_lineage(stage_def: Stage, fingerprints: QueueFingerprints | None) -> Lineage:
    # The queue has no output at halt, so the link points at the UPSTREAM stage's row.
    input_ids = stage_def.input_ids
    if fingerprints is not None and fingerprints.row_ordinals is None:
        return Lineage(None, (
            "This run halted before the queue recorded each row's ordinal, so there is "
            "no exact row to link to upstream."
        ))
    return Lineage(input_ids[0], None)


def build_lineage_urls(
    project: str, run_id: str, lineage: Lineage, fingerprints: QueueFingerprints
) -> list[str | None]:
    # POSITIONALLY aligned to `fingerprints.input_fingerprints`; None where no link holds.
    ordinals = fingerprints.row_ordinals
    if lineage.upstream_stage_id is None or ordinals is None:
        return [None] * len(fingerprints.input_fingerprints)
    linker = RowTraceLinker(project=project, run_id=run_id)
    return [linker.build_row_trace_url(lineage.upstream_stage_id, o) for o in ordinals]


def resolve_notes_label(stage_def: Stage, column: str) -> str:
    output_schema = stage_def.resolve_output_schema()
    assert output_schema is not None  # _schemas_declared: an outer is stored or resolves
    declared = output_schema.column_for_name(column)
    if declared is not None and declared.description:
        return declared.description
    return column.replace("_", " ").capitalize()


# ── The reviewed fields ──────────────────────────────────────────────────────
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
) -> ReviewedField:
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
    return ReviewedField(
        source=source, target=target, control=control, nullable=column.nullable,
        step=_STEP_BY_COLUMN_TYPE.get(column.type), minimum=low, maximum=high,
        options=None if options is None else list(options),
        description=column.description or source_description,
    )


# ── Describing the queued rows ───────────────────────────────────────────────


def _subtract_reviewed_columns(
    columns: list[QueuedColumn], queue: QueueConfig
) -> list[QueuedColumn]:
    under_review = set(queue.reviewed_columns)
    return [column for column in columns if column.name not in under_review]


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


# ── The per-row cards ────────────────────────────────────────────────────────


def _build_review_items(
    snapshot: pd.DataFrame,
    fingerprints: QueueFingerprints,
    entries_by_fingerprint: dict[str, StageCacheEntry],
    queue: QueueConfig,
    fields: list[ReviewedField],
    lineage_urls: list[str | None],
) -> list[ReviewItem]:
    # Zipped POSITIONALLY: the snapshot carries no fingerprint column to key on.
    return [
        _build_review_item(row, fp, entries_by_fingerprint, queue, fields, url)
        for (_, row), fp, url in zip(
            snapshot.iterrows(), fingerprints.input_fingerprints, lineage_urls
        )
    ]


def _build_review_item(
    row: pd.Series,
    input_fingerprint: str,
    entries_by_fingerprint: dict[str, StageCacheEntry],
    queue: QueueConfig,
    fields: list[ReviewedField],
    lineage_url: str | None,
) -> ReviewItem:
    entry = entries_by_fingerprint.get(input_fingerprint)
    prior = _display_decision(entry, queue) if entry is not None else None
    displayed_row = {str(k): display_cell(v) for k, v in row.items()}
    return ReviewItem(
        input_fingerprint=input_fingerprint,
        row={str(name): _as_cell_text(value) for name, value in row.items()},
        lineage_url=lineage_url,
        prior_decision=prior,
        prefill=_build_field_prefills(fields, displayed_row, prior),
        upstream_text=_build_upstream_texts(fields, displayed_row),
    )


def _build_upstream_texts(
    fields: list[ReviewedField], displayed_row: dict[str, object]
) -> dict[str, str]:
    return {
        field.target: (
            "" if (received := _blank_to_none(displayed_row.get(field.source))) is None
            else _as_option_text(received)
        )
        for field in fields
    }


def _build_field_prefills(
    fields: list[ReviewedField], displayed_row: dict[str, object],
    prior: DecisionDisplay | None,
) -> dict[str, object]:
    return {
        field.target: _resolve_prefill(
            field,
            displayed_row.get(field.source) if prior is None
            else prior.reviewed_values.get(field.target),
        )
        for field in fields
    }


def _resolve_prefill(field: ReviewedField, value: object) -> object:
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


# ── Prior decisions ──────────────────────────────────────────────────────────


def _load_decided_entries(
    project: str, stage_id: str, stage_fingerprint: str
) -> dict[str, StageCacheEntry]:
    entries = StageCacheEntry.read_only().find_entries(project, stage_id, stage_fingerprint)
    return {entry.input_fingerprint: entry for entry in entries}


def _display_decision(entry: StageCacheEntry, queue: QueueConfig) -> DecisionDisplay:
    output = _require_recorded_output(entry, queue)
    notes_column = queue.review_notes_column
    notes = None if notes_column is None else output.get(notes_column)
    return DecisionDisplay(
        verdict=str(output[queue.verdict_column]),
        reviewed_values={
            target: (None if output[target] is None else _as_option_text(output[target]))
            for target in queue.reviewed_columns.values()
        },
        review_notes=None if notes is None else str(notes),
        reviewer=str(output[queue.reviewer_column]),
        reviewed_at=str(output[queue.reviewed_at_column]),
    )


def _require_recorded_output(
    entry: StageCacheEntry, queue: QueueConfig
) -> Mapping[str, object]:
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


# ── Value spellings ──────────────────────────────────────────────────────────


def _as_cell_text(value: object) -> str | None:
    # `display_cell` flattens a null to "", so a real empty string would read as nothing.
    if _is_null(value):
        return None
    displayed = display_cell(value)
    return "" if displayed is None else _as_option_text(displayed)


def _is_null(value: object) -> bool:
    # Not `pd.isna`, which raises on a list- or array-valued cell.
    if value is None or value is pd.NaT or value is pd.NA:
        return True
    return isinstance(value, float) and math.isnan(value)


def _as_option_text(value: object) -> str:
    # A bool spells "true"/"false" — what the form's options and the parser both use.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _blank_to_none(value: object) -> object:
    return None if value is None or value == "" else value
