"""Stage-level contract: the node types, their executable-handle blocks, and the
Stage model. Constructing a model validates it.

Models ignore unknown keys (compiled stage JSON carries fields we pass through) but are
strict about the fields declared here.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from app.core.llm.options import LLMModel
from app.core.models.schema import (
    SourceRef,
    TableSchema,
    _Base,
    _SNAKE_RE,
    format_errors,
)
from app.core.models.stages.code import check_inline_function_code

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


# The stage types that guarantee output row i came from input row i — 1:1 and in
# the same order. This is owned by core (app.core.models) because it's a fact about the
# stage-type contract that several layers must read — the eval gate, the SYW
# positional tracer, the compiler — and only core is importable by all of them.
# Ask it through is_grain_and_order_preserving(); the runtime handler registry is
# held to conform (app/runtime/stages checks each type's shape against it at import).
_GRAIN_AND_ORDER_PRESERVING_TYPES: frozenset[StageType] = frozenset({
    StageType.input_data,
    StageType.python_row_function,
    StageType.llm_transform,
})


def is_grain_and_order_preserving(stage_type: StageType) -> bool:
    """Does one input row of this stage type map to exactly one output row, in the
    same order? Fixed entirely by stage type — see the
    Stage.is_grain_and_order_preserving property for the per-type contract."""
    return stage_type in _GRAIN_AND_ORDER_PRESERVING_TYPES


class ConnectorKind(str, Enum):
    file = "file"
    computed_static = "computed_static"


class FileFormat(str, Enum):
    csv = "csv"
    parquet = "parquet"
    json = "json"
    geojson = "geojson"


class AggFormula(str, Enum):
    sum = "sum"
    mean = "mean"
    count_ = "count"  # trailing underscore: `count` would shadow str.count
    min = "min"
    max = "max"
    first = "first"
    list = "list"


class JoinType(str, Enum):
    inner = "inner"
    left = "left"
    right = "right"
    outer = "outer"


class FunctionKind(str, Enum):
    inline = "inline"
    module = "module"


class PublishFormat(str, Enum):
    html_report = "html_report"
    json = "json"
    csv = "csv"
    evidence_cards = "evidence_cards"


# ── Executable-handle blocks (each self-validates) ───────────────────────────
# The input_data handle is a `kind`-discriminated union of connector subtypes.
# Each kind's parameters are real typed fields — not an untyped `dict[str, Any]`
# — so the schema is self-documenting and validated by shape (e.g. a `file`
# connector without a `path` fails on the missing required field, not in a
# hand-written validator). `kind` is the discriminator; adding a new connector
# kind means adding a subtype and a runtime handler.
class _ConnectorBase(_Base):
    """Fields common to every connector kind."""
    refresh: str = "ad_hoc"
    notes: Optional[str] = None


class FileConnector(_ConnectorBase):
    """input_data read from a repo-root-relative data file."""
    kind: Literal["file"] = "file"
    path: str = Field(description="repo-root-relative path to the data file")
    format: Optional[FileFormat] = Field(
        default=None, description="file format; defaults to csv when omitted"
    )
    list_columns: list[str] = Field(
        default_factory=list,
        description="columns whose cells are '[a, b]'-style lists to split on read",
    )
    parse_dates: list[str] = Field(
        default_factory=list, description="columns to coerce to datetime on read"
    )


class ComputedStaticConnector(_ConnectorBase):
    """input_data computed/seeded in place. Demo mode may seed the frame from an
    optional CSV named by `file`; otherwise the stage originates an empty frame."""
    kind: Literal["computed_static"] = "computed_static"
    file: Optional[str] = Field(
        default=None,
        description="optional repo-root-relative CSV to seed the frame (demo mode)",
    )


# The connector handle, as a pydantic discriminated union. Used directly as the
# `Stage.connector` annotation; `ConnectorAdapter` gives the same union a
# standalone `.validate_python(...)` for validating a connector dict on its own.
Connector = Annotated[
    Union[FileConnector, ComputedStaticConnector],
    Field(discriminator="kind"),
]

ConnectorAdapter: TypeAdapter[Union[FileConnector, ComputedStaticConnector]] = (
    TypeAdapter(Connector)
)


class LLMConfig(_Base):
    """llm_transform handle."""
    prompt_template: str
    model: Optional[LLMModel] = None
    temperature: float = 0.0
    max_retries: int = 3
    response_format: Literal["json", "text"] = "json"
    rubric: Optional[dict[str, Any]] = None
    tools: Optional[list[str]] = None


class PythonFunction(_Base):
    """Handle for python_row_function / python_frame_function (and publish). The
    row-vs-frame distinction lives in the stage `type`, not here — the runtime
    reads the type to decide whether to invoke this per row or per frame."""
    kind: FunctionKind
    code: Optional[str] = None
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
        check_inline_function_code(self.code, self.function)
        return self


class JoinKey(_Base):
    left: str
    right: str


class JoinConfig(_Base):
    """join handle. `keys` OR `on` is accepted."""
    type: JoinType = JoinType.inner
    keys: Optional[list[JoinKey]] = None
    on: Optional[list[JoinKey]] = None
    select: Optional[list[str]] = None

    @model_validator(mode="after")
    def _need_keys_or_on(self) -> "JoinConfig":
        if not (self.keys or self.on):
            raise ValueError("join needs `keys` or `on`")
        return self


class AggregationOp(_Base):
    output_column: str
    formula: AggFormula
    value_column: Optional[str] = None
    where: Optional[str] = None

    @model_validator(mode="after")
    def _value_column_for_formula(self) -> "AggregationOp":
        if self.formula != AggFormula.count_ and not self.value_column:
            raise ValueError(
                f"aggregation `{self.output_column}`: formula `{self.formula}` needs value_column"
            )
        return self


class AggregateConfig(_Base):
    """aggregate handle."""
    group_by: list[str]
    aggregations: list[AggregationOp]
    having: Optional[str] = None


class QueueConfig(_Base):
    """human_review_queue handle. `hash_columns` is optional; when absent the
    runner content-hashes on the upstream primary key."""
    filter: Optional[str] = None
    hash_columns: Optional[list[str]] = None
    reviewer_instructions: Optional[str] = None
    routing: Optional[str] = None
    conflict_resolution: Optional[str] = None
    estimated_volume_per_week: Optional[int] = None


class PublishConfig(_Base):
    """publish handle (runs alongside a `function` block)."""
    format: Optional[PublishFormat] = None
    destination: Optional[str] = None
    template: Optional[str] = None
    one_file_per: Optional[str] = None
    cross_link: Optional[bool] = None


class ReviewConfig(_Base):
    """Routes a stage's outputs into human review."""
    when: Optional[str] = None
    routing: Optional[str] = None
    rationale: Optional[str] = None
    queue_name: Optional[str] = None


class InputRef(_Base):
    """One upstream dependency: the upstream stage id, plus (optionally) the
    schema this stage expects that upstream's output to satisfy. The runner
    validates the real dataframe against `table_schema` before the stage runs.
    The compiled stage spells the field `schema:`; the python name differs only
    because pydantic reserves `schema` on BaseModel."""
    id: str
    table_schema: Optional[TableSchema] = Field(default=None, alias="schema")


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
}


class Stage(_Base):
    """One node in the workflow. Exactly one handle block is required,
    selected by `type`."""
    id: str
    type: StageType
    name: str
    source: Optional[SourceRef] = None
    inputs: list[InputRef] = Field(default_factory=list)
    output_schema: Optional[TableSchema] = None

    # executable handles (exactly one populated, per type)
    connector: Optional[Connector] = None
    llm: Optional[LLMConfig] = None
    function: Optional[PythonFunction] = None
    join: Optional[JoinConfig] = None
    aggregate: Optional[AggregateConfig] = None
    queue: Optional[QueueConfig] = None
    publish: Optional[PublishConfig] = None

    review: Optional[ReviewConfig] = None
    limit: Optional[int] = None
    compiler_notes: list[str] = Field(default_factory=list)

    # Descriptive eval note rendered on the stage page (reference data, metrics).
    # Display only — the executable eval contract is EvalConfig (app/core/models/eval.py).
    eval: Optional[dict[str, Any]] = None

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

    @field_validator("id")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"id {v!r} should be snake_case")
        return v

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
    def _llm_transform_one_to_one(self) -> "Stage":
        """An llm_transform maps one input row to one output row, so on its
        DECLARED schemas alone it must: take exactly one input; declare a
        primary_key on both that input's schema and its output_schema, naming
        the same columns; keep every input column unchanged (a transform never
        rewrites an existing column's schema); and add at least one new column.

        Enforced here — a stage carries its own contract — so the reply spec the
        runtime derives (`output_schema.subtract(input_schema)`) is exactly the
        added columns and can never throw mid-run. Cross-stage checks (unique
        ids, inputs resolve, acyclic) live in `workflow.graph_issues`; a single
        stage's invariants live on the stage."""
        if self.type != StageType.llm_transform:
            return self

        if len(self.inputs) != 1:
            raise ValueError(
                f"llm_transform must have exactly one input, has {len(self.inputs)}"
            )
        input_schema = self.inputs[0].table_schema
        output_schema = self.output_schema
        if input_schema is None or output_schema is None:
            missing = "input schema" if input_schema is None else "output_schema"
            raise ValueError(
                f"llm_transform declares no {missing}; a 1:1 stage needs a "
                "primary_key on both its input and output schemas"
            )

        issues: list[str] = []
        input_pk, output_pk = input_schema.primary_key, output_schema.primary_key
        if not input_pk:
            issues.append("input schema declares no primary_key")
        if not output_pk:
            issues.append("output_schema declares no primary_key")
        if input_pk and output_pk and set(input_pk) != set(output_pk):
            issues.append(
                f"input primary_key {input_pk} != output primary_key {output_pk}"
            )

        if not input_schema.is_subset_of(output_schema):
            issues.append(
                "output must keep every input column unchanged (a transform is "
                f"additive: output ⊇ input); input columns "
                f"{[c.name for c in input_schema.columns]} vs output columns "
                f"{[c.name for c in output_schema.columns]}"
            )

        input_names = {c.name for c in input_schema.columns}
        if not any(c.name not in input_names for c in output_schema.columns):
            issues.append("output_schema adds no columns beyond the input")

        if issues:
            raise ValueError("llm_transform not strictly 1:1: " + "; ".join(issues))
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
          - human_review_queue → NO — handle_human_review_queue drops rejected rows
                                 and concatenates decided+passthrough, changing both
                                 grain and order. Its intended "edits in place"
                                 contract would make it yes; closing that gap is #106.
          - join (fan-out) / aggregate (fan-in) → NO; grain changes are deferred
          - publish            → NO — handle_publish runs an authored function whose
                                 output is a table of artifact paths, not the input
                                 rows (and it is terminal — nothing downstream).
        """
        return is_grain_and_order_preserving(self.type)


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        Stage.model_validate(stage)
        return []
    except ValidationError as err:
        return format_errors(err)
