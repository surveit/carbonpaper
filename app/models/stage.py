"""Stage-level contract: the node types, their executable-handle blocks, and the
Stage model. Constructing a model validates it.

Models ignore unknown keys (compiled YAML carries fields we pass through) but are
strict about the fields declared here.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.llm.options import LLMModel

# ── Column-type vocabulary ───────────────────────────────────────────────────
SCALAR_COLUMN_TYPES: set[str] = {
    "str", "int", "float", "bool", "datetime", "date", "dict", "json",
}
_LIST_RE = re.compile(r"^list\[(.+)\]$")


def is_valid_column_type(t: str) -> bool:
    """Scalar, or list[<scalar>] / nested list[list[...]]."""
    if not isinstance(t, str):
        return False
    if t in SCALAR_COLUMN_TYPES:
        return True
    m = _LIST_RE.match(t)
    if m:
        inner = m.group(1).strip()
        return inner in SCALAR_COLUMN_TYPES or bool(_LIST_RE.match(inner))
    return False


# ── Enumerated vocabularies ──────────────────────────────────────────────────
class StageType(str, Enum):
    input_data = "input_data"
    llm_transform = "llm_transform"
    python_transform = "python_transform"
    join = "join"
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
    count = "count"
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


# ── Base ─────────────────────────────────────────────────────────────────────
class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ── Provenance ───────────────────────────────────────────────────────────────
class SourceRef(_Base):
    """Where a stage's prose justification lives."""
    doc: Optional[str] = None
    section: Optional[str] = None
    lines: Optional[list[int]] = None


# ── Typed columns / schemas ──────────────────────────────────────────────────
class Column(_Base):
    name: str
    type: str = "str"
    nullable: bool = True
    description: Optional[str] = None
    range: Optional[list[Any]] = None
    source: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if not is_valid_column_type(v):
            raise ValueError(f"unknown column type {v!r}")
        return v


class TableSchema(_Base):
    columns: list[Column]
    estimated_rows: Optional[int] = None
    primary_key: Optional[list[str]] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "TableSchema":
        seen: set[str] = set()
        for c in self.columns:
            if c.name in seen:
                raise ValueError(f"duplicate column {c.name!r}")
            seen.add(c.name)
        for k in self.primary_key or []:
            if k not in seen:
                raise ValueError(f"primary_key {k!r} is not a declared column")
        return self


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
    StageType.join:               {"handle": "join",      "requires_inputs": True,  "min_inputs": 2},
    StageType.aggregate:          {"handle": "aggregate", "requires_inputs": True,  "min_inputs": 1},
    StageType.human_review_queue: {"handle": "queue",     "requires_inputs": True,  "min_inputs": 1},
    StageType.publish:            {"handle": "publish",   "also_requires": ["function"], "requires_inputs": True, "min_inputs": 1},
}

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


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


def format_errors(err: ValidationError) -> list[str]:
    """Pydantic errors → human-readable issue strings."""
    out: list[str] = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "stages")
        msg = e.get("msg", "invalid")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        Stage.model_validate(stage)
        return []
    except ValidationError as err:
        return format_errors(err)
