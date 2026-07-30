"""Node-type runtime facts rendered into the authoring prompts, in ONE place so no two
prompts can drift apart.

Lives in app.models, not app.compiler: an import-linter contract admits only app.main
and app.services into app.compiler, which would lock out app.agents and app.mcp."""
from __future__ import annotations

# Appended to the notes of every type carrying authored code. The reviewer of a
# python stage is a journalist, not an engineer: the summary is the only part of
# the stage they can check, and the stage page leads with it.
CODE_SUMMARY_CONTRACT_NOTE = (
    "ALWAYS write the handle's `summary` alongside the code: one or two plain "
    "sentences telling a non-engineer what this step does and why, in the "
    "methodology's own words. It is what the stage page leads with — the code is "
    "shown last, folded — because the human reviewing this stage reads prose, not "
    "Python. Say the rule, not the implementation, name no Python constructs, and "
    "call out anything conditional (rows left untouched, values deliberately "
    "blank). Rewrite it in the same edit whenever the code changes."
)

CODE_CORNER_CASES_CONTRACT_NOTE = (
    "ALSO write the handle's `corner_cases` in that same edit: one entry per input "
    "whose handling a reader could not predict from the summary, each paired with the "
    "outcome it must produce. Blank or unparseable values, boundaries (say which side "
    "is inclusive), ties, duplicates, empty input, values outside an expected set. "
    "Keep them OUT of the summary, which has to stay short enough to actually be read "
    "— but do not leave them unsaid, because a description that is true about the "
    "common path and silent about the awkward input is how a reviewer approves a step "
    "that does the wrong thing. Both the summary and these cases are handed to the "
    "agent that derives this step's examples, and every case you state becomes an "
    "example the code must satisfy — so state the outcome the methodology gives, never "
    "one you are inventing to match the code you just wrote."
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
