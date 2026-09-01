"""Everything a stage carries whatever its type: the type vocabulary, the shared
field list, and the base class each per-type stage model extends.

Sits below the per-type modules alongside it, and below `app/models/stage.py`,
which unions them into `Stage`.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    Optional,
    Union,
    get_args,
)

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from app.models.schema import (
    SourceRef,
    StageConfig,
    StageId,
    _Base,
    _SNAKE_RE,
)
from app.models.stages.shared import find_internal_namespace_column_issues
from app.models.stages.signature import (
    ReplacesSignature,
    TransformSignature,
    anchor_read_columns,
)
from app.models.stages.stage_tests import StageTest, validate_stage_tests
from app.models.stages.warnings import CompilerWarning
from app.core.utils import compute_short_hash
from app.core.ids import ID

if TYPE_CHECKING:
    # app.models.stages.code imports this module, so the reference stays lazy.
    from app.models.stages.code import AuthoredCode
    from app.models.workflow_stage import WorkflowStageInput


# ── Enumerated vocabularies ──────────────────────────────────────────────────
class StageType(str, Enum):
    input_data = "input_data"
    llm_transform = "llm_transform"
    # Two Python transforms, distinguished by how the runtime invokes them (which
    # is what makes the grain guarantee real rather than a claim):
    #   python_row_function   — runtime maps the function over the input's rows,
    #                           one row in → one row out. It never sees the frame,
    #                           so it *cannot* fan out / fan in. Prefer this.
    #   python_frame_function — runtime hands it the whole frame(s); it may reshape
    #                           (group-by, pivot, dedup, multi-input merge).
    python_row_function = "python_row_function"
    python_frame_function = "python_frame_function"
    # Two LEFT joins over exactly two inputs (inputs[0] = subject,
    # inputs[1] = reference), differing ONLY in the cardinality they permit —
    # which is why the TYPE carries it rather than a config field:
    #   enrich — the reference must be unique on the key (m:1). The runtime asks
    #            pandas to VERIFY that, so a non-unique reference fails the run
    #            instead of silently multiplying rows.
    #   expand — the reference may repeat (m:n): deliberate fan-out.
    # Neither ever drops a subject row: an unmatched subject survives carrying
    # nulls. Dropping rows is filter_rows' job, because filter_rows records
    # per-row provenance and a join that silently discarded rows would be
    # invisible downstream.
    enrich = "enrich"
    expand = "expand"
    aggregate = "aggregate"
    human_review_queue = "human_review_queue"
    report = "report"
    # Both preserve exact per-row PROVENANCE (each output row traces to one
    # specific input row) but neither is grain-and-order preserving BY
    # POSITION: filter_rows drops rows, union interleaves rows from several
    # inputs. The runtime records their per-row provenance explicitly (see
    # app.runtime.lineage) so app.runtime.trace can still cross them.
    union = "union"
    filter_rows = "filter_rows"
    starlark_row_function = "starlark_row_function"
    starlark_filter_rows = "starlark_filter_rows"
    # Three declared reshapes, each recording per-row provenance the runtime works
    # out itself — which is what separates them from a python_frame_function doing
    # the same pandas call and taking the trace with it.
    explode = "explode"
    dedupe = "dedupe"
    sort_rank = "sort_rank"



# The stage types that guarantee output row i came from input row i — 1:1 and in
# the same order. This is owned by core (app.models) because it's a fact about the
# stage-type contract that several layers must read — the eval gate, the SYW
# positional tracer, the compiler — and only core is importable by all of them.
# Ask it through is_grain_and_order_preserving(); the runtime handler registry is
# held to conform (app/runtime/stages checks each type's shape against it at import).
_GRAIN_AND_ORDER_PRESERVING_TYPES: frozenset[StageType] = frozenset({
    StageType.input_data,
    StageType.python_row_function,
    StageType.llm_transform,
    StageType.human_review_queue,
    StageType.starlark_row_function,
})


def is_grain_and_order_preserving(stage_type: StageType) -> bool:
    return stage_type in _GRAIN_AND_ORDER_PRESERVING_TYPES



# ── Shared blocks ────────────────────────────────────────────────────────────
class ReviewConfig(_Base):
    when: Optional[str] = None
    routing: Optional[str] = None
    rationale: Optional[str] = None
    queue_name: Optional[str] = None


class StageInput(_Base):
    id: StageId


# ── The one name, and the line under it ──────────────────────────────────────
# A stage has ONE name — its `id` — and every surface shows that one: the graph
# node, every heading, the URL, the `inputs` of downstream stages, the run
# manifest. `description` explains that name and is never rendered as a name, so
# a reader is never asked to hold two strings for one stage.
# The ceilings are what keep them in their roles. Measured against all 895 stored
# stages and all 138 compiled ones when they were set: the longest id was 35
# characters and the longest description 96, so neither refuses anything already
# written.
STAGE_ID_MAX_CHARS = 60
# Tighter than SUMMARY_MAX_CHARS (app.models.stages.code): a summary explains code
# in a paragraph a non-engineer reads; this is one line under a heading and a graph
# tooltip, and stops being one line at a paragraph's length.
STAGE_DESCRIPTION_MAX_CHARS = 200

STAGE_ID_DESCRIPTION = (
    "The stage's ONE name, snake_case. Every surface shows this and only this — the "
    "workflow graph node, every page heading, the URL, the run manifest, and the "
    "`inputs` of every downstream stage — so name the step well enough that a reader "
    f"needs no gloss. HARD LIMIT: {STAGE_ID_MAX_CHARS} characters, refused above that."
)
WORKFLOW_OUTPUTS_DESCRIPTION = (
    "What this workflow publishes as results, each with a slug that identifies it across "
    "runs and a label a reader sees. `kind` says which of two.\n"
    "`figure` names one COLUMN and publishes its one cell. A stage declaring one must "
    "output exactly one row — aggregate first. Refused rather than conventional: a "
    "scalar read out of a many-row frame drifts to a different row when the data "
    "changes, and the value it returns never looks wrong.\n"
    "`table` publishes this stage's rows, each linking to its own lineage. `columns` "
    "names the ones a reader gets; omit it for every column the stage produced. A column "
    "the stage did not produce stops the run.\n"
    "A `figure` may also name the `shape_id` it fills — one of the claim shapes this "
    "project has authored, by its id. Read them first; never coin one here, and leave it "
    "out rather than guessing, because a figure claiming the wrong coverage is worse than "
    "one claiming none.\n"
    "Mark `primary` on the one or two a reader should see first — the run leads with "
    "those and lists the rest, so marking everything primary is the same as marking "
    "nothing. A primary table is drawn on the run page; a secondary one is a line and a "
    "link."
)


STAGE_DESCRIPTION_DESCRIPTION = (
    "ONE line saying what this step does, shown UNDER the id and as the graph node's "
    "tooltip — never as a heading and never as a label, so it must not restate the id "
    "in prose. Say what the id cannot: the reason the step exists, what it decides, "
    "which snapshot it is. Plain language, no Python vocabulary. HARD LIMIT: "
    f"{STAGE_DESCRIPTION_MAX_CHARS} characters, refused above that."
)


# ── The shared field list ────────────────────────────────────────────────────
class _WorkflowOutputFields(_Base):
    # A slug identifies the output across runs; what it holds is per run.
    slug: str
    label: str
    # Whether the run leads with this. Mark what the work rests on, not everything.
    primary: bool = False


class WorkflowFigureRule(_WorkflowOutputFields):
    kind: Literal["figure"]
    column: str
    # The project's claim shape this fills; without one the figure cannot be minted.
    shape_id: Optional[ID] = None


class WorkflowTableRule(_WorkflowOutputFields):
    kind: Literal["table"]
    # None publishes every column the stage produced, so a whole frame stays one word.
    columns: Optional[list[str]] = None


WorkflowOutputRule = Annotated[
    Union[WorkflowFigureRule, WorkflowTableRule], Field(discriminator="kind")
]


class AuthoredStageFields(_Base):
    id: ID = Field(max_length=STAGE_ID_MAX_CHARS, description=STAGE_ID_DESCRIPTION)
    type: StageType
    description: str = Field(
        max_length=STAGE_DESCRIPTION_MAX_CHARS, description=STAGE_DESCRIPTION_DESCRIPTION
    )
    inputs: list[StageInput] = Field(default_factory=list)

    # Whether the runtime records this stage's results and replays them on a
    # later run. Off by default: recomputing costs less than fingerprinting the
    # input for every type but llm_transform and human_review_queue, which
    # redeclare it True because their recompute spends a model call or a human's
    # attention. An author turns it on where the code is expensive enough to be
    # worth the storage. Deliberately absent from compute_definition_fingerprint:
    # it governs WHETHER the cache is consulted, not WHAT the stage computes, so
    # flipping it must never invalidate an entry already recorded.
    cache: bool = False
    # Why this type ignores `cache`; the refusal quotes it. None means it honours one.
    CACHE_IGNORED_BECAUSE: ClassVar[Optional[str]] = None
    compiler_notes: list[str] = Field(default_factory=list)

    # The authored contract of what this stage reads and writes, per input —
    # separate from the mechanism (code, prompt, join keys) that honours it.
    # Declared here so StageDraft carries it too: the signature is authored, not
    # server-written. Each stored per-type model narrows it to its one form
    # (ExtendsSignature for the anchored family, ReplacesSignature for the
    # reshaping family — app.models.stages.signature) and REQUIRES it: a stage's
    # output schema resolves from the signature and nothing else. The draft
    # keeps the permissive optional union, per the StageDraft philosophy. It is
    # checked against the config (find_signature_config_issues) and against what
    # the inputs supply (find_signature_issues).
    signature: Optional[TransformSignature] = None
    # Authored, so the draft carries it.
    workflow_outputs: Optional[list[WorkflowOutputRule]] = Field(
        default=None, description=WORKFLOW_OUTPUTS_DESCRIPTION
    )

    @field_validator("inputs", mode="before")
    @classmethod
    def _bare_id_shorthand(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        return [{"id": item} if isinstance(item, str) else item for item in v]

    @property
    def input_ids(self) -> list[ID]:
        return [ref.id for ref in self.inputs]

    def list_published_figures(self) -> list[WorkflowFigureRule]:
        return [r for r in self.workflow_outputs or [] if isinstance(r, WorkflowFigureRule)]

    def list_published_tables(self) -> list[WorkflowTableRule]:
        return [r for r in self.workflow_outputs or [] if isinstance(r, WorkflowTableRule)]


# ── The per-type stage base ──────────────────────────────────────────────────
class AbstractStage(AuthoredStageFields):
    # Narrowed from the optional on AuthoredStageFields: a stored stage always has one.
    signature: TransformSignature

    REQUIRES_OUTPUT_SCHEMA: ClassVar[bool] = True

    # True for the types whose registered handler can execute one authored
    # StageTest — the types that may carry `tests`. A subclass that flips this on
    # also redeclares `tests` with the StageTest subclass stating its own arity;
    # __init_subclass__ below refuses the class otherwise.
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        own_fields = vars(cls).get("__annotations__", {})
        if cls.CARRIES_RUNNABLE_TESTS and "tests" not in own_fields:
            raise TypeError(
                f"{cls.__name__} carries runnable tests but declares no `tests` field of "
                f"its own — name the StageTest subclass whose `expected` states its arity"
            )

    inputs: list[StageInput] = Field(
        default_factory=list,
        description=(
            "Upstream dependencies, each naming one upstream stage id. What that input "
            "supplies is the upstream stage's own output schema, so it is never restated "
            "here."
        ),
    )
    source: Optional[SourceRef] = None
    review: Optional[ReviewConfig] = None

    # Descriptive eval note rendered on the stage page (reference data, metrics).
    # Display only — the executable eval contract is EvalConfig (app/models/eval.py).
    eval: Optional[dict[str, Any]] = None

    # Authored input→expected-output cases for python transforms — the stage's
    # reviewable behavior contract, run by app.runtime.stage_tests. None when the
    # stage has none, so the model dump carries no `tests` key for a stage
    # without tests.
    tests: Optional[Sequence[StageTest]] = None

    # ── the per-type hooks a subclass answers ────────────────────────────────
    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        raise NotImplementedError

    def find_authored_code_block(self) -> Optional["AuthoredCode"]:
        return None

    def find_handle_compiler_warnings(self) -> list["CompilerWarning"]:
        return []

    def anchor_reads(self) -> frozenset[str]:
        return frozenset(column.name for column in anchor_read_columns(self))

    # Answerable from the stage alone, so it runs at construction (below).
    def find_signature_config_issues(self) -> list[str]:
        return []

    # Told its resolved input schemas rather than holding them: both are run by
    # app.models.workflow, which is what can resolve them.
    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return []

    def find_signature_schema_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return []

    # ── the fingerprint ──────────────────────────────────────────────────────
    def compute_definition_fingerprint(self) -> str:
        """Excludes `cache`: it decides whether the cache is consulted, not what the stage computes."""
        fields: dict[str, Any] = {
            "type": self.type,
            "signature": self.signature.model_dump(mode="json", exclude_none=True),
            **{
                name: _trim_block_to_fingerprint_fields(block)
                for name, block in self.fingerprint_blocks().items()
            },
        }
        payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
        return compute_short_hash(payload)

    # ── the type-independent rules ───────────────────────────────────────────
    @field_validator("id")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"id {v!r} should be snake_case")
        return v

    @field_validator("tests", mode="before")
    @classmethod
    def _empty_tests_means_absent(cls, v: Any) -> Any:
        """So the dumped spec is identical whether `tests` was omitted or given empty."""
        return None if v == [] else v

    @model_validator(mode="after")
    def _tests_shape(self) -> "AbstractStage":
        if self.tests and not self.CARRIES_RUNNABLE_TESTS:
            raise ValueError(
                f"tests are only supported on stage types whose handler can run "
                f"them, not `{self.type}`"
            )
        validate_stage_tests(self.input_ids, list(self.tests or []))
        return self

    @model_validator(mode="after")
    def _schemas_declared(self) -> "AbstractStage":
        issues: list[str] = []
        if self.REQUIRES_OUTPUT_SCHEMA and isinstance(
            self.signature, ReplacesSignature
        ) and not self.signature.produces:
            issues.append(
                "its signature produces no columns — only report emits no table"
            )
        issues.extend(find_internal_namespace_column_issues(self))
        if issues:
            raise ValueError(f"type `{self.type}`: " + "; ".join(issues))
        return self

    @model_validator(mode="after")
    def _signature_consistent(self) -> "AbstractStage":
        issues = self.find_signature_config_issues()
        if issues:
            raise ValueError("; ".join(issues))
        return self

    @property
    def is_grain_and_order_preserving(self) -> bool:
        return is_grain_and_order_preserving(self.type)


def find_stage_test_class(stage_cls: type[AbstractStage]) -> type[StageTest]:
    sequence_type, _none_type = get_args(stage_cls.model_fields["tests"].annotation)
    (test_class,) = get_args(sequence_type)
    assert issubclass(test_class, StageTest)  # __init_subclass__ admits nothing else
    return test_class


def _trim_block_to_fingerprint_fields(block: StageConfig) -> dict[str, Any]:
    dump = block.model_dump(mode="json", exclude_none=True)
    return {
        key: value for key, value in dump.items()
        if key in type(block).FINGERPRINT_FIELDS
    }
