"""Stage-level contract: the node types, their executable-handle blocks, and the
Stage model. Constructing a model validates it.

Models ignore unknown keys (compiled YAML carries fields we pass through) but are
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
    python_transform = "python_transform"
    # Trailing underscore: a member literally named `join` would shadow
    # str.join on every instance. The value — what YAML declares and
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
    weighted_mean = "weighted_mean"
    weighted_sum = "weighted_sum"


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
    def _file_format(self) -> "Connector":
        if self.kind is ConnectorKind.file:
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
    """python_transform (and publish) handle."""
    kind: FunctionKind
    code: Optional[str] = None
    module: Optional[str] = None
    function: Optional[str] = None
    requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_fields(self) -> "PythonFunction":
        if self.kind is FunctionKind.module and not self.module:
            raise ValueError("function.kind=module needs `module`")
        if self.kind is FunctionKind.inline and not self.code:
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
    weight_column: Optional[str] = None
    where: Optional[str] = None

    @model_validator(mode="after")
    def _weighted_needs_value_and_weight(self) -> "AggregationOp":
        if self.formula in (AggFormula.weighted_mean, AggFormula.weighted_sum) and not (
            self.value_column and self.weight_column
        ):
            raise ValueError(
                f"{self.formula.value} needs both value_column and weight_column"
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


# ── Stage ────────────────────────────────────────────────────────────────────
# type → which handle block it must carry, plus input arity.
_TYPE_SPEC: dict[StageType, dict[str, Any]] = {
    StageType.input_data:         {"handle": "connector", "requires_inputs": False, "min_inputs": 0},
    StageType.llm_transform:      {"handle": "llm",       "requires_inputs": True,  "min_inputs": 1},
    StageType.python_transform:   {"handle": "function",  "requires_inputs": True,  "min_inputs": 1},
    StageType.join_:              {"handle": "join",      "requires_inputs": True,  "min_inputs": 2},
    StageType.aggregate:          {"handle": "aggregate", "requires_inputs": True,  "min_inputs": 1},
    StageType.human_review_queue: {"handle": "queue",     "requires_inputs": True,  "min_inputs": 1},
    StageType.publish:            {"handle": "publish",   "also_requires": ["function"], "requires_inputs": True, "min_inputs": 1},
}


class Stage(_Base):
    """One node in the methodology DAG. Exactly one handle block is required,
    selected by `type`."""
    id: str
    type: StageType
    name: str
    source: Optional[SourceRef] = None
    inputs: list[str] = Field(default_factory=list)
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

    @field_validator("inputs", mode="before")
    @classmethod
    def _ids_only(cls, v: Any) -> Any:
        """Accept inputs as [{id, schema}] or [id]; keep only the upstream id."""
        if not isinstance(v, list):
            return v
        return [item.get("id") if isinstance(item, dict) else item for item in v]

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
            raise ValueError(f"type `{self.type.value}` requires a `{handle}:` block")
        for extra in spec.get("also_requires", ()):
            if getattr(self, extra) is None:
                raise ValueError(f"type `{self.type.value}` also requires a `{extra}:` block")
        if spec["requires_inputs"] and len(self.inputs) < spec["min_inputs"]:
            raise ValueError(
                f"type `{self.type.value}` needs >= {spec['min_inputs']} input(s), got {len(self.inputs)}"
            )
        return self


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        Stage.model_validate(stage)
        return []
    except ValidationError as err:
        return format_errors(err)
