"""Node-type runtime facts rendered into the authoring prompts, in ONE place so no two
prompts can drift apart.

Lives in app.models, not app.compiler: an import-linter contract admits only app.main
and app.services into app.compiler, which would lock out app.agents and app.mcp."""
from __future__ import annotations

HUMAN_REVIEW_QUEUE_CONTRACT_NOTE = (
    "This type's output columns are the input columns it passes through PLUS exactly the "
    "columns its own `queue` block names: one added column per `queue.reviewed_columns` "
    "entry, `queue.verdict_column`, `queue.reviewer_column`, `queue.reviewed_at_column`, "
    "and `queue.review_notes_column` when declared. Declare all of them in output_schema; "
    "and unlike python_frame_function, undeclared upstream columns are silently dropped, so "
    "declare every column a later stage needs to read. Those four bookkeeping columns must "
    "be declared type str — `queue.reviewed_at_column` too, never date or datetime — and "
    "all but `queue.verdict_column` must be declared nullable, since a skipped or "
    "auto-approved row carries no reviewer, timestamp or note. "
    "A reviewed column is ADDED beside its source, whose value is never modified in place: "
    "name it `reviewed_<source>` by default and declare it with the SAME spec as its source "
    "column — type, enum and range alike — and nullability at least as permissive. A "
    "reviewed source column must be scalar (str/int/float/bool/date/datetime): a json or "
    "list column cannot be reviewed through a form field and fails validation. No added "
    "column may reuse an input column's name, so two review stages in series must name "
    "their added columns differently. `queue.filter` may reference INPUT columns only, "
    "never a column this stage adds. "
    "This type emits one output row per input row and never removes any. The verdict column "
    "holds \"approve\" (a human accepted the value this stage received), \"modify\" (a "
    "human supplied a different one), or \"skipped\" (the queue filter did not select the "
    "row, so the received value stands unreviewed). A downstream stage that wants only "
    "human-sanctioned values "
    "filters on the verdict column != \"skipped\"."
)
