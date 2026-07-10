"""Stage-level contract: the node types, their executable-handle blocks, and the
per-type Stage models combined into a discriminated union.

A stage is one of eight per-type models — `InputDataStage`, `LlmTransformStage`,
`PythonRowFunctionStage`, `PythonFrameFunctionStage`, `JoinStage`,
`AggregateStage`, `HumanReviewQueueStage`, `PublishStage` — combined via
`Stage = Annotated[Union[...], Field(discriminator="type")]`. Discriminating on
`type` means each model carries exactly its own handle block (so the old
"exactly one handle field per type" table is structural, not a validator) and
declares an `output_schema` only where it produces a table. Every
table-producing type REQUIRES `output_schema`; `publish` (which writes artifacts,
not a table) has no such field at all. See issue #51.

Constructing a model validates it. Use `parse_stage(data)` to validate a raw
dict into the right per-type model; `validate_stage(data)` is the non-fatal
variant returning issue strings.

Models ignore nothing — unknown keys are rejected (a typo'd field is an invalid
stage, not silently-ignored data)."""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from app.llm.options import LLMModel
from app.models.schema import (
    SourceRef,
    TableSchema,
    _Base,
    _SNAKE_RE,
    format_errors,
)

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
class Connector(_Base):
    """input_data handle."""
    kind: ConnectorKind
    params: dict[str, Any] = Field(default_factory=dict)
    refresh: str = "ad_hoc"
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _params_for_kind(self) -> "Connector":
        if self.kind == ConnectorKind.file:
            path = (self.params or {}).get("path")
            if not path or not isinstance(path, str):
                raise ValueError(
                    "connector kind=file requires params.path (repo-root-relative data file)"
                )
            fmt = (self.params or {}).get("format")
            if fmt is not None and fmt not in {f.value for f in FileFormat}:
                raise ValueError(f"unknown file format {fmt!r}")
        return self


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


# ── Stage: a discriminated union of per-type models ──────────────────────────
# The old single `Stage` model carried every handle field as Optional plus a
# `_TYPE_SPEC` table + validator to enforce "exactly the right handle for this
# type" and "output_schema present". Splitting into per-type models makes both
# structural: each model declares exactly its own handle block, and only
# table-producing types declare `output_schema` (required). `publish` writes
# artifacts, not a table, so it has no `output_schema` field at all. Parse errors
# now name the actually-missing field per type. See issue #51.
class StageBase(_Base):
    """Fields and behaviour common to every stage type. Never instantiated
    directly — a stage is always one of the concrete per-type subclasses,
    combined into the `Stage` discriminated union. The `type` discriminator and
    the handle block (`connector`/`llm`/`function`/…) live on the subclasses;
    `output_schema` lives on the table-producing ones."""

    # Per-type input arity, read by the shared `_check_input_arity` validator.
    # Subclasses override; the base defaults to "no inputs required, no cap".
    MIN_INPUTS: ClassVar[int] = 0
    MAX_INPUTS: ClassVar[Optional[int]] = None

    id: str
    # Narrowed to a `Literal[...]` on each subclass — that Literal is the union
    # discriminator. Declared here (as the widened `str`) so shared helpers like
    # `is_grain_preserving` can read `self.type`.
    type: str
    name: str
    source: Optional[SourceRef] = None
    inputs: list[InputRef] = Field(default_factory=list)

    review: Optional[ReviewConfig] = None
    limit: Optional[int] = None
    compiler_notes: list[str] = Field(default_factory=list)

    # Descriptive eval note rendered on the stage page (reference data, metrics).
    # Display only — the executable eval contract is EvalConfig (app/models/eval.py).
    eval: Optional[dict[str, Any]] = None

    @field_validator("inputs", mode="before")
    @classmethod
    def _bare_id_shorthand(cls, v: Any) -> Any:
        """Accept `inputs: [upstream_id]` shorthand for `[{id: upstream_id}]`."""
        if not isinstance(v, list):
            return v
        return [{"id": item} if isinstance(item, str) else item for item in v]

    @field_validator("id")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"id {v!r} should be snake_case")
        return v

    @model_validator(mode="after")
    def _check_input_arity(self) -> "StageBase":
        n = len(self.inputs)
        if n < self.MIN_INPUTS:
            raise ValueError(
                f"type `{self.type}` needs >= {self.MIN_INPUTS} input(s), got {n}"
            )
        if self.MAX_INPUTS is not None and n > self.MAX_INPUTS:
            raise ValueError(
                f"type `{self.type}` takes <= {self.MAX_INPUTS} input(s), got {n} "
                f"(more than one input is a join, or use python_frame_function)"
            )
        return self

    @property
    def input_ids(self) -> list[str]:
        return [ref.id for ref in self.inputs]

    @property
    def is_grain_preserving(self) -> bool:
        """Does one input row map to exactly one output row? This is the v1 eval
        gate: a declarative (single-table, row-aligned) eval can only tap a node
        reached through grain-preserving stages. Fixed entirely by stage type:
          - python_row_function → yes (runtime maps it per row — enforced 1:1)
          - python_frame_function → NO (may reshape the frame)
          - llm_transform      → yes (per-row 1:1 in v1; a fan-out LLM like
                                 doc→pieces is out of scope until fan-out evals)
          - input_data         → yes (originates the rows)
          - human_review_queue → yes (keyed, edits in place)
          - join (fan-out) / aggregate (fan-in) → NO; grain changes are deferred
          - publish            → terminal, never a tap target
        """
        return self.type in (
            StageType.python_row_function,
            StageType.llm_transform,
            StageType.input_data,
            StageType.human_review_queue,
            StageType.publish,
        )


class InputDataStage(StageBase):
    """Declares a source dataset with a typed schema."""
    type: Literal["input_data"] = "input_data"
    connector: Connector
    output_schema: TableSchema


class LlmTransformStage(StageBase):
    """Row-by-row LLM call producing structured output. Strictly 1:1."""
    MIN_INPUTS: ClassVar[int] = 1

    type: Literal["llm_transform"] = "llm_transform"
    llm: LLMConfig
    output_schema: TableSchema

    @model_validator(mode="after")
    def _llm_transform_one_to_one(self) -> "LlmTransformStage":
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
        if len(self.inputs) != 1:
            raise ValueError(
                f"llm_transform must have exactly one input, has {len(self.inputs)}"
            )
        input_schema = self.inputs[0].table_schema
        output_schema = self.output_schema
        if input_schema is None:
            raise ValueError(
                "llm_transform declares no input schema; a 1:1 stage needs a "
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


class PythonRowFunctionStage(StageBase):
    """Per-row Python transform (runtime maps it row-by-row: enforced 1:1)."""
    MIN_INPUTS: ClassVar[int] = 1
    MAX_INPUTS: ClassVar[Optional[int]] = 1

    type: Literal["python_row_function"] = "python_row_function"
    function: PythonFunction
    output_schema: TableSchema


class PythonFrameFunctionStage(StageBase):
    """Whole-frame Python transform (may reshape the frame(s))."""
    MIN_INPUTS: ClassVar[int] = 1

    type: Literal["python_frame_function"] = "python_frame_function"
    function: PythonFunction
    output_schema: TableSchema


class JoinStage(StageBase):
    """Combine two or more upstream dataframes on keys."""
    MIN_INPUTS: ClassVar[int] = 2

    type: Literal["join"] = "join"
    join: JoinConfig
    output_schema: TableSchema


class AggregateStage(StageBase):
    """Structured group-by aggregation."""
    MIN_INPUTS: ClassVar[int] = 1

    type: Literal["aggregate"] = "aggregate"
    aggregate: AggregateConfig
    output_schema: TableSchema


class HumanReviewQueueStage(StageBase):
    """Pulls flagged rows for human decision; halts the run."""
    MIN_INPUTS: ClassVar[int] = 1

    type: Literal["human_review_queue"] = "human_review_queue"
    queue: QueueConfig
    output_schema: TableSchema


class PublishStage(StageBase):
    """Render a final artifact (html, json, csv, cards). Writes artifacts, not a
    table — so, unlike every other type, it declares NO `output_schema`."""
    MIN_INPUTS: ClassVar[int] = 1

    type: Literal["publish"] = "publish"
    publish: PublishConfig
    function: PythonFunction


# Order is for readability only; discrimination is by the `type` literal.
AnyStage = Union[
    InputDataStage,
    LlmTransformStage,
    PythonRowFunctionStage,
    PythonFrameFunctionStage,
    JoinStage,
    AggregateStage,
    HumanReviewQueueStage,
    PublishStage,
]

# THE public "a stage is one of these" type. Use it in annotations (`stage: Stage`,
# `list[Stage]`) and as a pydantic field type — it discriminates on `type`. For
# an isinstance check use `StageBase` (the common base every member subclasses);
# to validate a raw dict use `parse_stage` (`Stage` is an alias, not a class, so
# it has no `.model_validate`).
Stage = Annotated[AnyStage, Field(discriminator="type")]

_STAGE_ADAPTER: TypeAdapter[AnyStage] = TypeAdapter(Stage)


def parse_stage(data: Any) -> AnyStage:
    """Validate a raw stage dict into its per-type model, discriminating on
    `type`. Raises ValidationError if invalid."""
    return _STAGE_ADAPTER.validate_python(data)


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        parse_stage(stage)
        return []
    except ValidationError as err:
        return format_errors(err)
