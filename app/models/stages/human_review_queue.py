from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.schema import SCALAR_COLUMN_TYPES, STR_COLUMN_TYPE, TableSchema
from app.models.stages.shared import find_predicate_column_issues, resolve_input_schema

if TYPE_CHECKING:
    from app.models.stage import QueueConfig, Stage


def find_queue_column_issues(stage: "Stage") -> list[str]:
    queue = stage.queue
    assert queue is not None  # Stage._handle_for_type guarantees this for type="human_review_queue"
    input_schema = resolve_input_schema(stage, 0)
    issues = _find_filter_issues(stage.id, queue, input_schema)
    issues += _find_reviewed_source_issues(stage.id, queue, input_schema)
    issues += _find_added_column_collisions(stage.id, queue, input_schema)
    issues += _find_duplicate_added_names(stage.id, queue)
    output_schema = stage.output_schema
    assert output_schema is not None  # Stage._schemas_declared runs first and requires one
    issues += _find_reviewed_target_issues(stage.id, queue, input_schema, output_schema)
    issues += _find_bookkeeping_target_issues(stage.id, queue, output_schema)
    return issues


def _added_column_names(queue: "QueueConfig") -> list[str]:
    """Every column a queue stage adds to its input, in the order it declares
    them: the reviewed targets, then the bookkeeping columns. Duplicates are
    NOT collapsed — `_find_duplicate_added_names` reports them."""
    names = list(queue.reviewed_columns.values())
    names += [queue.verdict_column, queue.reviewer_column, queue.reviewed_at_column]
    if queue.review_notes_column is not None:
        names.append(queue.review_notes_column)
    return names


# --- the individual checks -----------------------------------------------------


def _find_filter_issues(
    sid: str, queue: "QueueConfig", input_schema: TableSchema
) -> list[str]:
    """Columns `queue.filter` reads that the input does not supply — the check
    that catches a filter reading a column the review is meant to SET."""
    if not queue.filter:
        return []
    return find_predicate_column_issues(
        queue.filter, stage_id=sid, field="queue.filter",
        cols={c.name for c in input_schema.columns},
    )


def _find_reviewed_source_issues(
    sid: str, queue: "QueueConfig", input_schema: TableSchema
) -> list[str]:
    issues: list[str] = []
    for source in sorted(queue.reviewed_columns):
        column = input_schema.column_for_name(source)
        if column is None:
            issues.append(
                f"stage '{sid}': queue.reviewed_columns names source column "
                f"'{source}' not in its input schema "
                f"(declares {sorted(c.name for c in input_schema.columns)})"
            )
        elif column.type not in SCALAR_COLUMN_TYPES:
            issues.append(
                f"stage '{sid}': queue.reviewed_columns source column '{source}' is "
                f"type '{column.type}', which cannot be reviewed through a form field "
                f"(reviewable types: {sorted(SCALAR_COLUMN_TYPES)})"
            )
    return issues


def _find_added_column_collisions(
    sid: str, queue: "QueueConfig", input_schema: TableSchema
) -> list[str]:
    """A column this stage adds may never reuse an input column's name: the
    stage adds columns and never modifies one in place. This also catches two
    review stages in series where the second reuses the first's names."""
    existing = {c.name for c in input_schema.columns}
    return [
        f"stage '{sid}': queue adds column '{name}', which its input schema already "
        f"declares — a review stage adds columns and never overwrites one"
        for name in sorted(set(_added_column_names(queue)) & existing)
    ]


def _find_duplicate_added_names(sid: str, queue: "QueueConfig") -> list[str]:
    names = _added_column_names(queue)
    return [
        f"stage '{sid}': queue declares column name '{name}' more than once"
        for name in sorted({n for n in names if names.count(n) > 1})
    ]


def _find_reviewed_target_issues(
    sid: str, queue: "QueueConfig", input_schema: TableSchema, output_schema: TableSchema
) -> list[str]:
    """Each reviewed target must be declared on output_schema with its source
    column's spec, and be at least as permissive about nulls. Framed as a
    producer/consumer check on a one-column schema pair: the declared target
    column is the consumer, the source column (renamed to the target) is what
    supplies it."""
    issues: list[str] = []
    for source, target in sorted(queue.reviewed_columns.items()):
        source_column = input_schema.column_for_name(source)
        target_column = output_schema.column_for_name(target)
        if target_column is None:
            issues.append(
                f"stage '{sid}': queue.reviewed_columns adds column '{target}', which "
                f"output_schema does not declare"
            )
            continue
        if source_column is None:
            continue  # already reported by _find_reviewed_source_issues
        reasons = TableSchema(columns=[target_column]).find_unsatisfied_columns(
            TableSchema(columns=[source_column.model_copy(update={"name": target})])
        )
        issues += [
            f"stage '{sid}': queue.reviewed_columns reviews '{source}' into '{target}', "
            f"but {reason} (the source column)"
            for reason in reasons
        ]
    return issues


def _find_bookkeeping_target_issues(
    sid: str, queue: "QueueConfig", output_schema: TableSchema
) -> list[str]:
    issues: list[str] = []
    for field, name in _bookkeeping_columns(queue):
        column = output_schema.column_for_name(name)
        if column is None:
            issues.append(
                f"stage '{sid}': queue.{field} adds column '{name}', which output_schema "
                f"does not declare"
            )
        elif column.type != STR_COLUMN_TYPE:
            issues.append(
                f"stage '{sid}': queue.{field} column '{name}' is declared "
                f"'{column.type}' in output_schema, but it is written as "
                f"'{STR_COLUMN_TYPE}'"
            )
    return issues


def _bookkeeping_columns(queue: "QueueConfig") -> list[tuple[str, str]]:
    columns = [
        ("verdict_column", queue.verdict_column),
        ("reviewer_column", queue.reviewer_column),
        ("reviewed_at_column", queue.reviewed_at_column),
    ]
    if queue.review_notes_column is not None:
        columns.append(("review_notes_column", queue.review_notes_column))
    return columns
