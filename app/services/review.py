"""Record a reviewer's verdict on one queued row as a stage-result cache entry
(app.core.stage_cache), building the review stage's output row from what the
stage's own `output_schema` DECLARES — never from a fixed score vocabulary.

A human_review_queue stage is a transform whose worker is a person. The
columns it outputs beyond the row it was handed are the fields that person
supplies (`app.models.stages.find_reviewer_fields`), exactly as an
llm_transform asks its model for `output_schema.subtract(input_schema)`. This
module is the one place that turns a submitted form into those declared
fields: it coerces each value to its declared column type, enforces the
schema's own nullability/enum/range rules, and writes

    output_row = frozen input + the reviewer's declared fields + audit columns

through the read+write cache accessor (`StageCacheEntry.read_write()`) —
recording a decision is the one sanctioned way run activity persists something
that outlives the run. A `reject` writes no row at all: its output is a
tombstone, and the row is dropped from the stage's output.

Below this module the cache stores an opaque `output_row`; above it the web
route is an HTTP translation. Neither knows what a score is — and neither does
this module. It knows only what the stage declared."""
from __future__ import annotations

from collections.abc import Collection, Mapping
import json

from app.core.errors import ReviewValidationError
from app.core.stage_cache import StageCacheEntry
from app.models import Column, RowReviewDecision, Stage
from app.models.schema import JSON_COLUMN_TYPE, LIST_JSON_COLUMN_TYPE, RANGE_UNBOUNDED_MARKER
from app.models.stages import find_reviewer_fields

# Form values arrive as strings; these are the spellings a `bool` column
# accepts, case-folded. Anything else is a loud ReviewValidationError rather
# than Python truthiness, which would read "false" as True.
_TRUE_SPELLINGS = frozenset({"true", "t", "yes", "y", "on", "1"})
_FALSE_SPELLINGS = frozenset({"false", "f", "no", "n", "off", "0"})


def record_decision(
    *, project: str, stage: Stage,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: RowReviewDecision, submitted_fields: Mapping[str, str],
    reviewer: str, reviewed_at: str,
) -> None:
    """Build the output row one reviewed row produces and record it through the
    read+write cache accessor, passing the raw frozen row and the resolved
    fingerprints.

    `submitted_fields` maps a reviewer-field NAME (as `stage.output_schema`
    declares it) to the raw string the reviewer submitted; an absent or blank
    value means "not supplied". Raises `ReviewValidationError` when the
    submission does not satisfy the stage's own declaration: a field the stage
    never declared, a value that will not coerce to its declared type or falls
    outside its declared enum/range, a required (non-nullable) field left
    blank, or a `modify` that changes nothing."""
    fields = _resolve_reviewer_fields(stage, frozen_row.keys(), verdict, submitted_fields)
    output_row = _build_output_row(frozen_row, verdict, fields, reviewer, reviewed_at)
    StageCacheEntry.read_write().record(
        project=project, stage_id=stage.id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        input_row=frozen_row, output_row=output_row,
    )


def _resolve_reviewer_fields(
    stage: Stage, input_columns: Collection[str],
    verdict: RowReviewDecision, submitted: Mapping[str, str],
) -> dict[str, object]:
    """The declared reviewer fields resolved to their stored values: one entry
    per field `find_reviewer_fields` derives, coerced from the submitted string
    or None when nothing was supplied.

    A `reject` produces no row at all, so nothing is asked of it and `{}` comes
    back without validating the submission. Otherwise every submitted name must
    be a declared field (a stray name is a form/schema mismatch, not something
    to drop silently), every non-nullable field must carry a value, and a
    `modify` must supply at least one — the general form of the old "modify
    requires modified_score" rule."""
    if verdict == RowReviewDecision.reject:
        return {}
    declared = find_reviewer_fields(stage, input_columns)
    _reject_undeclared_names(stage, declared, submitted)
    values: dict[str, object] = {
        column.name: _coerce_declared_value(column, submitted.get(column.name))
        for column in declared
    }
    if verdict == RowReviewDecision.modify and all(value is None for value in values.values()):
        raise ReviewValidationError(
            f"stage '{stage.id}': a modify must supply at least one of the fields its "
            f"output_schema declares (reviewer fields: {sorted(values) or 'none'})"
        )
    return values


def _reject_undeclared_names(
    stage: Stage, declared: list[Column], submitted: Mapping[str, str]
) -> None:
    """Loudly refuse a submitted field the stage's output_schema does not
    declare: dropping it silently would discard reviewer input, and keeping it
    silently would write a column the schema never promised."""
    unknown = sorted(set(submitted) - {column.name for column in declared})
    if unknown:
        raise ReviewValidationError(
            f"stage '{stage.id}': field(s) {unknown} are not declared by its output_schema "
            f"(reviewer fields: {[column.name for column in declared] or 'none'})"
        )


def _build_output_row(
    frozen_row: Mapping[str, object], verdict: RowReviewDecision,
    fields: Mapping[str, object], reviewer: str, reviewed_at: str,
) -> Mapping[str, object] | None:
    """The review stage's output row for one reviewed input: the frozen input,
    the reviewer's declared fields, and the audit columns the service owns
    (`app.models.stages.SERVICE_FILLED_COLUMNS`). A `reject` drops the row, so
    its output is None (a tombstone). Which of these columns actually survive
    is the stage's own decision — the runtime projects the frame onto
    output_schema."""
    if verdict == RowReviewDecision.reject:
        return None
    return {
        **frozen_row, **fields,
        "decision": verdict.value, "reviewer_id": reviewer, "reviewed_at": reviewed_at,
    }


# --- declared-value coercion ---------------------------------------------------


def _coerce_declared_value(column: Column, raw: str | None) -> object:
    """`raw` as the type `column` declares, or None when the reviewer supplied
    nothing (absent, or blank after stripping — an empty text box is "not
    supplied", never the empty string). A non-nullable column with nothing
    supplied raises, as does a value that will not coerce or that falls outside
    the column's declared enum/range."""
    text = (raw or "").strip()
    if not text:
        if not column.nullable:
            raise ReviewValidationError(
                f"field '{column.name}' is required (output_schema declares it non-nullable)"
            )
        return None
    value = _parse_by_type(column, text)
    _validate_enum(column, value)
    _validate_range(column, value)
    return value


def _parse_by_type(column: Column, text: str) -> object:
    """`text` parsed as `column.type`. Numeric and bool scalars parse to their
    Python equivalents; a `json`/`list[...]` column parses as JSON (the
    reviewer types the literal, in the shape the column declares); `str`,
    `date` and `datetime` stay strings — the JSON-native form the cache stores
    and a resumed run reloads."""
    declared = column.type
    is_structured = declared in (JSON_COLUMN_TYPE, LIST_JSON_COLUMN_TYPE) or declared.startswith("list[")
    try:
        if declared == "int":
            return int(text)
        if declared == "float":
            return float(text)
        if is_structured:
            return json.loads(text)
    except ValueError as exc:
        raise ReviewValidationError(
            f"field '{column.name}': {text!r} is not a valid {declared} ({exc})"
        ) from exc
    if declared == "bool":
        return _parse_bool(column, text)
    return text


def _parse_bool(column: Column, text: str) -> bool:
    folded = text.casefold()
    if folded in _TRUE_SPELLINGS:
        return True
    if folded in _FALSE_SPELLINGS:
        return False
    raise ReviewValidationError(
        f"field '{column.name}': {text!r} is not a valid bool "
        f"(expected one of {sorted(_TRUE_SPELLINGS | _FALSE_SPELLINGS)})"
    )


def _validate_enum(column: Column, value: object) -> None:
    """A value against the column's declared string vocabulary, if it has one."""
    if column.enum is not None and value not in column.enum:
        raise ReviewValidationError(
            f"field '{column.name}': {value!r} is not one of the declared values {column.enum}"
        )


def _validate_range(column: Column, value: object) -> None:
    """A numeric value against the column's declared `[low, high]` bounds — a
    bound containing RANGE_UNBOUNDED_MARKER ("+inf"/"-inf") means unbounded on
    that side, the same sentinel app/runtime/validation.py honours when it
    checks a frame against its schema. Checked here too so an out-of-range
    entry is refused at submit time rather than failing the run on resume."""
    if column.range is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    low, high = column.range
    if not _is_unbounded(low) and value < low:
        raise ReviewValidationError(
            f"field '{column.name}': {value!r} is below the declared range {column.range}"
        )
    if not _is_unbounded(high) and value > high:
        raise ReviewValidationError(
            f"field '{column.name}': {value!r} is above the declared range {column.range}"
        )


def _is_unbounded(bound: object) -> bool:
    return isinstance(bound, str) and RANGE_UNBOUNDED_MARKER in bound


__all__ = ["record_decision"]
