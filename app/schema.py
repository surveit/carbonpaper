"""
DAG schema v2 — executable node types.

A compiled methodology is a directed acyclic graph of stages. Every stage
declares typed input schemas and a typed output schema, and carries an
executable handle: a connector spec, a prompt template, a pandas function,
join keys, or aggregation rules. The runtime can actually execute these.

Stage types (small, deliberate set):

  input_data          — declares a source dataset with a typed output schema.
                        Connector spec says how to fetch (file/http/scrape/api/manual).
  llm_transform       — row-by-row LLM call with prompt template + structured output.
                        Typed input schema, typed output schema, eval and review configs.
  python_transform    — arbitrary Python over one or more upstream dataframes.
                        Function may be inline or module:fn ref. Typed inputs/outputs.
  join                — combine two or more upstream dataframes on keys.
  aggregate           — structured group-by aggregation. Sugar over python_transform.
  human_review_queue  — human verification queue. Pulls flagged rows from upstream.
  publish             — render final artifact (table, json, html, evidence cards).

Anything outside these types must use python_transform with a compiler_note
explaining why it doesn't fit one of the structured types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# ─── Stage taxonomy ──────────────────────────────────────────────────────────

StageType = Literal[
    "input_data",
    "llm_transform",
    "python_transform",
    "join",
    "aggregate",
    "human_review_queue",
    "publish",
]


# ─── Provenance ──────────────────────────────────────────────────────────────

@dataclass
class SourceRef:
    """Pointer back to the prose passage that justified this stage."""
    doc: str
    section: Optional[str] = None
    lines: Optional[list[int]] = None


# ─── Typed columns / schemas ─────────────────────────────────────────────────
#
# Every stage declares its output_schema as a list of columns. Downstream
# stages reference those columns by name. This is informal but enforced at
# parse time (see app/main.py); a stage that consumes a column the upstream
# doesn't declare is a compile error.

@dataclass
class Column:
    name: str
    type: str                          # "str" | "int" | "float" | "bool" | "datetime" | "list[str]" | "dict" | "json"
    nullable: bool = True
    description: Optional[str] = None
    range: Optional[list[Any]] = None  # for numeric: [low, high]; for enum: [allowed_values...]
    source: Optional[str] = None       # "passthrough" | "computed" | etc.


@dataclass
class TableSchema:
    columns: list[Column]
    estimated_rows: Optional[int] = None
    primary_key: Optional[list[str]] = None
    notes: Optional[str] = None


# ─── Connector (for input_data) ──────────────────────────────────────────────

@dataclass
class Connector:
    """How an input_data stage fetches its data."""
    kind: Literal["file", "http", "scrape", "api", "manual_upload", "sql", "computed_static"]
    # kind-specific fields go in `params`. Examples:
    #   file:           {path: "...", format: "csv|json|parquet"}
    #   http:           {url: "...", method: "GET", headers: {}}
    #   scrape:         {url: "...", parser: "html_table" | "custom"}
    #   api:            {provider: "...", endpoint: "..."}
    #   manual_upload:  {accept: "csv|json", description: "..."}
    #   sql:            {connection: "...", query: "..."}
    #   computed_static: {description: "user-curated list, no automated fetch"}
    params: dict[str, Any] = field(default_factory=dict)
    refresh: str = "ad_hoc"            # "yearly" | "monthly" | "weekly" | "daily" | "ad_hoc" | "continuous"
    notes: Optional[str] = None


# ─── LLM call config ─────────────────────────────────────────────────────────

@dataclass
class LLMConfig:
    """Configuration for the LLM invocation in llm_transform stages."""
    model: str                          # e.g. "claude-sonnet-4-6", "gpt-4o-mini"
    prompt_template: str                # placeholders match input_schema column names: {col_name}
    temperature: float = 0.0
    max_retries: int = 3
    response_format: Literal["json", "text"] = "json"
    # optional rubric/benchmark structures the prompt references
    rubric: Optional[dict[str, Any]] = None
    benchmarks: Optional[dict[str, Any]] = None


# ─── Python function reference ───────────────────────────────────────────────

@dataclass
class PythonFunction:
    """The executable handle for a python_transform stage."""
    kind: Literal["inline", "module"]
    # If kind == "inline": `code` contains the Python source. Must define
    # a function `transform` that takes positional dataframe arguments
    # in the same order as `inputs:` and returns one dataframe.
    # If kind == "module": `module` and `function` reference an importable callable.
    code: Optional[str] = None
    module: Optional[str] = None
    function: Optional[str] = None
    requirements: list[str] = field(default_factory=list)


# ─── Join config ─────────────────────────────────────────────────────────────

@dataclass
class JoinKey:
    left: str          # column name in the left input
    right: str         # column name in the right input


@dataclass
class JoinConfig:
    type: Literal["inner", "left", "right", "outer"]
    on: list[JoinKey]
    select: Optional[list[str]] = None  # explicit column projection if needed


# ─── Aggregation config ──────────────────────────────────────────────────────

@dataclass
class AggregationOp:
    output_column: str
    formula: Literal["sum", "mean", "weighted_mean", "weighted_sum",
                     "count", "min", "max", "first", "list"]
    value_column: Optional[str] = None
    weight_column: Optional[str] = None    # for weighted_*
    where: Optional[str] = None            # optional filter expression


@dataclass
class AggregateConfig:
    group_by: list[str]
    aggregations: list[AggregationOp]
    having: Optional[str] = None


# ─── Eval and review ─────────────────────────────────────────────────────────

@dataclass
class EvalConfig:
    """How an LLM stage's output is evaluated against ground truth."""
    reference: str                      # file path to ground-truth dataframe
    reference_schema: TableSchema       # expected columns in the reference
    join_on: list[str]                  # how to join eval output to reference
    metrics: list[str]                  # metric names the runtime computes
    notes: Optional[str] = None


@dataclass
class ReviewConfig:
    """Routing for human verification of a stage's outputs."""
    when: str                           # SQL-like predicate over output columns
    routing: str                        # "1-of-1" | "2-of-2" | "1-of-2-disagreement-escalates"
    rationale: Optional[str] = None
    queue_name: Optional[str] = None    # references a human_review_queue stage if not inline


# ─── The Stage ───────────────────────────────────────────────────────────────

@dataclass
class Stage:
    """One node in the methodology DAG."""

    # Identity
    id: str
    name: str
    type: StageType

    # Provenance
    source: SourceRef

    # Topology — every non-input stage declares typed inputs
    # `inputs` is a list of objects, each referencing an upstream stage id
    # plus the schema we expect (so compile-time validation can flag drift).
    inputs: list[dict[str, Any]] = field(default_factory=list)
    # Each input dict: {id: "stage_id", schema: TableSchema}

    # Output schema — every stage declares what it produces
    output_schema: Optional[TableSchema] = None

    # Executable handle — exactly one of these is populated, depending on type
    connector: Optional[Connector] = None              # input_data
    llm: Optional[LLMConfig] = None                    # llm_transform
    function: Optional[PythonFunction] = None          # python_transform
    join: Optional[JoinConfig] = None                  # join
    aggregate: Optional[AggregateConfig] = None        # aggregate
    queue: Optional[dict[str, Any]] = None             # human_review_queue
    publish: Optional[dict[str, Any]] = None           # publish

    # Eval and review (mostly llm_transform, but any stage may have review)
    eval: Optional[EvalConfig] = None
    review: Optional[ReviewConfig] = None

    # Compilation honesty
    compiler_notes: list[str] = field(default_factory=list)
