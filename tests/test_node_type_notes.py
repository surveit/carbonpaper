"""The human_review_queue hash-source requirement must reach BOTH workflow-authoring
agents as prompt guidance — not only as a validation error after the fact. These
guard that the NODE_TYPES `notes` for human_review_queue is rendered into the batch
compiler's contract AND the interactive editing agent's system prompt, from the one
source (app.models.NODE_TYPES) so the two prompts can't drift."""
from __future__ import annotations

from app import models as m


def test_human_review_queue_note_states_the_hash_requirement():
    note = m.NODE_TYPES["human_review_queue"].get("notes")
    assert note, "human_review_queue must carry a `notes` explanation"
    # both ways to satisfy the requirement, stated for the authoring agent
    assert "hash_columns" in note
    assert "primary_key" in note


def test_note_reaches_the_batch_compiler_prompt():
    from app.compiler.prompt import _node_type_contract

    note = m.NODE_TYPES["human_review_queue"]["notes"]
    assert note in _node_type_contract()


def test_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = m.NODE_TYPES["human_review_queue"]["notes"]
    assert note in EDITING_SYSTEM_PROMPT
