"""The workflow contract, as Pydantic models.

Split across modules:
  - schema.py        — model base + the Column/TableSchema primitives
  - stage.py         — node types, handle blocks, the Stage model
  - workflow.py      — the Workflow model + cross-stage graph checks
  - named_schemas.py — the named data model (NamedSchema, SchemaLibrary)
  - table.py         — TableRef (a general on-disk table pointer)
  - eval.py          — the eval contract (EvalConfig, EvalRun, scorability)

Import from `app.core.models` (this aggregator) for the stable public surface.
"""
from app.core.models.schema import (
    Column,
    SourceRef,
    TableSchema,
    is_valid_column_type,
)
from app.core.models.stage import (
    AggFormula,
    AggregateConfig,
    AggregationOp,
    Connector,
    ConnectorKind,
    FileFormat,
    FunctionKind,
    InputRef,
    JoinConfig,
    JoinKey,
    JoinType,
    LLMConfig,
    PublishConfig,
    PublishFormat,
    PythonFunction,
    QueueConfig,
    ReviewConfig,
    Stage,
    StageType,
    validate_stage,
)
from app.core.models.stages.stage_tests import StageTest
from app.core.models.workflow import (
    Workflow,
    validate_inputs_resolve,
    validate_unique_ids,
    detect_cycle,
    parse_workflow,
    validate_workflow,
    validate_workflow_draft,
)
from app.core.models.named_schemas import (
    NamedColumn,
    NamedSchema,
    SchemaKind,
    SchemaLibrary,
    validate_references_resolve,
    validate_unique_schema_names,
    parse_reference,
    parse_schema_library,
    validate_named_schema,
    validate_schema_library,
)
from app.core.models.table import TableRef
from app.core.models.eval import (
    CodeScorer,
    EvalConfig,
    EvalRun,
    EvalRunSettings,
    ExpectedOutput,
    StageOutputOverride,
)
# NOTE: the compiled-stage loader lives in app.services.loader (it does
# filesystem I/O, which is service work, not schema). Import it from there;
# app.core.models stays a pure, side-effect-free schema package.

# ── Compat vocabularies (the plain-data surface the compiler + prompt render) ──
# The Pydantic models above are the contract. The string/dict vocabularies below
# are what `app/prompt.py` renders into the LLM prompt and fenced-block contracts,
# and what `app/compiler.py` reads to name kinds. They are DERIVED from the enums
# where the two agree, so they can't drift; where the emit-vocabulary is broader
# than what the runtime executes, it is spelled out (see CONNECTOR_KINDS).
from typing import Any as _Any

# Scalar column-type vocabulary, re-exported from schema.py (its single
# definition). `list[<type>]` / dict / json are handled by is_valid_column_type.
from app.core.models.schema import SCALAR_COLUMN_TYPES

# Kind/type vocabularies as string sets, derived from the enums so they stay in
# lockstep with the models the runtime validates against.
SCHEMA_KINDS: set[str] = {k.value for k in SchemaKind}
JOIN_TYPES: set[str] = {j.value for j in JoinType}

# The connector kinds the compiler may EMIT and the prompt advertises to the LLM
# (the six listed below). This is deliberately broader than the ConnectorKind
# enum, which lists only the kinds the runtime executes today (file); a stage
# using any other kind is a valid draft but not yet runnable.
CONNECTOR_KINDS: set[str] = {
    "file", "http", "scrape", "api", "manual_upload", "sql",
}

# ── The seven node types and their handle-block contract ─────────────────────
# prompt._node_type_contract() iterates this to render the contract to the LLM:
# type -> {summary, handle, required, optional, min_inputs, requires_inputs,
# also_requires?}. The Stage model does not expose this rendering shape, so the
# spec is kept here as plain data purely for prompt rendering.
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
    "InputRef", "Stage", "StageTest", "validate_stage",
    "Workflow", "parse_workflow", "validate_workflow", "validate_workflow_draft",
    "validate_unique_ids", "validate_inputs_resolve", "detect_cycle",
    "SchemaKind", "NamedColumn", "NamedSchema", "SchemaLibrary",
    "parse_schema_library", "validate_schema_library", "validate_named_schema",
    "validate_unique_schema_names", "validate_references_resolve", "parse_reference",
    # general
    "TableRef",
    # eval contract
    "StageOutputOverride", "ExpectedOutput", "CodeScorer", "EvalConfig",
    "EvalRunSettings", "EvalRun",
    # compat vocabularies (rendered by prompt.py / read by compiler.py)
    "SCALAR_COLUMN_TYPES", "SCHEMA_KINDS", "JOIN_TYPES", "CONNECTOR_KINDS",
    "NODE_TYPES", "NODE_TYPE_NAMES",
]
