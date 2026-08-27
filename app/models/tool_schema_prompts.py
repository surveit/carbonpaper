"""Model-facing descriptions for the models an authoring agent is handed a schema of.

Read by an agent, not by a maintainer: each string is carried into a tool's input schema,
so editing one edits a prompt. Explicit for the reason
tests/arch/test_tool_descriptions_are_explicit.py already gives for tool descriptions.
"""
from __future__ import annotations


CORNER_CASE_DESCRIPTION = "One input where a step's behaviour needs stating, and what must happen."

EXTENDS_SIGNATURE_DESCRIPTION = (
    "An anchored stage's contract: the first input's rows, rewritten and extended."
)

FILTER_ROWS_STAGE_TEST_DESCRIPTION = "One row in → that row kept, dropped (`[]`), or a refusal."

INPUT_READS_DESCRIPTION = "The columns a stage's transform consumes from ONE of its inputs."

NAMED_COLUMN_DESCRIPTION = (
    "A Column that may carry a foreign key (`references`) to another named\n"
    "schema, by name or `schema.column`."
)

NAMED_SCHEMA_ALSO_WRITTEN_DESCRIPTION = (
    "The other spellings this methodology uses for this same thing."
)

NAMED_SCHEMA_DESCRIPTION = (
    "One named table in the data model: a schema promoted to an addressable artifact,\n"
    "with foreign keys on its columns."
)

PYTHON_FRAME_FUNCTION_STAGE_TEST_DESCRIPTION = (
    "Any rows in → any rows out, or a refusal: a frame function may reshape freely."
)

PYTHON_FUNCTION_DESCRIPTION = (
    "Whether this runs per row or per whole frame is set by the stage `type`, not by\n"
    "anything in this block."
)

PYTHON_ROW_FUNCTION_STAGE_TEST_DESCRIPTION = "One row in → that one row out, or a refusal."

REPLACES_SIGNATURE_DESCRIPTION = (
    "The contract of a reshaping stage: nothing flows through, the output is\n"
    "exactly `produces`."
)

REPORT_CONFIG_DESCRIPTION = (
    "The code a report stage RUNS lives in its `function` block, not here."
)

REVIEW_GUIDE_DRAFT_DESCRIPTION = (
    "A guide as written, before it is addressed to a version and stored."
)

REVIEW_GUIDE_STEP_DESCRIPTION = (
    "One step — a Workflow section in the UI. `prose` may carry `backticked` columns."
)

SCHEMA_LIBRARY_DESCRIPTION = (
    "The whole data model: named schemas with unique names and resolvable FKs."
)

SOURCE_REF_DESCRIPTION = "Where a stage's or schema's prose justification lives."

STAGE_DRAFT_DESCRIPTION = (
    "One stage as an authoring client submits it: every config block is optional here, and\n"
    "a stage that breaks a rule is refused in the reply rather than rejected as a bad argument."
)

STAGE_EDIT_DESCRIPTION = "The stage to change, and the fields of it that change."

STAGE_TEST_DESCRIPTION = (
    "A rows case states `expected` rows; a failure case states `expected: null`."
)

STARLARK_ROW_FUNCTION_STAGE_TEST_DESCRIPTION = "One row in → that one row out, or a refusal."

TABLE_SCHEMA_DESCRIPTION = (
    "An anonymous schema — columns declared inline, e.g. an input_data stage's file."
)

TERMS_DESCRIPTION = (
    "A methodology's whole vocabulary: its nouns (the data model) and its verbs.\n"
    "One word carries one meaning — no word appears twice across either half."
)

VERB_ALSO_WRITTEN_DESCRIPTION = (
    "The other spellings this methodology uses for this same act."
)

VERB_DESCRIPTION = (
    "One act the methodology performs, under the word its owner uses for it — so a\n"
    "reader meets that word rather than a synonym chosen for them."
)
