"""Column facts for a human_review_queue stage: which columns its
`queue.filter` predicate may reference (they must resolve against the stage's
input edge), and which columns its REVIEWER supplies.

A human_review_queue stage is a transform whose worker is a person, so the
same derivation an llm_transform uses for its reply spec applies to it: the
columns `output_schema` declares beyond the stage's input are the ones the
worker produces. `find_reviewer_fields` is that derivation, minus the handful
of columns the review service fills itself (`SERVICE_FILLED_COLUMNS`). It
lives here — on the model, beside the filter check — so the three layers that
need it (the review service that validates and records a decision, the web
form that asks for it, the runtime handler that shapes the un-reviewed rows)
all read one definition instead of three hardcoded column lists."""
from __future__ import annotations

from typing import TYPE_CHECKING, Collection

from app.models.stages.shared import find_predicate_column_issues, resolve_input_columns

if TYPE_CHECKING:
    from app.models.schema import Column
    from app.models.stage import Stage


SERVICE_FILLED_COLUMNS: frozenset[str] = frozenset({"decision", "reviewer_id", "reviewed_at"})
"""The output columns the review service fills for every reviewed row, so the
reviewer is never asked for them: the verdict itself (`decision`, chosen with
the approve/modify/reject control rather than typed into a field), and the
audit pair naming who reviewed the row (`reviewer_id`) and when
(`reviewed_at`). A stage that declares none of them simply has them projected
away by its output_schema; a stage that declares them gets them populated."""


def find_queue_filter_column_issues(stage: "Stage") -> list[str]:
    """Every column `queue.filter` references that is absent from the
    resolved single input — the check that catches a filter reading a column
    the review is meant to SET (e.g. a human-decision column that only exists
    after review), not one already produced upstream. [] when there is no
    filter, or the input's edge declares no schema at all."""
    queue = stage.queue
    assert queue is not None  # Stage._handle_for_type guarantees this for type="human_review_queue"
    if not queue.filter:
        return []
    cols = resolve_input_columns(stage, 0)
    if cols is None:
        return []
    return find_predicate_column_issues(queue.filter, stage_id=stage.id, field="queue.filter", cols=cols)


def find_reviewer_fields(stage: "Stage", input_columns: Collection[str]) -> list["Column"]:
    """The fields this stage's reviewer supplies for one queued row, in the
    order `output_schema` declares them: every declared output column that
    `input_columns` does not already carry and that the service does not fill
    itself (`SERVICE_FILLED_COLUMNS`).

    `input_columns` is the queued ROW's own column names — the exact upstream
    columns the reviewer saw — so the subtraction is by name, not by column
    spec (a frozen row carries values, not types). `TableSchema.subtract` is
    the schema-level analogue this mirrors: an llm_transform asks its model
    for `output_schema.subtract(input_schema)`, and a human_review_queue asks
    its human for the same difference.

    [] when the stage declares no output_schema: nothing is declared, so
    nothing is asked, and every reviewed row is its input plus the service's
    own audit columns."""
    if stage.output_schema is None:
        return []
    carried = set(input_columns)
    return [
        column for column in stage.output_schema.columns
        if column.name not in carried and column.name not in SERVICE_FILLED_COLUMNS
    ]
