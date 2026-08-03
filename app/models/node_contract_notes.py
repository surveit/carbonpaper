"""Node-type runtime facts rendered into the authoring prompts, in ONE place so no two
prompts can drift apart.

Lives in app.models, not app.compiler: an import-linter contract admits only app.main
and app.services into app.compiler, which would lock out app.agents and app.mcp."""
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
    "This type's output columns are FIXED by the runtime regardless of what "
    "output_schema declares: only `decision`, `ai_score`, `human_score`, "
    "`final_score`, `review_notes`, `reviewer_id`, `reviewed_at`, plus "
    "passthrough columns are ever populated — declare exactly those (never invented "
    "names like \"review_decision\"). Declare `human_score` and `final_score` "
    "NULLABLE: both are null on a rejected row, and `human_score` is null on every "
    "row the filter passed through unreviewed. "
    "This type emits one output row per input row and never removes any: a rejected "
    "row is still emitted, carrying `decision == \"reject\"` with `human_score`/"
    "`final_score` null. Every output row carries a `decision` — \"approve\", "
    "\"modify\" or \"reject\" where a human decided, and \"not_reviewed\" where the "
    "queue filter passed the row through without review. So a downstream stage "
    "filtering on `decision != \"reject\"` is what excludes a rejected row; filtering "
    "on `decision == \"approve\"` would silently discard the unreviewed rows too. "
    "Unlike python_frame_function, undeclared upstream columns are silently dropped, so "
    "declare every column a later stage needs to read."
)
