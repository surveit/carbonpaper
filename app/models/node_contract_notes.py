# Lives in app.models, not app.compiler: an import-linter contract admits only
# app.main and app.services into app.compiler, which would lock out app.agents
# and app.mcp.
from __future__ import annotations

from app.models.stages.code import SUMMARY_MAX_CHARS

# Appended to the notes of every type carrying authored code. The limit is
# interpolated rather than spelled out so the prompt cannot outlive the number
# app.services.stage_edit actually refuses on.
CODE_SUMMARY_CONTRACT_NOTE = (
    "The description is a BUDGET ON THE CODE, not an annotation of it: the block's "
    f"`summary` ({SUMMARY_MAX_CHARS} characters, refused above that) plus its "
    "`corner_cases` is the whole space this step's behaviour gets. Author both in the "
    "same edit as the code, and rewrite them in the same edit whenever it changes. If "
    "the behaviour will not fit that budget precisely enough for a reader who never "
    "sees the code to reconstruct it exactly, the step does too much — downscope it or "
    "split the stage. The description does not grow to fit the code. Why the budget "
    "binds: the human reviewing this stage is a journalist, not an engineer, and the "
    "stage page leads with the summary — the code is shown last, folded — so the "
    "summary is the only part they check; and the agent that generates this stage's "
    "test examples is shown the summary, the corner cases and the schemas, never the code "
    "and never the methodology, then its examples are run against the real code. "
    "Examples that fail on code you believe is correct mean the description "
    "under-determined the behaviour."
)

# Deliberately one sentence: the field's own description carries the substance, and
# repeating it here only gives the two places to drift apart.
CODE_CORNER_CASES_CONTRACT_NOTE = (
    "ALWAYS submit the block's `corner_cases` alongside the summary, in the same edit "
    "— an empty list if the step genuinely has none, but never omitted."
)

HUMAN_REVIEW_QUEUE_CONTRACT_NOTE = (
    "This type's output columns are the input columns it passes through PLUS exactly the "
    "columns its own `queue` block names: one added column per `queue.reviewed_columns` "
    "entry, `queue.verdict_column`, `queue.reviewer_column`, `queue.reviewed_at_column`, "
    "and `queue.review_notes_column` when declared. Declare all of them in output_schema; "
    "and unlike python_frame_function, undeclared upstream columns are silently dropped, so "
    "declare every column a later stage needs to read. Those four columns record the act "
    "of reviewing — what verdict, who, when, what notes — and must "
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
