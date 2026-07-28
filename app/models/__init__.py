"""The workflow contract, as Pydantic models.

Import from `app.models` (this aggregator) for the stable public surface.
"""
from app.models.coverage import Coverage
from app.models.node_contract_notes import HUMAN_REVIEW_QUEUE_CONTRACT_NOTE
from app.models.schema import (
    Column,
    JSON_COLUMN_TYPE,
    LIST_JSON_COLUMN_TYPE,
    RANGE_UNBOUNDED_MARKER,
    STR_COLUMN_TYPE,
    SourceRef,
    TableSchema,
    is_valid_column_type,
)
from app.models.stage import (
    AggFormula,
    AggregateConfig,
    AggregationOp,
    Connector,
    ConnectorKind,
    FileFormat,
    FunctionKind,
    StageInput,
    JoinConfig,
    JoinKey,
    JoinType,
    LLMConfig,
    PublishConfig,
    PublishFormat,
    PythonFunction,
    QueueConfig,
    ReviewConfig,
    RowReviewDecision,
    Stage,
    StageType,
    validate_stage,
)
from app.models.stages.stage_tests import StageTest
from app.models.workflow import (
    Workflow,
    validate_inputs_resolve,
    validate_unique_ids,
    detect_cycle,
    validate_publish_is_terminal,
    validate_edge_schemas,
    parse_workflow,
    validate_workflow,
    validate_workflow_draft,
)
from app.models.named_schemas import (
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
from app.models.table import TableRef
from app.models.eval import (
    CodeScorer,
    EvalConfig,
    EvalRun,
    EvalRunSettings,
    ExpectedOutput,
    ScoringMetric,
    StageOutputOverride,
)
# NOTE: the compiled-stage loader lives in app.services.loader (it does
# filesystem I/O, which is service work, not schema). Import it from there;
# app.models stays a pure, side-effect-free schema package.

# ── Compat vocabularies (the plain-data surface the compiler + prompt render) ──
# The Pydantic models above are the contract. The string/dict vocabularies below
# are what `app/prompt.py` renders into the LLM prompt and fenced-block contracts,
# and what `app/compiler.py` reads to name kinds. They are DERIVED from the enums
# where the two agree, so they can't drift; where the emit-vocabulary is broader
# than what the runtime executes, it is spelled out (see CONNECTOR_KINDS).
from typing import Any as _Any

# Scalar column-type vocabulary, re-exported from schema.py (its single
# definition). `list[<type>]` / dict / json are handled by is_valid_column_type.
from app.models.schema import SCALAR_COLUMN_TYPES

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
# app.agents.compiler.prompt renders this into the editing agent's system prompt:
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
        "notes": (
            "NEVER include a file path — where data physically lives is not "
            "part of the methodology; the user binds a file when starting a run. "
            "Takes no inputs, but must still declare its output_schema."
        ),
    },
    "llm_transform": {
        "summary": "Row-by-row LLM call producing structured output.",
        "handle": "llm",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["prompt_data_template"],
        "optional": ["model", "temperature", "response_format", "max_retries",
                     "rubric", "tools"],
        "notes": (
            "Author it as TWO fields: prompt_instructions is the row-invariant guidance "
            "(role, methodology, how to weigh evidence/sources) and MUST NOT depend on "
            "any row value — the same instructions run over every input row, so keeping "
            "them byte-stable and separate from per-row data lets the runtime cache that "
            "prefix, cutting latency (and cost on a per-token backend). "
            "prompt_data_template is the minimal per-row input framing, rendered with "
            "Python's str.format_map: inject a column as {column_name}. "
            "Its single input's schema must declare a primary_key, and its output_schema "
            "must be strictly ADDITIVE and 1:1: the SAME primary_key as that input, every "
            "input column unchanged, plus at least one new column (one input row -> one "
            "output row)."
        ),
    },
    "python_row_function": {
        "summary": "Deterministic Python run once per row: one row in → one row out (cannot fan rows out/in or reorder).",
        "handle": "function",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["kind"],
        "optional": ["module", "function", "code", "requirements"],
        "notes": (
            "Takes exactly ONE input — two or more is a join or a python_frame_function. "
            "`transform(row)` is handed a plain dict and must return a plain dict, and that "
            "dict IS the output row: a key you do not return is absent from the output, so "
            "carry columns through explicitly (`return {**row, ...}`). The function is shown "
            "neither the frame nor the row's position, so it cannot fan out, drop or reorder."
        ),
    },
    "python_frame_function": {
        "summary": "Deterministic Python over the whole dataframe(s); may reshape (dedup, pivot, multi-input merge).",
        "handle": "function",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["kind"],
        "optional": ["module", "function", "code", "requirements"],
        "notes": (
            "The runtime calls `transform(*frames)`: one POSITIONAL parameter per declared "
            "input, in `inputs` order — never by name, never a dict of frames. It receives no "
            "output_dir and no trace_links; writing files is publish's job. Return the output "
            "DataFrame. Rows may be added, dropped or reordered here, so this stage breaks the "
            "row-position provenance trail an upstream row-mapped stage preserves."
        ),
    },
    "join": {
        "summary": "Combine two or more upstream dataframes on keys.",
        "handle": "join",
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys"],
        "optional": ["type", "select", "on"],
        "notes": (
            "Merges the FIRST TWO inputs only: inputs[0] is left, inputs[1] is right, and a "
            "third declared input is never merged in. A right column whose name a left column "
            "shares arrives as `<name>_r`; a key pair with the SAME name on both sides "
            "collapses into one column. `select` and output_schema may name only columns the "
            "merge produces — anything else is rejected when the stage is saved."
        ),
    },
    "aggregate": {
        "summary": "Structured group-by aggregation.",
        "handle": "aggregate",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["group_by", "aggregations"],
        "optional": [],
        "notes": (
            "Output columns are exactly group_by plus each aggregation's output_column — every "
            "other input column is DROPPED, so carry anything needed downstream via group_by "
            "or a `first` aggregation. formula `count` takes no value_column; every other "
            "formula requires one. Declared output types must match "
            "the derivation: count->int, mean->float, min/max/first->the value column's type, "
            "list->list[<that type>]."
        ),
    },
    "human_review_queue": {
        "summary": "Pulls flagged rows for human decision; halts the run.",
        "handle": "queue",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["filter", "reviewer_instructions",
                     "routing", "conflict_resolution", "estimated_volume_per_week"],
        "notes": (
            "Reviewed rows are matched to a cached human decision by "
            "fingerprinting the row itself — no column configuration is needed. "
            "Editing `filter` or `reviewer_instructions` changes the stage's "
            "definition fingerprint, so every previously cached decision for "
            "this stage stops matching and every row is asked again."
        ),
    },
    "publish": {
        "summary": "Render a final artifact (html, json, csv, cards).",
        "handle": "publish",
        "also_requires": ["function"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["format", "destination", "template", "one_file_per", "cross_link"],
        "notes": (
            "Published output must be INTERROGABLE: every row or claim it renders links "
            "back to that row's provenance. Declare the keyword `trace_links` on the "
            "function — `def transform(df, output_dir, trace_links)` — and the runtime "
            "hands it a linker for this run; per row emit "
            "`trace_links.build_row_trace_url(\"<the input stage's id>\", row_ordinal)` as "
            "an href, where row_ordinal is that row's 0-based position in the input frame "
            "AS RECEIVED. Iterate the frame in order (enumerate it) and do not sort, "
            "filter, or dedup before reading the ordinal — position is the only key the "
            "trace has. Omit the keyword for a format that cannot carry a link (csv, json). "
            "The one type exempt from declaring an output_schema: it emits files, not a table."
        ),
    },
}

NODE_TYPE_NAMES: set[str] = set(NODE_TYPES)

__all__ = [
    "Coverage",
    "StageType", "ConnectorKind", "FileFormat", "AggFormula", "JoinType",
    "FunctionKind", "PublishFormat", "is_valid_column_type",
    "SourceRef", "Column", "TableSchema", "Connector", "LLMConfig",
    "PythonFunction", "JoinKey", "JoinConfig", "AggregationOp",
    "AggregateConfig", "QueueConfig", "PublishConfig", "ReviewConfig",
    "RowReviewDecision",
    "StageInput", "Stage", "StageTest", "validate_stage",
    "Workflow", "parse_workflow", "validate_workflow", "validate_workflow_draft",
    "validate_unique_ids", "validate_inputs_resolve", "detect_cycle",
    "validate_publish_is_terminal", "validate_edge_schemas",
    "SchemaKind", "NamedColumn", "NamedSchema", "SchemaLibrary",
    "parse_schema_library", "validate_schema_library", "validate_named_schema",
    "validate_unique_schema_names", "validate_references_resolve", "parse_reference",
    # general
    "TableRef",
    # eval contract
    "StageOutputOverride", "ExpectedOutput", "ScoringMetric", "CodeScorer", "EvalConfig",
    "EvalRunSettings", "EvalRun",
    # compat vocabularies (rendered into the authoring prompts)
    "SCALAR_COLUMN_TYPES", "SCHEMA_KINDS", "JOIN_TYPES", "CONNECTOR_KINDS",
    "NODE_TYPES", "NODE_TYPE_NAMES", "HUMAN_REVIEW_QUEUE_CONTRACT_NOTE",
    # individual column-type comparison handles
    "STR_COLUMN_TYPE", "JSON_COLUMN_TYPE", "LIST_JSON_COLUMN_TYPE",
    "RANGE_UNBOUNDED_MARKER",
]
