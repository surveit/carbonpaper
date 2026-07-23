"""Shared node-type facts both compiler prompts (prompt.py's one-shot CLI
compiler and workflow_prompt.py's live chat-driven compiler) must teach the
authoring model — kept in ONE place so the two prompts can't silently drift
apart on the same underlying facts about the runtime.

These are runtime CONTRACTS, not authoring style — discovered by actually
running compiled workflows and finding the compiler's assumptions didn't match
what the runtime handlers really do."""
from __future__ import annotations

LLM_TRANSFORM_TOOL_CALLING_NOTE = (
    "At runtime the model answers ONLY by calling submit_answer — a prose answer is "
    "silently discarded even if correct — so prompt_instructions must include an "
    "explicit instruction to call submit_answer with the verdict, never explain it as "
    "text. This directive is the same for every row, so it belongs in the row-invariant "
    "prompt_instructions, not the per-row prompt_data_template."
)

HUMAN_REVIEW_QUEUE_CONTRACT_NOTE = (
    "This type's output columns are FIXED by the runtime regardless of what "
    "output_schema declares: only `decision` (approve/modify/reject), `ai_score`, "
    "`human_score`, `final_score`, `review_notes`, `reviewer_id`, `reviewed_at`, plus "
    "passthrough columns are ever populated — declare exactly those (never invented "
    "names like \"review_decision\") and filter downstream on `decision == \"approve\"`. "
    "Unlike python_frame_function, undeclared upstream columns are silently dropped, so "
    "declare every column a later stage needs to read."
)
