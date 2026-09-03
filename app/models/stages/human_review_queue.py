"""human_review_queue stage: the config block naming the columns the stage ADDS,
the reviewer's verdict vocabulary, and the checks that every named column
resolves — sources against what the input supplies, added names against the signature."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Literal, Mapping, Optional, Sequence

from pydantic import Field, field_validator

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate
from app.models.schema import (
    SCALAR_COLUMN_TYPES,
    STR_COLUMN_TYPE,
    Column,
    StageConfig,
    TableSchema,
    _Base,
)
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import find_predicate_column_issues
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class ReviewVerdict(str, Enum):
    """`skipped` is written by the runtime for a row the filter did not select; no client may post it."""
    approve = "approve"
    modify = "modify"
    skipped = "skipped"


class SortDirection(str, Enum):
    ascending = "ascending"
    descending = "descending"


# The queued row carries only the signature's reads, so an unread column cannot order it.
class QueueSortKey(_Base):
    column: str = Field(
        description=(
            "An input column to order the queue by. The signature must also read it, "
            "and its declared type must be scalar — a list or json column has no order."
        ),
    )
    direction: SortDirection = Field(
        description=(
            "`descending` puts the largest value (latest date, `true`) first. A row "
            "whose value is null sorts last whichever direction this is."
        ),
    )


class QueueConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "filter", "reviewer_instructions", "reviewed_columns", "context_columns",
        "verdict_column", "reviewer_column", "reviewed_at_column", "review_notes_column",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "routing", "conflict_resolution", "estimated_volume_per_week", "sort",
    })

    filter: Optional[str] = None
    reviewer_instructions: Optional[str] = None
    reviewed_columns: dict[str, str] = Field(
        description=(
            "Each input column the human reviews, mapped to the name of the column this "
            "stage adds carrying the reviewed value: {source column -> reviewed column}."
        ),
    )
    context_columns: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional ordered input columns shown beside the editable columns. Omit it "
            "to show every input column the signature reads except reviewed columns; "
            "use an empty list to show no additional context."
        ),
    )
    verdict_column: str
    reviewer_column: str
    reviewed_at_column: str
    review_notes_column: Optional[str] = Field(
        default=None,
        description=(
            "Optional: name the column a reviewer's free-text note lands in. Omit it and "
            "the stage neither offers a notes box nor adds a notes column. That column's "
            "`description` is the label over the box, which otherwise reads `Notes` — "
            "write one only where the review needs specific documentation."
        ),
    )
    sort: list[QueueSortKey] = Field(
        default_factory=list,
        description=(
            "Optional: the order a human works this queue in, most significant key "
            "first. Declare it when some rows deserve the reviewer's attention before "
            "the rest — largest `amount_usd` first, so the costliest errors are caught "
            "while the reviewer is fresh. Leave it empty and rows are reviewed in "
            "whatever order the upstream stage happened to produce. This orders the "
            "review, not the stage's output, and it never changes WHAT is reviewed, so "
            "editing it leaves decisions already recorded intact."
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

    def build_reviewed_row(
        self,
        row: Mapping[str, object],
        *,
        verdict: str,
        reviewed_values: Mapping[str, object],
        reviewer: object,
        reviewed_at: object,
        review_notes: object,
    ) -> dict[str, object]:
        """The row a decision produces, human or the runtime's own skip/approve."""
        output_row: dict[str, object] = {
            **row,
            **reviewed_values,
            self.verdict_column: verdict,
            self.reviewer_column: reviewer,
            self.reviewed_at_column: reviewed_at,
        }
        if self.review_notes_column is not None:
            output_row[self.review_notes_column] = review_notes
        return output_row


class HumanReviewQueueStage(AbstractStage):
    type: Literal[StageType.human_review_queue]
    queue: QueueConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ExtendsSignature
    # The ledger replays a decision now, so the row cache must not race it for a row.
    cache: bool = False

    @field_validator("cache")
    @classmethod
    def _cache_never_arms_this_stage(cls, v: bool) -> bool:
        """Stored specs predate the ledger and still say true; refusing them would strand a version."""
        return False

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"queue": self.queue}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        sid, queue = self.id, self.queue
        input_schema = inputs[0].table_schema
        return (
            _find_duplicate_added_names(sid, queue)
            + _find_filter_issues(sid, queue, input_schema)
            + _find_sort_issues(sid, queue, input_schema)
            + _find_context_column_issues(sid, queue, self.signature, input_schema)
            + _find_reviewed_source_issues(sid, queue, input_schema)
            + _find_added_column_collisions(sid, queue, input_schema)
        )

    # `queue` names the added columns, and `_find_added_column_collisions` is what
    # enforces "a review stage adds columns and never overwrites one" — it also catches
    # two review stages in series reusing names. What this owns is the tie between the
    # two accounts: the signature's adds and the queue block must name the SAME columns,
    # each add carrying a spec the review runtime actually writes, and a rewrite is never
    # claimable. Given that, the signature's own anchor-collision check
    # (signature._find_extends_issues) can only ever restate the config check's verdict.
    def find_signature_config_issues(self) -> list[str]:
        return _find_unread_column_issues(self.id, self.queue, self.signature)

    # The rest of the queue-vs-signature cross-check runs together, beside the
    # collision check in find_config_column_issues: they are one verdict on one
    # question — whether the signature adds exactly what the queue block names —
    # and half of it needs the input schema, so a reader sees all of it or none.
    def find_signature_schema_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        declared = {name for _, name in find_added_columns(self.queue)}
        issues = [
            f"stage '{self.id}': signature adds `{column.name}`, which the review "
            f"runtime never writes — this stage adds exactly the columns its queue "
            f"block names ({sorted(declared)})"
            for column in self.signature.adds
            if column.name not in declared
        ]
        if self.signature.rewrites:
            issues.append(
                f"stage '{self.id}': human_review_queue never revises an input "
                f"column; rewrites are not supported"
            )
        adds_by_name = _index_adds_by_name(self.signature)
        return (
            issues
            + _find_reviewed_target_issues(
                self.id, self.queue, inputs[0].table_schema, adds_by_name)
            + _find_review_record_target_issues(self.id, self.queue, adds_by_name)
        )


def _index_adds_by_name(signature: ExtendsSignature) -> dict[str, Column]:
    return {column.name: column for column in signature.adds}



def resolve_queue_config(stage: AbstractStage) -> Optional[QueueConfig]:
    """The only sanctioned access to `.queue` (tests/arch/test_handle_access_is_owned.py)."""
    return stage.queue if isinstance(stage, HumanReviewQueueStage) else None


def find_queue_column_issues(
    stage: HumanReviewQueueStage, inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    return (
        stage.find_config_column_issues(inputs)
        + stage.find_signature_config_issues()
        + stage.find_signature_schema_issues(inputs)
    )


def find_added_columns(queue: QueueConfig) -> list[tuple[str, str]]:
    added = [
        (f"queue.reviewed_columns['{source}']", target)
        for source, target in queue.reviewed_columns.items()
    ]
    return added + find_review_record_columns(queue)


def find_review_record_columns(queue: QueueConfig) -> list[tuple[str, str]]:
    columns = [
        ("queue.verdict_column", queue.verdict_column),
        ("queue.reviewer_column", queue.reviewer_column),
        ("queue.reviewed_at_column", queue.reviewed_at_column),
    ]
    if queue.review_notes_column is not None:
        columns.append(("queue.review_notes_column", queue.review_notes_column))
    return columns


# ── the individual checks ────────────────────────────────────────────────────
def _find_unread_column_issues(
    sid: str, queue: QueueConfig, signature: ExtendsSignature
) -> list[str]:
    # The human sees the narrowed row, so an unread column is one they never see.
    read = {column.name for entry in signature.reads for column in entry.columns}
    if not read:
        return [
            f"stage '{sid}': its signature reads nothing, so the row this stage "
            f"queues for a human would carry no columns — declare what the "
            f"reviewer needs to see"
        ]
    try:
        tested = set(parse_predicate(queue.filter, read).columns) if queue.filter else set()
    except PredicateError:
        tested = set()  # _find_filter_issues already reports the bad predicate
    return [
        f"stage '{sid}': queue.filter tests `{name}` but the signature does not read it"
        for name in sorted(tested - read)
    ] + [
        f"stage '{sid}': queue.sort orders by `{key.column}` but the signature does "
        f"not read it, so the queued row does not carry it"
        for key in queue.sort
        if key.column not in read
    ]



def _find_filter_issues(sid: str, queue: QueueConfig, input_schema: TableSchema) -> list[str]:
    if not queue.filter:
        return []
    return find_predicate_column_issues(
        queue.filter, stage_id=sid, field="queue.filter",
        cols={c.name for c in input_schema.columns},
    )


def _find_sort_issues(sid: str, queue: QueueConfig, input_schema: TableSchema) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    for key in queue.sort:
        column = input_schema.column_for_name(key.column)
        if column is None:
            issues.append(
                f"stage '{sid}': queue.sort orders by column '{key.column}' not in its "
                f"input schema "
                f"(declares {sorted(c.name for c in input_schema.columns)})"
            )
        elif column.type not in SCALAR_COLUMN_TYPES:
            issues.append(
                f"stage '{sid}': queue.sort orders by column '{key.column}', which is "
                f"type '{column.type}' — that has no order to put the queue in "
                f"(orderable types: {sorted(SCALAR_COLUMN_TYPES)})"
            )
        if key.column in seen:
            issues.append(
                f"stage '{sid}': queue.sort names column '{key.column}' more than "
                f"once — a column places a row in the queue once"
            )
        seen.add(key.column)
    return issues


def _find_context_column_issues(
    sid: str, queue: QueueConfig, signature: ExtendsSignature,
    input_schema: TableSchema,
) -> list[str]:
    if queue.context_columns is None:
        return []
    declared = {column.name for column in input_schema.columns}
    reviewed = set(queue.reviewed_columns)
    read = {column.name for entry in signature.reads for column in entry.columns}
    issues: list[str] = []
    seen: set[str] = set()
    for name in queue.context_columns:
        if name not in declared:
            issues.append(
                f"stage '{sid}': queue.context_columns names column '{name}' not in "
                f"its input schema (declares {sorted(declared)})"
            )
        elif name not in read:
            issues.append(
                f"stage '{sid}': queue.context_columns names `{name}` but the "
                "signature does not read it, so the queued row does not carry it"
            )
        if name in reviewed:
            issues.append(
                f"stage '{sid}': queue.context_columns names reviewed column '{name}' "
                "— a column cannot be both editable and context"
            )
        if name in seen:
            issues.append(
                f"stage '{sid}': queue.context_columns names column '{name}' more "
                "than once"
            )
        seen.add(name)
    return issues


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
    existing ={c.name for c in input_schema.columns}
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

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "human_review_queue": StageTypeSpec(
        summary="Pulls flagged rows for human decision; halts the run.",
        signature_form="extends",
        blocks=["queue"],
        requires_inputs=True,
        min_inputs=1,
        required=["reviewed_columns", "verdict_column", "reviewer_column",
                     "reviewed_at_column"],
        optional=["filter", "reviewer_instructions", "review_notes_column",
                     "context_columns", "sort",
                     "routing", "conflict_resolution", "estimated_volume_per_week"],
        notes=(
            "Output columns are the input columns PLUS exactly what its `queue` block names: "
            "one added column per `queue.reviewed_columns` entry, `queue.verdict_column`, "
            "`queue.reviewer_column`, `queue.reviewed_at_column`, and "
            "`queue.review_notes_column` when declared; the signature `adds` all of them. "
            "Those four record-of-review columns are declared type str — "
            "`queue.reviewed_at_column` too, never date or datetime — and all but the "
            "verdict column nullable. A reviewed column is ADDED beside its source, "
            "never modifying it: name it `reviewed_<source>` and declare it with the "
            "SAME spec, nullability at least as permissive. Its source must be scalar. "
            "`queue.filter` may reference INPUT columns only. Without "
            "`queue.context_columns`, the reviewer sees every column the signature `reads` "
            "except the reviewed columns. When declared, `queue.context_columns` is the "
            "ordered subset shown beside the reviewed columns; each must be an input column "
            "the signature reads and none may also be reviewed. Signature reads must cover "
            "every column `queue.filter` tests, and may never be empty. The verdict column holds "
            "\"approve\", \"modify\", or \"skipped\" (the filter did not select the row), so "
            "a downstream stage wanting only human-sanctioned values filters on != "
            "\"skipped\". `queue.sort` declares the order a human works the queue in; "
            "like `queue.filter` it may name INPUT columns only, each one scalar and read "
            "by the signature. Rows match a cached decision by fingerprinting the row itself; "
            "editing any queue field, `filter` or `reviewer_instructions` changes the "
            "stage fingerprint and every row is asked again."
        ),
    ),
}
