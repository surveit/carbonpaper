"""Shared node-type facts both compiler prompts (prompt.py's one-shot CLI
compiler and workflow_prompt.py's live chat-driven compiler) must teach the
authoring model — kept in ONE place so the two prompts can't silently drift
apart on the same underlying facts about the runtime.

These are runtime CONTRACTS, not authoring style — discovered by actually
running compiled workflows and finding the compiler's assumptions didn't match
what the runtime handlers really do."""
from __future__ import annotations

HUMAN_REVIEW_QUEUE_CONTRACT_NOTE = (
    "output_schema DEFINES what the reviewer is asked for: every column it declares "
    "that the stage's input does not already carry becomes a field on the review form, "
    "which the human fills in per row (the human counterpart of an llm_transform's "
    "reply spec). Declare exactly the columns a human can supply by looking at one row "
    "— e.g. `final_score`, `review_notes` — plus every upstream column a later stage "
    "needs (undeclared upstream columns are silently dropped, unlike "
    "python_frame_function). Three names are filled by the runtime, never asked of the "
    "reviewer: `decision` (approve/modify/reject), `reviewer_id` and `reviewed_at` — "
    "declare them (never invented names like \"review_decision\") to keep them, and "
    "filter downstream on `decision == \"approve\"`. A rejected row is dropped from the "
    "output entirely; a row the queue filter never selected keeps its upstream columns "
    "and carries null in every reviewer-supplied and runtime-filled column."
)
