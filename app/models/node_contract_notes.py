"""Node-type runtime facts rendered into the authoring prompts, in ONE place so no two
prompts can drift apart.

Lives in app.models, not app.compiler: an import-linter contract admits only app.main
and app.services into app.compiler, which would lock out app.agents and app.mcp."""
from __future__ import annotations

HUMAN_REVIEW_QUEUE_CONTRACT_NOTE = (
    "This type's output columns are the input columns it passes through PLUS exactly the "
    "columns its own `queue` block names: one added column per `queue.reviewed_columns` "
    "entry (a {source column -> added reviewed column} mapping), `queue.verdict_column`, "
    "`queue.reviewer_column`, `queue.reviewed_at_column`, and `queue.review_notes_column` "
    "when declared. Declare all of them in output_schema; and unlike python_frame_function, "
    "undeclared upstream columns are silently dropped, so declare every column a later "
    "stage needs to read. "
    "The added reviewed column is how human-reviewed data leaves this stage — the source "
    "column is NEVER modified in place: the AI's original value stays on the row for "
    "provenance and the human's value lands in the added column beside it. Name it "
    "`reviewed_<source>` by default, and declare it with the SAME type as its source column "
    "and nullability at least as permissive. A reviewed source column must be scalar "
    "(str/int/float/bool/date/datetime) — a json or list column cannot be reviewed through "
    "a form field and fails validation. No added column may reuse an input column's name, "
    "so two review stages in series must name their added columns differently. "
    "This type emits one output row per input row and never removes any. The verdict column "
    "holds \"approve\" (a human accepted the AI value), \"modify\" (a human supplied a "
    "different one), or \"skipped\" (the queue filter did not select the row, so the AI "
    "value stands unreviewed and no human saw it). A downstream stage that wants only "
    "human-sanctioned values filters on the verdict column != \"skipped\"."
)
