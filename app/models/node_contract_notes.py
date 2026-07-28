"""Node-type runtime facts rendered into the authoring prompts, in ONE place so no two
prompts can drift apart.

Lives in app.models, not app.compiler: an import-linter contract admits only app.main
and app.services into app.compiler, which would lock out app.agents and app.mcp."""
from __future__ import annotations

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
