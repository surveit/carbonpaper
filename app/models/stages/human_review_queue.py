"""human_review_queue stage: the config block naming the columns the stage ADDS,
the reviewer's verdict vocabulary, and the checks that every named column
resolves — sources against the input edge, added names against output_schema."""
from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal, Optional

from pydantic import Field, field_validator

from app.models.schema import SCALAR_COLUMN_TYPES, STR_COLUMN_TYPE, StageConfig, TableSchema
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.shared import find_predicate_column_issues


class ReviewVerdict(str, Enum):
    """`skipped` is written by the runtime for a row the filter did not select; no client may post it."""
    approve = "approve"
    modify = "modify"
    skipped = "skipped"


class QueueConfig(StageConfig):
    """human_review_queue config block: what the human is asked, and what the stage adds."""
    # Every declared column name changes what the stage computes (which columns
    # the human is asked about, and what the added columns are called); routing,
    # conflict_resolution, and estimated_volume_per_week describe how a decision
    # is routed, not what is asked — see StageBase.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "filter", "reviewer_instructions", "reviewed_columns",
        "verdict_column", "reviewer_column", "reviewed_at_column", "review_notes_column",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "routing", "conflict_resolution", "estimated_volume_per_week",
    })

    filter: Optional[str] = None
    reviewer_instructions: Optional[str] = None
    reviewed_columns: dict[str, str] = Field(
        description=(
            "Each input column the human reviews, mapped to the name of the column this "
            "stage adds carrying the reviewed value: {source column -> reviewed column}."
        ),
    )
    verdict_column: str
    reviewer_column: str
    reviewed_at_column: str
    review_notes_column: Optional[str] = Field(
        default=None,
        description=(
            "Optional: name the column a reviewer's free-text note lands in. Omit it and "
            "the stage neither offers a notes box nor adds a notes column."
        ),
    )
    routing: Optional[str] = None
    conflict_resolution: Optional[str] = None
    estimated_volume_per_week: Optional[int] = None

    @field_validator("reviewed_columns")
    @classmethod
    def _require_a_reviewed_column(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError(
                "reviewed_columns must name at least one column: a review stage that "
                "adds no reviewed column asks the human for nothing"
            )
        return v


class HumanReviewQueueStage(StageBase):
    type: Literal[StageType.human_review_queue]
    queue: QueueConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"queue": self.queue}

    def find_config_column_issues(self) -> list[str]:
        sid, queue = self.id, self.queue
        input_schema = self.inputs[0].table_schema
        return (
            _find_duplicate_added_names(sid, queue)
            + _find_filter_issues(sid, queue, input_schema)
            + _find_reviewed_source_issues(sid, queue, input_schema)
            + _find_added_column_collisions(sid, queue, input_schema)
        )

    def find_output_schema_issues(self) -> list[str]:
        sid, queue = self.id, self.queue
        output_schema = self.output_schema
        assert output_schema is not None  # _schemas_declared runs first and requires one
        input_schema = self.inputs[0].table_schema
        return (
            _find_reviewed_target_issues(sid, queue, input_schema, output_schema)
            + _find_review_record_target_issues(sid, queue, output_schema)
        )


def resolve_queue_config(stage: StageBase) -> Optional[QueueConfig]:
    """The queue block, or None when `stage` is not a human_review_queue."""
    # This module owns `.queue` (tests/arch/test_handle_access_is_owned.py), so
    # every other layer asks for the block through here.
    return stage.queue if isinstance(stage, HumanReviewQueueStage) else None


def find_queue_column_issues(stage: HumanReviewQueueStage) -> list[str]:
    """Every issue in one queue stage's column configuration, input side then output side."""
    return stage.find_config_column_issues() + stage.find_output_schema_issues()


def find_added_columns(queue: QueueConfig) -> list[tuple[str, str]]:
    """Every column the stage adds, as (config field that names it, the name). Duplicates kept."""
    added = [
        (f"queue.reviewed_columns['{source}']", target)
        for source, target in queue.reviewed_columns.items()
    ]
    return added + find_review_record_columns(queue)


def find_review_record_columns(queue: QueueConfig) -> list[tuple[str, str]]:
    """The columns recording the act of reviewing, as (config field, name)."""
    columns = [
        ("queue.verdict_column", queue.verdict_column),
        ("queue.reviewer_column", queue.reviewer_column),
        ("queue.reviewed_at_column", queue.reviewed_at_column),
    ]
    if queue.review_notes_column is not None:
        columns.append(("queue.review_notes_column", queue.review_notes_column))
    return columns


# ── the individual checks ────────────────────────────────────────────────────
def _find_filter_issues(sid: str, queue: QueueConfig, input_schema: TableSchema) -> list[str]:
    # Catches a filter reading a column the review is meant to SET.
    if not queue.filter:
        return []
    return find_predicate_column_issues(
        queue.filter, stage_id=sid, field="queue.filter",
        cols={c.name for c in input_schema.columns},
    )


def _find_reviewed_source_issues(
    sid: str, queue: QueueConfig, input_schema: TableSchema
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
    sid: str, queue: QueueConfig, input_schema: TableSchema
) -> list[str]:
    # A stage that adds columns never overwrites one — this also catches two review
    # stages in series where the second reuses the first's names.
    existing = {c.name for c in input_schema.columns}
    return [
        f"stage '{sid}': {field} adds column '{name}', which its input schema already "
        f"declares — a review stage adds columns and never overwrites one"
        for field, name in sorted(find_added_columns(queue), key=lambda pair: pair[::-1])
        if name in existing
    ]


def _find_duplicate_added_names(sid: str, queue: QueueConfig) -> list[str]:
    fields_by_name: dict[str, list[str]] = {}
    for field, name in find_added_columns(queue):
        fields_by_name.setdefault(name, []).append(field)
    return [
        f"stage '{sid}': column '{name}' is named more than once, by "
        f"{' and '.join(sorted(fields))}"
        for name, fields in sorted(fields_by_name.items())
        if len(fields) > 1
    ]


def _find_reviewed_target_issues(
    sid: str, queue: QueueConfig, input_schema: TableSchema, output_schema: TableSchema
) -> list[str]:
    # Each reviewed target must be declared on output_schema carrying its source
    # column's full spec, and be at least as permissive about nulls. Framed as a
    # producer/consumer check over a one-column schema pair: the declared target is
    # the consumer, the source column renamed to the target is what supplies it.
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


def _find_review_record_target_issues(
    sid: str, queue: QueueConfig, output_schema: TableSchema
) -> list[str]:
    issues: list[str] = []
    for field, name in find_review_record_columns(queue):
        column = output_schema.column_for_name(name)
        if column is None:
            issues.append(
                f"stage '{sid}': {field} adds column '{name}', which output_schema "
                f"does not declare"
            )
            continue
        if column.type != STR_COLUMN_TYPE:
            issues.append(
                f"stage '{sid}': {field} column '{name}' is declared "
                f"'{column.type}' in output_schema, but it is written as "
                f"'{STR_COLUMN_TYPE}'"
            )
        if not column.nullable and field != "queue.verdict_column":
            issues.append(
                f"stage '{sid}': {field} column '{name}' is declared non-nullable in "
                f"output_schema, but this stage writes no value into it for a row the "
                f"filter skipped or auto-approve decided — declare it nullable. Only "
                f"queue.verdict_column carries a value on every row."
            )
    return issues

# Authoring notes for this module's stage type(s), as the plain-data shape the
# authoring prompts render. Assembled into NODE_TYPES by app.models.stages.
NODE_TYPE_SPECS: dict[str, dict[str, Any]] = {
    "human_review_queue": {
        "summary": "Pulls flagged rows for human decision; halts the run.",
        "blocks": ["queue"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["reviewed_columns", "verdict_column", "reviewer_column",
                     "reviewed_at_column"],
        "optional": ["filter", "reviewer_instructions", "review_notes_column",
                     "routing", "conflict_resolution", "estimated_volume_per_week"],
        "notes": (
            "Reviewed rows are matched to a cached human decision by "
            "fingerprinting the row itself — no column configuration is needed "
            "to enable that matching. The column fields say what the human is "
            "asked and what the stage ADDS: every column named by "
            "`reviewed_columns`, `verdict_column`, `reviewer_column`, "
            "`reviewed_at_column` and `review_notes_column` must be declared in "
            "`output_schema` and must not already exist in the input. Editing "
            "any of them — or `filter`/`reviewer_instructions` — changes the "
            "stage's definition fingerprint, so every previously cached "
            "decision for this stage stops matching and every row is asked "
            "again."
        ),
    },
}
