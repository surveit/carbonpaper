"""Stage-level contract: the node types, their executable-handle blocks, and the
Stage model. Constructing a model validates it.

Models ignore unknown keys (compiled stage JSON carries fields we pass through) but are
strict about the fields declared here.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, ClassVar, Optional

from pydantic import (
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from app.models.schema import (
    FunctionKind,
    SourceRef,
    TableSchema,
    _Base,
    _SNAKE_RE,
)
from app.models.stages.aggregate import AggregateConfig
from app.models.stages.code import SUMMARY_DESCRIPTION, validate_inline_function_code
from app.models.stages.filter_rows import FilterConfig
from app.models.stages.human_review_queue import QueueConfig
from app.models.stages.input_data import Connector
from app.models.stages.join import JoinConfig
from app.models.stages.llm_transform import LLMConfig, find_llm_one_to_one_issues
from app.models.stages.publish import PublishConfig
from app.models.stages.shared import find_internal_namespace_column_issues
from app.models.stages.stage_tests import StageTest, validate_stage_tests
from app.models.stages.union import UnionConfig
from app.core.utils import compute_short_hash, format_errors

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
    # Trailing underscore: a member literally named `join` would shadow
    # str.join on every instance. The value — what a compiled stage declares and
    # StageType("join") looks up — is still "join".
    join_ = "join"
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
})


def is_grain_and_order_preserving(stage_type: StageType) -> bool:
    """Does one input row of this stage type map to exactly one output row, in the
    same order? Fixed entirely by stage type — see the
    Stage.is_grain_and_order_preserving property for the per-type contract."""
    return stage_type in _GRAIN_AND_ORDER_PRESERVING_TYPES


# ── Executable-handle blocks (each self-validates) ───────────────────────────
class PythonFunction(_Base):
    """Handle for python_row_function / python_frame_function (and publish). The
    row-vs-frame distinction lives in the stage `type`, not here — the runtime
    reads the type to decide whether to invoke this per row or per frame."""
    # Every field changes what this stage computes (the code/module it runs)
    # except `summary`, which describes that code to a reader — see
    # Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "kind", "code", "module", "function", "requirements",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary"})

    kind: FunctionKind
    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    code: Optional[str] = Field(
        default=None,
        description=(
            "Inline Python defining `function` (default `transform`). Signature by stage "
            "type: python_row_function `def transform(row: dict) -> dict` (1 row in, 1 out; "
            "cannot reorder or fan out); python_frame_function "
            "`def transform(df, ...) -> DataFrame` (inputs positional in declared order); "
            "publish `def transform(df, ..., output_dir, trace_links) -> DataFrame` (writes "
            "artifact files into output_dir; the returned frame lists them)."
        ),
    )
    module: Optional[str] = None
    function: Optional[str] = None
    requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_fields(self) -> "PythonFunction":
        if self.kind == FunctionKind.module and not self.module:
            raise ValueError("function.kind=module needs `module`")
        if self.kind == FunctionKind.inline and not self.code:
            raise ValueError("function.kind=inline needs `code`")
        return self

    @model_validator(mode="after")
    def _inline_code_is_runnable(self) -> "PythonFunction":
        """Inline code must parse and define the function the runtime calls
        (`transform` by default). Enforced here — a single stage's invariant — so
        broken code (e.g. a bare body with a top-level `return`) is rejected at
        write time instead of raising only when the runner exec()s it."""
        if self.kind != FunctionKind.inline or not self.code:
            return self
        validate_inline_function_code(self.code, self.function)
        return self


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


# ── Stage ────────────────────────────────────────────────────────────────────
# type → which handle block it must carry, plus input arity. Keyed by the plain
# value string, not the enum member: with `use_enum_values`, `self.type` is a
# str at runtime, and str-enum members hash by *name* (StageType.join_ hashes
# as "join_", not "join") — a member-keyed dict would silently miss the lookup.
_TYPE_SPEC: dict[str, dict[str, Any]] = {
    "input_data":            {"handle": "connector", "requires_inputs": False, "min_inputs": 0},
    "llm_transform":         {"handle": "llm",       "requires_inputs": True,  "min_inputs": 1},
    "python_row_function":   {"handle": "function",  "requires_inputs": True,  "min_inputs": 1, "max_inputs": 1},
    "python_frame_function": {"handle": "function",  "requires_inputs": True,  "min_inputs": 1},
    "join":                  {"handle": "join",      "requires_inputs": True,  "min_inputs": 2},
    "aggregate":             {"handle": "aggregate", "requires_inputs": True,  "min_inputs": 1},
    "human_review_queue":    {"handle": "queue",     "requires_inputs": True,  "min_inputs": 1},
    "publish":               {"handle": "publish",   "also_requires": ["function"], "requires_inputs": True, "min_inputs": 1},
    "union":                 {"handle": "union",     "requires_inputs": True,  "min_inputs": 2},
    "filter_rows":           {"handle": "filter",    "requires_inputs": True,  "min_inputs": 1, "max_inputs": 1},
}


# Stage fields an authoring client never writes, so StageDraft does not declare
# them. A client that echoes back a stage it read from the server carries them
# anyway; the draft drops those rather than refusing the whole stage.
SERVER_OWNED_STAGE_FIELDS = ("tests", "eval", "review", "source")


class StageDraft(_Base):
    """One stage as an authoring client submits it. Carries no cross-field
    validator: a stage that breaks a rule must parse here and be refused by
    `Stage` in the handler, where the refusal reaches the client on the handler's
    own channel rather than as a parameter-binding error. `Stage` extends this,
    so the shared field list is declared once."""
    id: str
    type: StageType
    name: str
    inputs: list[StageInput] = Field(default_factory=list)
    output_schema: Optional[TableSchema] = None

    # executable handles (exactly one populated, per type — enforced by Stage)
    connector: Optional[Connector] = None
    llm: Optional[LLMConfig] = None
    function: Optional[PythonFunction] = None
    join: Optional[JoinConfig] = None
    aggregate: Optional[AggregateConfig] = None
    queue: Optional[QueueConfig] = None
    publish: Optional[PublishConfig] = None
    union: Optional[UnionConfig] = None
    filter: Optional[FilterConfig] = None

    # False declares this stage INTENTIONALLY non-deterministic — it must
    # re-roll every run — so the runtime consults no stage-result cache for its
    # rows. Not a performance knob, and deliberately absent from
    # compute_definition_fingerprint: it governs WHETHER the cache is consulted,
    # not WHAT the stage computes, so flipping it must never invalidate an
    # entry already recorded.
    cache: bool = True
    limit: Optional[int] = None
    compiler_notes: list[str] = Field(default_factory=list)

    # Which SERVER_OWNED_STAGE_FIELDS the submitted draft carried, for the caller
    # to warn about. Bookkeeping about one submission, not part of a stage: kept
    # out of the JSON schema a client is handed and out of every dump. Always
    # empty on `Stage`, whose own fields these are.
    dropped_server_owned_fields: SkipJsonSchema[list[str]] = Field(
        default_factory=list, exclude=True
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_server_owned_fields(cls, data: Any) -> Any:
        """Accept and discard the fields only the server writes, so a client can
        echo back a stage it read without tripping `extra="forbid"`. Cannot
        raise, and does not run for `Stage`, which owns these fields."""
        if cls is not StageDraft or not isinstance(data, dict):
            return data
        present = [name for name in SERVER_OWNED_STAGE_FIELDS if name in data]
        remaining = {k: v for k, v in data.items() if k not in SERVER_OWNED_STAGE_FIELDS}
        remaining["dropped_server_owned_fields"] = present
        return remaining

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

    def to_stage_spec(self) -> dict[str, Any]:
        """This draft as a dict `Stage.model_validate` accepts — by alias, so
        `StageInput.table_schema` spells itself `schema:` the way a compiled stage
        does."""
        return self.model_dump(exclude_unset=True, by_alias=True)


class Stage(StageDraft):
    """One node in the workflow: a submitted draft plus the fields only the
    server writes, and every rule a stored stage must satisfy. Exactly one handle
    block is required, selected by `type`."""
    source: Optional[SourceRef] = None
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
    review: Optional[ReviewConfig] = None

    # Descriptive eval note rendered on the stage page (reference data, metrics).
    # Display only — the executable eval contract is EvalConfig (app/models/eval.py).
    eval: Optional[dict[str, Any]] = None

    # Authored input→expected-output cases for python transforms — the stage's
    # reviewable behavior contract, run by app.runtime.stage_tests. None when the
    # stage has none: the model dump must not carry a `tests` key for
    # stages without tests, or every pre-existing belief hash would change.
    tests: Optional[list[StageTest]] = None

    def compute_definition_fingerprint(self) -> str:
        """sha1[:16] over a sorted-key JSON dump of the output-determining subset of
        this stage: {"type", "handle": <the type's handle block>, "output_schema"}.
        Every other Stage field (id, name, source, inputs, review, cache,
        limit, compiler_notes, eval, tests) is incidental — it does not change what
        this stage computes — and stays out of the fingerprint, `cache`
        included: it decides whether the cache is consulted, not what the stage
        computes, so flipping it must not invalidate an existing entry. The handle
        block itself is trimmed to its class's own `FINGERPRINT_FIELDS` (every
        handle config class declares `FINGERPRINT_FIELDS`/`INCIDENTAL_FIELDS`
        explicitly, exhaustively over its own fields — see e.g. QueueConfig,
        whose `routing`/`conflict_resolution`/`estimated_volume_per_week`
        route or match a decision without changing what the human is asked)."""
        spec = _TYPE_SPEC[self.type]
        handle = getattr(self, spec["handle"])
        handle_dump = handle.model_dump(mode="json", exclude_none=True)
        handle_dump = {
            key: value for key, value in handle_dump.items()
            if key in type(handle).FINGERPRINT_FIELDS
        }
        output_dump = (
            self.output_schema.model_dump(mode="json", exclude_none=True)
            if self.output_schema is not None else None
        )
        fields = {"type": self.type, "handle": handle_dump, "output_schema": output_dump}
        payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
        return compute_short_hash(payload)

    def llm_reply_schema(self) -> Optional[TableSchema]:
        """What the model's reply itself must carry: `output_schema` minus the
        input schema — the columns this stage ADDS, since an llm_transform
        passes its input columns through untouched and the runtime rejoins them
        itself. This is the single definition of that spec: the runtime compiles
        the reply model from it (app.runtime.stages.llm_transform) and the stage
        panel displays it, so neither can drift from the other.

        None for anything that is not a 1:1 llm_transform with both schemas
        declared. For one that is, `_llm_transform_one_to_one` has already
        guaranteed the difference is well defined, so `subtract` cannot throw."""
        if self.type != StageType.llm_transform or not self.inputs:
            return None
        input_schema = self.inputs[0].table_schema
        if self.output_schema is None or input_schema is None:
            return None
        return self.output_schema.subtract(input_schema)

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
    def _tests_shape(self) -> "Stage":
        validate_stage_tests(self.type, self.input_ids, self.tests or [])
        return self

    @model_validator(mode="after")
    def _handle_for_type(self) -> "Stage":
        spec = _TYPE_SPEC[self.type]
        handle = spec["handle"]
        if getattr(self, handle) is None:
            raise ValueError(f"type `{self.type}` requires a `{handle}:` block")
        for extra in spec.get("also_requires", ()):
            if getattr(self, extra) is None:
                raise ValueError(f"type `{self.type}` also requires a `{extra}:` block")
        if spec["requires_inputs"] and len(self.inputs) < spec["min_inputs"]:
            raise ValueError(
                f"type `{self.type}` needs >= {spec['min_inputs']} input(s), got {len(self.inputs)}"
            )
        max_inputs = spec.get("max_inputs")
        if max_inputs is not None and len(self.inputs) > max_inputs:
            raise ValueError(
                f"type `{self.type}` takes <= {max_inputs} input(s), got {len(self.inputs)} "
                f"(more than one input is a join, or use python_frame_function)"
            )
        return self

    @model_validator(mode="after")
    def _schemas_declared(self) -> "Stage":
        """Every schema this stage declares must be usable: non-empty, and naming no
        column in the reserved `_` namespace (find_internal_namespace_column_issues)."""
        issues = [
            f"input `{ref.id}` declares a schema with no columns"
            for ref in self.inputs
            if not ref.table_schema.columns
        ]
        if self.type != StageType.publish and not (
            self.output_schema and self.output_schema.columns
        ):
            issues.append("declares no output_schema")
        issues.extend(find_internal_namespace_column_issues(self))
        if issues:
            raise ValueError(f"type `{self.type}`: " + "; ".join(issues))
        return self

    @model_validator(mode="after")
    def _llm_transform_one_to_one(self) -> "Stage":
        """Enforced here — a stage carries its own contract — so the reply spec
        the runtime derives (`output_schema.subtract(input_schema)`) is exactly
        the added columns and can never throw mid-run. This is about schema
        SHAPE, not config columns, so it is not part of the
        `find_config_column_issues` dispatch. Cross-stage checks (unique ids,
        inputs resolve, acyclic) live in `workflow.graph_issues`; a single
        stage's invariants live on the stage."""
        if self.type != StageType.llm_transform:
            return self
        issues = find_llm_one_to_one_issues(self)
        if issues:
            raise ValueError("llm_transform not strictly 1:1: " + "; ".join(issues))
        return self

    @model_validator(mode="after")
    def _config_columns_resolve(self) -> "Stage":
        """Every column this stage's config directly names (a join key, an
        aggregate group_by/value_column, publish.one_file_per, an llm prompt
        {placeholder} — single-braced, since a double-braced one is never
        injected) or references via a where/filter predicate (aggregate
        `where`, human_review_queue `filter`) must resolve against that
        reference's own input edge — `inputs[index].table_schema`, per
        `app.models.stages.shared.resolve_input_columns`. EDGE-ONLY: this says
        nothing about what an upstream producer itself declares, so it holds
        for a single stage in isolation, independent of the rest of any
        workflow.

        Runs after `_handle_for_type`, so the type-matched handle block (join/
        aggregate/publish/llm/queue) this dispatches on is already guaranteed
        present. Lazy-imports the dispatch, rather than importing it at module
        level like every other import in this file: `app.models.stages`
        needs `Stage` back only for a type hint, but a module-level import
        here would run while this module (which defines `Stage`) is still
        mid-import."""
        from app.models.stages import find_config_column_issues

        issues = find_config_column_issues(self)
        if issues:
            raise ValueError("; ".join(issues))
        return self

    @model_validator(mode="after")
    def _output_schema_deliverable(self) -> "Stage":
        """A declared output_schema must be deliverable by this stage's own
        handle: for the types whose output is fixed by config (join, aggregate),
        every declared column must be producible by name, with the declared type
        matching the derivation where it can be known (see
        app.models.stages.find_output_schema_issues). EDGE-ONLY and per-stage,
        like _config_columns_resolve; same lazy import, same reason."""
        from app.models.stages import find_output_schema_issues

        issues = find_output_schema_issues(self)
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
          - filter_rows / union → NO — a filter drops rows, a union interleaves rows
                                 from several inputs, so neither is 1:1-by-position.
                                 Each output row's exact source (stage id + row
                                 ordinal) is still recorded, in
                                 app.runtime.lineage, for the trace to follow.
        """
        return is_grain_and_order_preserving(self.type)


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        Stage.model_validate(stage)
        return []
    except ValidationError as err:
        return format_errors(err)
