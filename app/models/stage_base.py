"""Everything a stage carries whatever its type: the type vocabulary, the shared
field list, and the base class each per-type stage model extends.

Sits below `app/models/stages/*`, which define those per-type models, and below
`app/models/stage.py`, which unions them into `Stage`.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Optional, get_args

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from app.models.schema import (
    SourceRef,
    StageConfig,
    TableSchema,
    _Base,
    _SNAKE_RE,
)
from app.models.stages.shared import find_internal_namespace_column_issues
from app.models.stages.stage_tests import StageTest, validate_stage_tests
from app.models.stages.warnings import CompilerWarning
from app.core.utils import compute_short_hash

if TYPE_CHECKING:
    # app.models.stages.code imports this module, so the reference stays lazy.
    from app.models.stages.code import AuthoredCode


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
    publish = "publish"
    # Both preserve exact per-row PROVENANCE (each output row traces to one
    # specific input row) but neither is grain-and-order preserving BY
    # POSITION: filter_rows drops rows, union interleaves rows from several
    # inputs. The runtime records their per-row provenance explicitly (see
    # app.runtime.lineage) so app.runtime.trace can still cross them.
    union = "union"
    filter_rows = "filter_rows"
    # Shaped on python_row_function — one input, mapped per row — but it runs a
    # separate program, named as an argv by its `external:` block instead of the
    # `function:` block every python type carries.
    external = "external"


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
    StageType.external,
})


def is_grain_and_order_preserving(stage_type: StageType) -> bool:
    """Does one input row of this stage type map to exactly one output row, in the
    same order? Fixed entirely by stage type — see the
    StageBase.is_grain_and_order_preserving property for the per-type contract."""
    return stage_type in _GRAIN_AND_ORDER_PRESERVING_TYPES


# ── Shared blocks ────────────────────────────────────────────────────────────
class ReviewConfig(_Base):
    """Routes a stage's outputs into human review."""
    when: Optional[str] = None
    routing: Optional[str] = None
    rationale: Optional[str] = None
    queue_name: Optional[str] = None


class StageInput(_Base):
    """Spelled `schema:` on a compiled stage; pydantic reserves `schema` on BaseModel."""
    id: str
    table_schema: TableSchema = Field(alias="schema")


# ── The shared field list ────────────────────────────────────────────────────
class StageCommon(_Base):
    """The fields every stage carries whether it is being authored (`StageDraft`)
    or stored (`StageBase` and its per-type subclasses), declared once so the two
    cannot drift apart."""
    id: str
    type: StageType
    name: str
    inputs: list[StageInput] = Field(default_factory=list)
    output_schema: Optional[TableSchema] = None

    # False declares this stage INTENTIONALLY non-deterministic — it must
    # re-roll every run — so the runtime consults no stage-result cache for its
    # rows. Not a performance knob, and deliberately absent from
    # compute_definition_fingerprint: it governs WHETHER the cache is consulted,
    # not WHAT the stage computes, so flipping it must never invalidate an
    # entry already recorded.
    cache: bool = True
    limit: Optional[int] = None
    compiler_notes: list[str] = Field(default_factory=list)

    @field_validator("inputs", mode="before")
    @classmethod
    def _bare_id_shorthand(cls, v: Any) -> Any:
        """Accept `inputs: [upstream_id]` shorthand for `[{id: upstream_id}]`."""
        if not isinstance(v, list):
            return v
        return [{"id": item} if isinstance(item, str) else item for item in v]

    @property
    def input_ids(self) -> list[str]:
        return [ref.id for ref in self.inputs]


# ── The per-type stage base ──────────────────────────────────────────────────
class StageBase(StageCommon):
    """One node in the workflow: the authored fields plus the ones only the
    server writes, and every rule a stored stage must satisfy regardless of its
    type. Each type's own required config blocks and input arity are declared by
    its subclass under `app/models/stages/`."""

    # False for the one type that emits files rather than a table (publish).
    REQUIRES_OUTPUT_SCHEMA: ClassVar[bool] = True

    # True for the types whose registered handler can execute one authored
    # StageTest — the types that may carry `tests`. A subclass that flips this on
    # also redeclares `tests` with the StageTest subclass stating its own arity;
    # __init_subclass__ below refuses the class otherwise.
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """A type whose handler runs tests must name the StageTest subclass stating its arity."""
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
            "Upstream dependencies: each is an upstream stage id plus the REQUIRED schema "
            "this stage expects that input to satisfy — which is just the upstream stage's "
            "output_schema."
        ),
    )
    output_schema: Optional[TableSchema] = Field(
        default=None,
        description=(
            "Columns this stage outputs, with an optional primary_key. REQUIRED for every "
            "type except `publish`."
        ),
    )
    source: Optional[SourceRef] = None
    review: Optional[ReviewConfig] = None

    # Descriptive eval note rendered on the stage page (reference data, metrics).
    # Display only — the executable eval contract is EvalConfig (app/models/eval.py).
    eval: Optional[dict[str, Any]] = None

    # Authored input→expected-output cases for python transforms — the stage's
    # reviewable behavior contract, run by app.runtime.stage_tests. None when the
    # stage has none: the model dump must not carry a `tests` key for
    # stages without tests, or every pre-existing belief hash would change.
    tests: Optional[Sequence[StageTest]] = None

    # ── the per-type hooks a subclass answers ────────────────────────────────
    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        """The config blocks that decide what this stage computes, keyed by the
        name they are spelled with on the stage."""
        raise NotImplementedError

    def find_config_column_issues(self) -> list[str]:
        """Every column this stage's config names that its own input edge cannot
        supply. [] for a type whose config names no column."""
        return []

    def find_output_schema_issues(self) -> list[str]:
        """Every way the declared output_schema is undeliverable. [] for a type
        whose internals, not its config, fix the output."""
        return []

    def find_authored_code_block(self) -> Optional["AuthoredCode"]:
        """The block holding code a reviewer would otherwise have to read (it
        carries `summary` and `corner_cases`), None for a stage fixed entirely by
        config."""
        return None

    def find_handle_compiler_warnings(self) -> list["CompilerWarning"]:
        """What the module owning this type's config block says; [] when a reviewer reads the
        config directly."""
        return []

    def llm_reply_schema(self) -> Optional[TableSchema]:
        """What an llm_transform's model reply itself must carry; None for every
        other type."""
        return None

    # ── the fingerprint ──────────────────────────────────────────────────────
    def compute_definition_fingerprint(self) -> str:
        """sha1[:16] over a sorted-key JSON dump of the output-determining subset
        of this stage: {"type", "output_schema"} plus one entry per block
        `fingerprint_blocks` names.
        Every other field (id, name, source, inputs, review, cache, limit,
        compiler_notes, eval, tests) is incidental — it does not change what this
        stage computes — and stays out, `cache` included: it decides whether the
        cache is consulted, not what the stage computes, so flipping it must not
        invalidate an existing entry. Each block itself is trimmed to its class's
        own `FINGERPRINT_FIELDS` (every config class declares
        `FINGERPRINT_FIELDS`/`INCIDENTAL_FIELDS` explicitly, exhaustively over its
        own fields — see e.g. QueueConfig, whose
        `routing`/`conflict_resolution`/`estimated_volume_per_week` route or match
        a decision without changing what the human is asked)."""
        output_dump = (
            self.output_schema.model_dump(mode="json", exclude_none=True)
            if self.output_schema is not None else None
        )
        fields: dict[str, Any] = {
            "type": self.type,
            "output_schema": output_dump,
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
        """Normalise `tests: []` to absent, so the model dump (and the
        belief hash computed over it) is identical whether the key was omitted
        or given empty."""
        return None if v == [] else v

    @model_validator(mode="after")
    def _tests_shape(self) -> "StageBase":
        """Tests belong only on a type whose handler can run them, at that type's arity."""
        if self.tests and not self.CARRIES_RUNNABLE_TESTS:
            raise ValueError(
                f"tests are only supported on stage types whose handler can run "
                f"them, not `{self.type}`"
            )
        validate_stage_tests(self.input_ids, list(self.tests or []))
        return self

    @model_validator(mode="after")
    def _schemas_declared(self) -> "StageBase":
        """Every schema this stage declares must be usable: non-empty, and naming no
        column in the reserved `_` namespace (find_internal_namespace_column_issues)."""
        issues = [
            f"input `{ref.id}` declares a schema with no columns"
            for ref in self.inputs
            if not ref.table_schema.columns
        ]
        if self.REQUIRES_OUTPUT_SCHEMA and not (
            self.output_schema and self.output_schema.columns
        ):
            issues.append("declares no output_schema")
        issues.extend(find_internal_namespace_column_issues(self))
        if issues:
            raise ValueError(f"type `{self.type}`: " + "; ".join(issues))
        return self

    @model_validator(mode="after")
    def _config_columns_resolve(self) -> "StageBase":
        """Every column this stage's config directly names (a join key, an
        aggregate group_by/value_column, publish.one_file_per, an llm prompt
        {placeholder} — single-braced, since a double-braced one is never
        injected) or references via a where/filter predicate (aggregate
        `where`, human_review_queue `filter`) must resolve against that
        reference's own input edge — `inputs[index].table_schema`, per
        `app.models.stages.shared.resolve_input_columns`. EDGE-ONLY: this says
        nothing about what an upstream producer itself declares, so it holds
        for a single stage in isolation, independent of the rest of any
        workflow. Cross-stage checks (unique ids, inputs resolve, acyclic) live
        in `workflow.graph_issues`."""
        issues = self.find_config_column_issues()
        if issues:
            raise ValueError("; ".join(issues))
        return self

    @model_validator(mode="after")
    def _output_schema_deliverable(self) -> "StageBase":
        """A declared output_schema must be deliverable by this stage's own
        config: for the types whose output is fixed by config (join, aggregate,
        union, filter_rows), every declared column must be producible by name,
        with the declared type matching the derivation where it can be known.
        EDGE-ONLY and per-stage, like _config_columns_resolve."""
        issues = self.find_output_schema_issues()
        if issues:
            raise ValueError("; ".join(issues))
        return self

    @property
    def is_grain_and_order_preserving(self) -> bool:
        """Does one input row map to exactly one output row, IN THE SAME ORDER?
        Grain-preserving means both: 1:1 (no rows added or dropped) AND order-
        preserving (the Nth output row was produced from the Nth input row).
        Declaring a type grain-preserving commits it to both — a stage that
        reordered rows would break the guarantee even at 1:1.

        This is the v1 eval gate AND the property a declarative (single-table,
        row-aligned) eval relies on to align a target's output rows back to the
        eval-dataset rows that produced them BY POSITION — no lineage id needed,
        because position IS the identity through a grain-preserving path. Fixed
        entirely by stage type (the module function is_grain_and_order_preserving):
          - python_row_function → yes (runtime maps it per row, in emit order — enforced 1:1)
          - python_frame_function → NO (may reshape OR reorder the frame)
          - llm_transform      → yes (per-row 1:1 in emit order in v1; a fan-out LLM
                                 like doc→pieces is out of scope until fan-out evals)
          - input_data         → yes (originates the rows)
          - human_review_queue → yes — the runtime maps it per row, so an output row
                                 is in its input row's position, and every input row
                                 produces one: a rejected row stays, carrying the
                                 rejection. Removing rows is a downstream filter
                                 stage's job, not this one's.
          - join (fan-out) / aggregate (fan-in) → NO; grain changes are deferred
          - publish            → NO — handle_publish runs an authored function whose
                                 output is a table of artifact paths, not the input
                                 rows (and it is terminal — nothing downstream).
          - external           → yes — one process per input row, in that row's
                                 position; it sees neither the frame nor its own
                                 row number, so it cannot fan out or reorder.
          - filter_rows / union → NO — a filter drops rows, a union interleaves rows
                                 from several inputs, so neither is 1:1-by-position.
                                 Each output row's exact source (stage id + row
                                 ordinal) is still recorded, in
                                 app.runtime.lineage, for the trace to follow.
        """
        return is_grain_and_order_preserving(self.type)


def find_stage_test_class(stage_cls: type[StageBase]) -> type[StageTest]:
    """The StageTest subclass this stage type's `tests` field holds — its test arity."""
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
