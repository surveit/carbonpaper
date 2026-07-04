"""Stage-level contract: the node types, their executable-handle blocks, and the
Stage model. Constructing a model validates it.

Models ignore unknown keys (compiled stage JSON carries fields we pass through) but are
strict about the fields declared here.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import Field, ValidationError, field_validator, model_validator

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
    # Display only — the executable eval contract is EvalConfig (app/models/eval.py).
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


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        Stage.model_validate(stage)
        return []
    except ValidationError as err:
        return format_errors(err)
