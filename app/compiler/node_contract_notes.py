"""Shared node-type facts the compiler-facing prompts (workflow_prompt.py's
chat-driven compiler and the editing agent's prompt) must teach the authoring
model — kept in ONE place so the prompts can't silently drift apart on the
same underlying facts about the runtime.

These are runtime CONTRACTS, not authoring style — discovered by actually
running compiled workflows and finding the compiler's assumptions didn't match
what the runtime handlers really do."""
from __future__ import annotations

HUMAN_REVIEW_QUEUE_CONTRACT_NOTE = (
    "This type's output columns are FIXED by the runtime regardless of what "
    "output_schema declares: only `decision` (approve/modify/reject), `ai_score`, "
    "`human_score`, `final_score`, `review_notes`, `reviewer_id`, `reviewed_at`, plus "
    "passthrough columns are ever populated — declare exactly those (never invented "
    "names like \"review_decision\"). This type emits one output row per input row and "
    "never removes any: a rejected row is still emitted, carrying `decision == \"reject\"` "
    "with `human_score`/`final_score` null, so a downstream stage filtering on "
    "`decision == \"approve\"` is what actually excludes it. "
    "Unlike python_frame_function, undeclared upstream columns are silently dropped, so "
    "declare every column a later stage needs to read."
)
