"""Model-facing descriptions for the models `add_stage` binds its argument to.

Read by an authoring agent, not by a maintainer: each string is carried into the tool's
input schema, so editing one edits a prompt. Explicit for the reason
tests/arch/test_tool_descriptions_are_explicit.py already gives for tool descriptions.
"""
from __future__ import annotations


AGGREGATE_CONFIG_DESCRIPTION = "aggregate config block."

CONNECTOR_DESCRIPTION = "input_data config block."

CORNER_CASE_DESCRIPTION = "One input where a step's behaviour needs stating, and what must happen."

EXTENDS_SIGNATURE_DESCRIPTION = "An anchored stage's contract: the first input's rows, rewritten and extended."

FILTER_CONFIG_DESCRIPTION = (
    "filter_rows config block: an authored row predicate, `def should_include(row:\n"
    "dict) -> bool`. True keeps the row, False drops it; every kept row's\n"
    "columns pass through unchanged and its relative order is preserved.\n"
    "\n"
    "Inline code is the only source for that predicate: a filter decides, and a\n"
    "decision that needs an importable module is doing more than deciding. There\n"
    "is deliberately no `kind`/`module` here, unlike PythonFunction."
)

INPUT_READS_DESCRIPTION = "The columns a stage's transform consumes from ONE of its inputs."

JOIN_CONFIG_DESCRIPTION = "enrich/expand handle; cardinality lives in the stage TYPE."

LLM_CONFIG_DESCRIPTION = "llm_transform config block."

PUBLISH_CONFIG_DESCRIPTION = (
    "publish rendering config. The code a publish stage RUNS lives in its\n"
    "`function` block, not here."
)

PYTHON_FUNCTION_DESCRIPTION = (
    "Config block for python_row_function / python_frame_function (and publish). The\n"
    "row-vs-frame distinction lives in the stage `type`, not here — the runtime\n"
    "reads the type to decide whether to invoke this per row or per frame."
)

QUEUE_CONFIG_DESCRIPTION = "human_review_queue config block: what the human is asked, and what the stage adds."

REPLACES_SIGNATURE_DESCRIPTION = (
    "The contract of a reshaping stage: nothing flows through, the output is\n"
    "exactly `produces`."
)

STAGE_DRAFT_DESCRIPTION = (
    "One stage as an authoring client submits it: every config block optional\n"
    "and no cross-field validator. A stage that breaks a rule must parse here and\n"
    "be refused by `parse_stage` in the handler, where the refusal reaches the\n"
    "client on the handler's own channel rather than as a parameter-binding\n"
    "error. Shares `StageCommon` with the stored models, so the fields both carry\n"
    "are declared once."
)

STAGE_INPUT_DESCRIPTION = "Spelled `schema:` on a compiled stage; pydantic reserves `schema` on BaseModel."

STARLARK_FUNCTION_DESCRIPTION = "Config block for starlark_row_function: inline Starlark, no importable module."

TABLE_SCHEMA_DESCRIPTION = (
    "An anonymous schema — columns plus an optional primary key — that can be\n"
    "declared inline (e.g. on a stage's input edge). `NamedSchema` promotes it to\n"
    "a first-class, named artifact."
)

UNION_CONFIG_DESCRIPTION = (
    "union config block. No fields: a union's behavior is fixed entirely by its\n"
    "(schema-identical) declared inputs, concatenated in declared order."
)

FILTER_ROWS_STAGE_TEST_DESCRIPTION = "One row in → that row kept, dropped (`[]`), or a refusal."

PYTHON_FRAME_FUNCTION_STAGE_TEST_DESCRIPTION = (
    "Any rows in → any rows out, or a refusal: a frame function may reshape freely."
)

PYTHON_ROW_FUNCTION_STAGE_TEST_DESCRIPTION = "One row in → that one row out, or a refusal."

REVIEW_GUIDE_DRAFT_DESCRIPTION = "A guide as written, before it is addressed to a version and stored."

REVIEW_GUIDE_STEP_DESCRIPTION = (
    "One step — a Workflow section in the UI. `prose` may carry `backticked` columns."
)

SCHEMA_LIBRARY_DESCRIPTION = "The whole data model: named schemas with unique names and resolvable FKs."

SOURCE_REF_DESCRIPTION = "Where a stage's or schema's prose justification lives."

STAGE_TEST_DESCRIPTION = "A rows case states `expected` rows; a failure case states `expected: null`."

STARLARK_ROW_FUNCTION_STAGE_TEST_DESCRIPTION = "One row in → that one row out, or a refusal."

NAMED_COLUMN_DESCRIPTION = (
    "A Column that may carry a foreign key (`references`) to another named\n"
    "schema, by name or `schema.column`."
)

NAMED_SCHEMA_DESCRIPTION = (
    "One named table in the data model — a TableSchema with a `name`, a `kind`,\n"
    "a `primary_key`, and foreign-key-carrying columns. Column uniqueness is\n"
    "validated by TableSchema."
)
