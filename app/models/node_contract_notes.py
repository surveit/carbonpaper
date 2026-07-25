"""Node-type facts the workflow-authoring prompts must teach the model, kept in
ONE place so no two prompts can drift apart on the same underlying facts about
the runtime. Rendered into the editing agent's system prompt alongside each
node type's own `notes` from NODE_TYPES.

These are runtime CONTRACTS, not authoring style — discovered by actually
running workflows and finding the authoring assumptions didn't match what the
runtime handlers really do.

Lives in app.models (not app.compiler) so every authoring surface can reach it:
app.compiler is protected by an import-linter contract that admits only
app.main and app.services, which would lock out app.agents and app.mcp."""
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
