"""The workflow contract, as Pydantic models.

Import from `app.models` (this aggregator) for the stable public surface.
"""
from app.models.coverage import Coverage
from app.models.errors import StepRefused
from app.models.node_contract_notes import (
    CODE_SUMMARY_CONTRACT_NOTE,
    HUMAN_REVIEW_QUEUE_CONTRACT_NOTE,
)
from app.models.schema import (
    Column,
    FunctionKind,
    JSON_COLUMN_TYPE,
    LIST_JSON_COLUMN_TYPE,
    RANGE_UNBOUNDED_MARKER,
    STR_COLUMN_TYPE,
    SourceRef,
    TableSchema,
    is_valid_column_type,
)
from app.models.stage import (
    ReviewConfig,
    Stage,
    StageBase,
    StageDraft,
    StageInput,
    StageType,
    parse_stage,
    validate_stage,
)
from app.models.stages.aggregate import AggFormula, AggregateConfig, AggregationOp
from app.models.stages.filter_rows import FilterConfig
from app.models.stages.human_review_queue import QueueConfig, RowReviewDecision
from app.models.stages.input_data import (
    Connector,
    ConnectorKind,
    FileFormat,
    XlsxReadParams,
)
from app.models.stages.join import JoinConfig, JoinKey
from app.models.stages.llm_transform import LLMConfig
from app.models.stages.publish import PublishConfig, PublishFormat
from app.models.stages.union import UnionConfig
from app.models.stages.code import PythonFunction
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

# The connector kinds the compiler may EMIT and the prompt advertises to the LLM
# (the six listed below). This is deliberately broader than the ConnectorKind
# enum, which lists only the kinds the runtime executes today (file); a stage
# using any other kind is a valid draft but not yet runnable.
CONNECTOR_KINDS: set[str] = {
    "file", "http", "scrape", "api", "manual_upload", "sql",
}

# ── The node types as prompt copy ────────────────────────────────────────────
# app.agents.compiler.prompt renders this into the editing agent's system prompt:
# type -> {summary, blocks, required, optional, min_inputs, requires_inputs}.
# `blocks` names the config blocks that type's stage model requires. The models
# do not expose this rendering shape, so the copy is kept here as plain data
# purely for prompt rendering.
NODE_TYPES: dict[str, dict[str, _Any]] = {
    "input_data": {
        "summary": "Declares a source dataset with a typed schema.",
        "blocks": ["connector"],
        "requires_inputs": False,
        "min_inputs": 0,
        "required": ["kind"],
        "optional": ["params", "refresh", "notes"],
        "notes": (
            "When the methodology names a specific static file, params.path may "
            "carry it and MUST be an ABSOLUTE path; when the source does not say "
            "where the data lives, omit path — the user binds a file when starting "
            "a run. Never invent a path. "
            "For format=xlsx, optional params select the sheet and skip leading "
            "rows or columns: sheet_name (name or 0-based position, default first "
            "sheet), header_row (0-based index of the header row, default 0) and "
            "first_column (0-based index of the first column read, default 0). "
            "Takes no inputs, but must still declare its output_schema."
        ),
    },
    "llm_transform": {
        "summary": "Row-by-row LLM call producing structured output.",
        "blocks": ["llm"],
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
        "blocks": ["function"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["kind"],
        "optional": ["module", "function", "code", "requirements"],
        "notes": (
            "Takes exactly ONE input — to combine data from another input use enrich/expand, "
            "or python_frame_function. "
            "`transform(row)` is handed a plain dict and must return a plain dict, and that "
            "dict IS the output row: a key you do not return is absent from the output, so "
            "carry columns through explicitly (`return {**row, ...}`). The function is shown "
            "neither the frame nor the row's position, so it cannot fan out, drop or reorder."
        ),
    },
    "python_frame_function": {
        "summary": "Deterministic Python over the whole dataframe(s); may reshape (dedup, pivot, multi-input merge).",
        "blocks": ["function"],
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
    "enrich": {
        "summary": "Adds reference columns to each subject row; the reference must be unique on the key (many-to-one).",
        "blocks": ["join"],
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys"],
        "optional": ["select"],
        "notes": (
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "Row count and order come out unchanged, because the reference is required to hold "
            "at most ONE row per key: the runtime asks pandas to VERIFY that, so a reference "
            "that repeats a key FAILS THE RUN rather than silently multiplying rows. Use "
            "`expand` when the fan-out is intended. Every subject row survives — an unmatched "
            "one carries nulls for the reference columns — and an unmatched reference row is "
            "dropped. This stage NEVER drops a subject row: to drop rows (e.g. inner-join "
            "semantics), follow it with a `filter_rows` on a reference column being non-null, "
            "which records the row loss instead of hiding it. "
            "A reference column whose name a subject column shares arrives as `<name>_r`; a key "
            "pair with the SAME name on both sides collapses into one column. `select` and "
            "output_schema may name only columns the join produces — anything else is rejected "
            "when the stage is saved."
        ),
    },
    "expand": {
        "summary": "Joins reference rows into each subject row, fanning one subject row out to several (many-to-many).",
        "blocks": ["join"],
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys"],
        "optional": ["select"],
        "notes": (
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "The reference MAY hold several rows per key, so one subject row may come out as "
            "several — deliberate fan-out. Use `enrich` instead when the reference is meant to "
            "be unique on the key and a repeat is a bug you want caught. Every subject row "
            "survives — an unmatched one carries nulls for the reference columns — and an "
            "unmatched reference row is dropped. This stage NEVER drops a subject row: to drop "
            "rows (e.g. inner-join semantics), follow it with a `filter_rows` on a reference "
            "column being non-null, which records the row loss instead of hiding it. "
            "A reference column whose name a subject column shares arrives as `<name>_r`; a key "
            "pair with the SAME name on both sides collapses into one column. `select` and "
            "output_schema may name only columns the join produces — anything else is rejected "
            "when the stage is saved."
        ),
    },
    "aggregate": {
        "summary": "Structured group-by aggregation.",
        "blocks": ["aggregate"],
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
        "blocks": ["queue"],
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
        "blocks": ["publish", "function"],
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
    "union": {
        "summary": "Concatenate two or more upstream dataframes with an identical schema.",
        "blocks": ["union"],
        "requires_inputs": True,
        "min_inputs": 2,
        "required": [],
        "optional": [],
        "notes": (
            "No configuration — pass `union: {}`. Every input must declare an IDENTICAL "
            "schema (same columns, same types); a mismatch is refused when the stage is "
            "saved, naming the differing columns. Concatenates the inputs in declared "
            "order; output_schema must equal that shared schema."
        ),
    },
    "filter_rows": {
        "summary": "Keep the rows an authored predicate returns True for.",
        "blocks": ["filter"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["code"],
        "optional": ["function"],
        "notes": (
            "Takes exactly ONE input. The predicate is INLINE code only — there is no "
            "kind/module here; a filter that needs an importable module is doing more "
            "than deciding. `should_include(row)` is handed a plain dict and "
            "must return a bool — True keeps the row, False drops it; any other return "
            "type is a run-time error. Kept rows preserve their original relative order "
            "and every column unchanged, so output_schema must equal the input schema."
        ),
    },
}

# The types whose config carries authored code all owe a plain-language
# `summary`. Folded into their notes here rather than repeated in each entry, so
# every renderer of NODE_TYPES (the MCP instructions, the editing agent's
# catalog) states the obligation without one of them being able to forget it.
CODE_CARRYING_TYPES = ("python_row_function", "python_frame_function", "publish", "filter_rows")
for _type_name in CODE_CARRYING_TYPES:
    _spec = NODE_TYPES[_type_name]
    _spec["notes"] = f"{_spec['notes']} {CODE_SUMMARY_CONTRACT_NOTE}"
    _spec["optional"] = [*_spec["optional"], "summary"]

NODE_TYPE_NAMES: set[str] = set(NODE_TYPES)

__all__ = [
    "Coverage",
    "StepRefused",
    "StageType", "ConnectorKind", "FileFormat", "AggFormula",
    "FunctionKind", "PublishFormat", "is_valid_column_type",
    "SourceRef", "Column", "TableSchema", "Connector", "LLMConfig",
    "PythonFunction", "JoinKey", "JoinConfig", "AggregationOp",
    "AggregateConfig", "QueueConfig", "PublishConfig", "ReviewConfig",
    "RowReviewDecision", "UnionConfig", "FilterConfig",
    "StageInput", "Stage", "StageBase", "StageDraft", "StageTest", "XlsxReadParams",
    "parse_stage", "validate_stage",
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
    "SCALAR_COLUMN_TYPES", "SCHEMA_KINDS", "CONNECTOR_KINDS",
    "NODE_TYPES", "NODE_TYPE_NAMES", "HUMAN_REVIEW_QUEUE_CONTRACT_NOTE",
    "CODE_SUMMARY_CONTRACT_NOTE", "CODE_CARRYING_TYPES",
    # individual column-type comparison constants
    "STR_COLUMN_TYPE", "JSON_COLUMN_TYPE", "LIST_JSON_COLUMN_TYPE",
    "RANGE_UNBOUNDED_MARKER",
]
