"""
models.py — the methodology DAG contract, as Pydantic models.

This is the single source of truth for what a valid stage and a valid DAG look
like. Constructing a model validates it (Pydantic raises on a violation), so the
contract is the model — there is no separate validator to keep in sync.

Replaces two older things, now removed:
  - app/schema.py     (dataclass *spec*, imported by nothing)
  - app/dag_schema.py (hand-rolled validators returning issue lists)

The runtime and the compiler meet here: import these to parse/validate a DAG.
Models are lenient about *extra* keys (real compiled YAML carries provenance and
notes we don't validate) but strict about the core contract.

Cut per review (2026-06-29):
  - connector kinds reduced to the implemented ones (file, computed_static); the
    rest were declared but never had a handler. Add back when a handler exists.
  - weighted aggregation formulas (weighted_mean/weighted_sum) — unused in the
    compiled DAGs (weighting is done inside python_transform modules).
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
    # http / scrape / api / manual_upload / sql were declared but never
    # implemented — cut. Commit a snapshot and use `file`, or add a handler.


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
    # weighted_mean / weighted_sum cut — unused (weighting lives in python_transform).


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
    # Lenient about extra keys (compiled YAML carries source/eval/notes we pass
    # through), strict about the fields we declare.
    model_config = ConfigDict(extra="ignore")


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


# ── Executable-handle blocks ─────────────────────────────────────────────────
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
    model: Optional[str] = None
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
    """join handle. `keys` OR `on` is accepted (handle_join reads either)."""
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


# ── Stage ────────────────────────────────────────────────────────────────────
# type → which handle block it must carry, plus input arity.
_TYPE_SPEC: dict[StageType, dict[str, Any]] = {
    StageType.input_data:        {"handle": "connector", "requires_inputs": False, "min_inputs": 0},
    StageType.llm_transform:     {"handle": "llm",       "requires_inputs": True,  "min_inputs": 1},
    StageType.python_transform:  {"handle": "function",  "requires_inputs": True,  "min_inputs": 1},
    StageType.join:              {"handle": "join",      "requires_inputs": True,  "min_inputs": 2},
    StageType.aggregate:         {"handle": "aggregate", "requires_inputs": True,  "min_inputs": 1},
    StageType.human_review_queue:{"handle": "queue",     "requires_inputs": True,  "min_inputs": 1},
    StageType.publish:           {"handle": "publish",   "also_requires": ["function"], "requires_inputs": True, "min_inputs": 1},
}

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class Stage(_Base):
    """One node in the methodology DAG. Exactly one handle block is required,
    selected by `type` (see _TYPE_SPEC)."""
    id: str
    type: StageType
    name: Optional[str] = None
    source: Optional[Any] = None          # provenance — not validated here
    inputs: list[Any] = Field(default_factory=list)   # [{id, schema?}] or [id]
    output_schema: Optional[TableSchema] = None

    # executable handles (exactly one populated, per type)
    connector: Optional[Connector] = None
    llm: Optional[LLMConfig] = None
    function: Optional[PythonFunction] = None
    join: Optional[JoinConfig] = None
    aggregate: Optional[AggregateConfig] = None
    queue: Optional[dict[str, Any]] = None
    publish: Optional[dict[str, Any]] = None

    # cross-cutting (loose)
    eval: Optional[dict[str, Any]] = None
    review: Optional[dict[str, Any]] = None
    limit: Optional[int] = None
    compiler_notes: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"id {v!r} should be snake_case")
        return v

    @model_validator(mode="after")
    def _type_contract(self) -> "Stage":
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
        if self.type is StageType.human_review_queue:
            q = self.queue or {}
            if not q.get("hash_columns"):
                first = self.inputs[0] if self.inputs else None
                sch = first.get("schema") if isinstance(first, dict) else None
                pk = sch.get("primary_key") if isinstance(sch, dict) else None
                if not pk:
                    raise ValueError("queue needs `hash_columns` or an upstream primary_key")
        return self


def _input_id(inp: Any) -> Optional[str]:
    return inp.get("id") if isinstance(inp, dict) else (inp if isinstance(inp, str) else None)


class Methodology(_Base):
    """A whole DAG: validated stages + cross-stage checks (unique ids, inputs
    resolve, acyclic)."""
    stages: list[Stage]

    @model_validator(mode="after")
    def _dag(self) -> "Methodology":
        ids = [s.id for s in self.stages]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate stage id(s): {dupes}")
        id_set = set(ids)

        edges: dict[str, list[str]] = {}
        for s in self.stages:
            deps: list[str] = []
            for inp in s.inputs:
                iid = _input_id(inp)
                if iid and iid not in id_set:
                    raise ValueError(f"`{s.id}`: input `{iid}` references no stage")
                if iid:
                    deps.append(iid)
            edges[s.id] = deps

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {sid: WHITE for sid in edges}

        def visit(n: str, path: list[str]) -> None:
            color[n] = GRAY
            for nxt in edges.get(n, []):
                if color.get(nxt) == GRAY:
                    raise ValueError(f"cycle detected: {' -> '.join(path + [n, nxt])}")
                if color.get(nxt) == WHITE:
                    visit(nxt, path + [n])
            color[n] = BLACK

        for sid in edges:
            if color[sid] == WHITE:
                visit(sid, [])
        return self


# ── Convenience: parse / non-fatal validate ──────────────────────────────────
def parse_methodology(stages: list[dict[str, Any]]) -> Methodology:
    """Parse + validate a list of stage dicts. Raises ValidationError if invalid."""
    return Methodology(stages=list(stages))


def _format_errors(err: ValidationError) -> list[str]:
    out: list[str] = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "stages")
        msg = e.get("msg", "invalid")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


def validate_methodology(stages: list[dict[str, Any]]) -> list[str]:
    """Non-fatal: return a list of human-readable issues ([] means valid).
    For the UI/compiler, which want to *show* problems rather than crash."""
    try:
        Methodology(stages=list(stages))
        return []
    except ValidationError as err:
        return _format_errors(err)


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict."""
    try:
        Stage.model_validate(stage)
        return []
    except ValidationError as err:
        return _format_errors(err)


__all__ = [
    "StageType", "ConnectorKind", "FileFormat", "AggFormula", "JoinType",
    "FunctionKind", "PublishFormat", "is_valid_column_type",
    "Column", "TableSchema", "Connector", "LLMConfig", "PythonFunction",
    "JoinKey", "JoinConfig", "AggregationOp", "AggregateConfig",
    "Stage", "Methodology",
    "parse_methodology", "validate_methodology", "validate_stage",
]
