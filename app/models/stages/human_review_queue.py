"""human_review_queue stage: the config block naming the columns the stage ADDS,
the reviewer's verdict vocabulary, and the checks that every named column
resolves — sources against the input edge, added names against the signature."""
from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal, Mapping, Optional

from pydantic import Field, field_validator

from app.models.schema import (
    SCALAR_COLUMN_TYPES,
    STR_COLUMN_TYPE,
    Column,
    StageConfig,
    TableSchema,
)
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.shared import find_predicate_column_issues
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.signature import ExtendsSignature


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
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ExtendsSignature

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

    # `queue` names the added columns, and `_find_added_column_collisions` enforces "a
    # review stage adds columns and never overwrites one". What this owns is the tie
    # between the two accounts: the signature's adds and the queue block must name the
    # same columns, each add carrying a spec the review runtime honours — a rewrite is
    # never claimable. The signature's own anchor-collision check can only ever restate
    # the config check's verdict.
    def find_signature_config_issues(self) -> list[str]:
        signature = self.signature
        declared = {name for _, name in find_added_columns(self.queue)}
        issues = [
            f"stage '{self.id}': signature adds `{column.name}`, which the review "
            f"runtime never writes — this stage adds exactly the columns its queue "
            f"block names ({sorted(declared)})"
            for column in signature.adds
            if column.name not in declared
        ]
        if signature.rewrites:
            issues.append(
                f"stage '{self.id}': human_review_queue never revises an input "
                f"column; rewrites are not supported"
            )
        adds_by_name = {column.name: column for column in signature.adds}
        input_schema = self.inputs[0].table_schema
        return (
            issues
            + _find_reviewed_target_issues(self.id, self.queue, input_schema, adds_by_name)
            + _find_review_record_target_issues(self.id, self.queue, adds_by_name)
        )


def resolve_queue_config(stage: StageBase) -> Optional[QueueConfig]:
    """The queue block, or None when `stage` is not a human_review_queue."""
    # This module owns `.queue` (tests/arch/test_handle_access_is_owned.py), so
    # every other layer asks for the block through here.
    return stage.queue if isinstance(stage, HumanReviewQueueStage) else None


def find_queue_column_issues(stage: HumanReviewQueueStage) -> list[str]:
    """Every issue in one queue stage's column configuration, input side then signature side."""
    return stage.find_config_column_issues() + stage.find_signature_config_issues()


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
    # THE check for "a review stage adds columns and never overwrites one": it runs on
    # every queue stage, signature or not, and covers every name the queue block adds.
    # This also catches two review stages in series where the second reuses the first's
    # names. `find_signature_config_issues` keeps a declared signature pinned to these
    # names rather than restating the rule.
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
    sid: str, queue: QueueConfig, input_schema: TableSchema,
    adds_by_name: Mapping[str, Column],
) -> list[str]:
    # Each reviewed target must be among the signature's adds carrying its source
    # column's full spec, and be at least as permissive about nulls. Framed as a
    # producer/consumer check over a one-column schema pair: the declared target is
    # the consumer, the source column renamed to the target is what supplies it.
    issues: list[str] = []
    for source, target in sorted(queue.reviewed_columns.items()):
        source_column = input_schema.column_for_name(source)
        target_column = adds_by_name.get(target)
        if target_column is None:
            issues.append(
                f"stage '{sid}': queue.reviewed_columns adds column '{target}', which "
                f"the signature does not add"
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
    sid: str, queue: QueueConfig, adds_by_name: Mapping[str, Column]
) -> list[str]:
    issues: list[str] = []
    for field, name in find_review_record_columns(queue):
        column = adds_by_name.get(name)
        if column is None:
            issues.append(
                f"stage '{sid}': {field} adds column '{name}', which the signature "
                f"does not add"
            )
            continue
        if column.type != STR_COLUMN_TYPE:
            issues.append(
                f"stage '{sid}': {field} column '{name}' is declared "
                f"'{column.type}' in the signature, but it is written as "
                f"'{STR_COLUMN_TYPE}'"
            )
        if not column.nullable and field != "queue.verdict_column":
            issues.append(
                f"stage '{sid}': {field} column '{name}' is declared non-nullable in "
                f"the signature, but this stage writes no value into it for a row the "
                f"filter skipped or auto-approve decided — declare it nullable. Only "
                f"queue.verdict_column carries a value on every row."
            )
    return issues

# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "human_review_queue": NodeTypeSpec(
        summary="Pulls flagged rows for human decision; halts the run.",
        signature_form="extends",
        blocks=["queue"],
        requires_inputs=True,
        min_inputs=1,
        required=["reviewed_columns", "verdict_column", "reviewer_column",
                     "reviewed_at_column"],
        optional=["filter", "reviewer_instructions", "review_notes_column",
                     "routing", "conflict_resolution", "estimated_volume_per_week"],
        notes=(
            "This type's output columns are the input columns, which ALL flow through, PLUS "
            "exactly the columns its own `queue` block names: one added column per "
            "`queue.reviewed_columns` entry, `queue.verdict_column`, `queue.reviewer_column`, "
            "`queue.reviewed_at_column`, and `queue.review_notes_column` when declared. "
            "The signature `adds` all of them, with matching specs. The four "
            "record-of-review columns must be declared type str — `queue.reviewed_at_column` "
            "too, never date or datetime — and all but `queue.verdict_column` nullable, since "
            "a skipped or auto-approved row carries no reviewer, timestamp or note. "
            "A reviewed column is ADDED beside its source, whose value is never modified in "
            "place: name it `reviewed_<source>` by default and declare it with the SAME spec "
            "as its source — type, enum and range alike — and nullability at least as "
            "permissive. A reviewed source column must be scalar (str/int/float/bool/date/"
            "datetime): a json or list column cannot be reviewed through a form field. No "
            "added column may reuse an input column's name, so two review stages in series "
            "must name their added columns differently. `queue.filter` may reference INPUT "
            "columns only, never a column this stage adds. "
            "This type emits one output row per input row and never removes any. The verdict "
            "column holds \"approve\" (a human accepted the value), \"modify\" (a human "
            "supplied a different one), or \"skipped\" (the queue filter did not select the "
            "row); a downstream stage that wants only human-sanctioned values filters on the "
            "verdict column != \"skipped\". "
            "Reviewed rows are matched to a cached human decision by fingerprinting the row "
            "itself — no column configuration enables that. Editing any queue column field, "
            "`filter` or `reviewer_instructions` changes the stage's definition fingerprint: "
            "every cached decision stops matching and every row is asked again."
        ),
    ),
}
