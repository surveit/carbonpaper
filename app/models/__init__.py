"""The methodology DAG contract, as Pydantic models.

Split across two modules:
  - stage.py       — node types, handle blocks, the Stage model
  - methodology.py — the Methodology (DAG) model + cross-stage graph checks

Import from `app.models` (this aggregator) for the stable public surface.
"""
from app.models.stage import (
    AggFormula,
    AggregateConfig,
    AggregationOp,
    Column,
    Connector,
    ConnectorKind,
    FileFormat,
    FunctionKind,
    JoinConfig,
    JoinKey,
    JoinType,
    LLMConfig,
    PublishConfig,
    PublishFormat,
    PythonFunction,
    QueueConfig,
    ReviewConfig,
    SourceRef,
    Stage,
    StageType,
    TableSchema,
    is_valid_column_type,
    validate_stage,
)
from app.models.methodology import (
    Methodology,
    check_inputs_resolve,
    check_unique_ids,
    detect_cycle,
    parse_methodology,
    validate_methodology,
)
from app.models.schema import (
    NamedColumn,
    NamedSchema,
    SchemaKind,
    SchemaLibrary,
    check_references_resolve,
    check_unique_schema_names,
    parse_reference,
    parse_schema_library,
    validate_named_schema,
    validate_schema_library,
)
from app.models.eval import (
    EvalSpec,
    build_ground_truth_schema,
    validate_eval_spec,
)

# ── Compat constants (the plain-data surface the old contract module exposed) ──
# The Pydantic models above are the contract; these string/dict vocabularies are
# what the compiler and prompt render (to the LLM, to fenced-block contracts). They
# are DERIVED from the enums/models where possible so they can't drift, and the
# NODE_TYPES spec is preserved verbatim so prompt._node_type_contract() renders
# identically to the previous plain-data contract output.
from typing import Any as _Any

# Scalar column-type vocabulary (list[<type>] / dict / json handled by
# is_valid_column_type). Re-exported from stage.py, the single definition.
from app.models.stage import SCALAR_COLUMN_TYPES

# Kind/type vocabularies as string sets, derived from the enums.
SCHEMA_KINDS: set[str] = {k.value for k in SchemaKind}
JOIN_TYPES: set[str] = {j.value for j in JoinType}
# Full 7-kind connector vocabulary the prompt advertises to the LLM (the compiler
# may emit any of these). ConnectorKind was widened back to the full set.
CONNECTOR_KINDS: set[str] = {k.value for k in ConnectorKind}
# What the demo runtime can actually execute today; the rest raise
# NotImplementedError (handlers.handle_input_data).
IMPLEMENTED_CONNECTOR_KINDS: set[str] = {"file", "computed_static"}
# Weighted formulas need value_column + weight_column (enforced on AggregationOp).
WEIGHTED_FORMULAS: set[str] = {"weighted_mean", "weighted_sum"}

# ── The seven node types and their contract (preserved verbatim) ─────────────
# prompt._node_type_contract() iterates this to render the contract to the LLM:
# type -> {summary, handle, required, optional, min_inputs, requires_inputs,
# also_requires?}. The Pydantic Stage model does not expose this rendering shape,
# so it is preserved here so the prompt's rendered output is unchanged.
NODE_TYPES: dict[str, dict[str, _Any]] = {
    "input_data": {
        "summary": "Declares a source dataset with a typed schema.",
        "handle": "connector",
        "requires_inputs": False,
        "min_inputs": 0,
        "required": ["kind"],
        "optional": ["params", "refresh", "notes"],
    },
    "llm_transform": {
        "summary": "Row-by-row LLM call producing structured output.",
        "handle": "llm",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["prompt_template"],
        "optional": ["model", "temperature", "response_format", "max_retries",
                     "rubric", "tools"],
    },
    "python_transform": {
        "summary": "Arbitrary Python over upstream dataframes.",
        "handle": "function",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["kind"],
        "optional": ["module", "function", "code", "requirements"],
    },
    "join": {
        "summary": "Combine two or more upstream dataframes on keys.",
        "handle": "join",
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys"],
        "optional": ["type", "select", "on"],
    },
    "aggregate": {
        "summary": "Structured group-by aggregation.",
        "handle": "aggregate",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["group_by", "aggregations"],
        "optional": [],
    },
    "human_review_queue": {
        "summary": "Pulls flagged rows for human decision; halts the run.",
        "handle": "queue",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["filter", "hash_columns", "reviewer_instructions",
                     "routing", "conflict_resolution", "estimated_volume_per_week"],
    },
    "publish": {
        "summary": "Render a final artifact (html, json, csv, cards).",
        "handle": "publish",
        "also_requires": ["function"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["format", "destination", "template", "one_file_per", "cross_link"],
    },
}

NODE_TYPE_NAMES: set[str] = set(NODE_TYPES)

__all__ = [
    "StageType", "ConnectorKind", "FileFormat", "AggFormula", "JoinType",
    "FunctionKind", "PublishFormat", "is_valid_column_type",
    "SourceRef", "Column", "TableSchema", "Connector", "LLMConfig",
    "PythonFunction", "JoinKey", "JoinConfig", "AggregationOp",
    "AggregateConfig", "QueueConfig", "PublishConfig", "ReviewConfig",
    "Stage", "validate_stage",
    "Methodology", "parse_methodology", "validate_methodology",
    "check_unique_ids", "check_inputs_resolve", "detect_cycle",
    "SchemaKind", "NamedColumn", "NamedSchema", "SchemaLibrary",
    "parse_schema_library", "validate_schema_library", "validate_named_schema",
    "check_unique_schema_names", "check_references_resolve", "parse_reference",
    "EvalSpec", "build_ground_truth_schema", "validate_eval_spec",
    # compat constants
    "SCALAR_COLUMN_TYPES", "SCHEMA_KINDS", "JOIN_TYPES",
    "CONNECTOR_KINDS", "IMPLEMENTED_CONNECTOR_KINDS", "WEIGHTED_FORMULAS",
    "NODE_TYPES", "NODE_TYPE_NAMES",
]
