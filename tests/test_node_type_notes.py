"""The human_review_queue hash-source requirement must reach BOTH workflow-authoring
agents as prompt guidance — not only as a validation error after the fact. These
guard that the NODE_TYPES `notes` for human_review_queue is rendered into the
chat-driven workflow compiler's system prompt AND the interactive editing agent's
system prompt, from the one source (app.models.NODE_TYPES) so the two prompts can't
drift."""
from __future__ import annotations

from app import models as m


def test_human_review_queue_note_states_the_fingerprint_matching():
    note = m.NODE_TYPES["human_review_queue"].get("notes")
    assert note, "human_review_queue must carry a `notes` explanation"
    # the authoring agent needs to know editing filter/reviewer_instructions
    # invalidates every decision cached for this stage
    assert "fingerprint" in note
    assert "reviewer_instructions" in note


def test_note_reaches_the_workflow_compiler_prompt():
    from app.compiler.workflow_prompt import WORKFLOW_SYSTEM_PROMPT

    note = m.NODE_TYPES["human_review_queue"]["notes"]
    assert note in WORKFLOW_SYSTEM_PROMPT


def test_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = m.NODE_TYPES["human_review_queue"]["notes"]
    assert note in EDITING_SYSTEM_PROMPT
