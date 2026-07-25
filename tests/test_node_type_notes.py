"""The human_review_queue facts an authoring model cannot read off the schema — how
reviewed rows are matched to a cached decision, and which output columns the runtime
actually populates — must reach it as PROMPT GUIDANCE, not only as a validation error
after the fact. They live in ONE place (`app.models.NODE_TYPES[...]["notes"]`) so the
prompt and the model can't drift; these guard that single source and its rendering
into the editing agent's system prompt.

(The chat-driven whole-workflow compiler that used to render the same note was
removed with the one-shot chain in #243; the MCP authoring surface teaches the shape
by `read_stage` on real stages rather than by a catalogue.)"""
from __future__ import annotations

from app import models as m


def test_human_review_queue_note_states_the_fingerprint_matching():
    note = m.NODE_TYPES["human_review_queue"].get("notes")
    assert note, "human_review_queue must carry a `notes` explanation"
    # the authoring agent needs to know editing filter/reviewer_instructions
    # invalidates every decision cached for this stage
    assert "fingerprint" in note
    assert "reviewer_instructions" in note


def test_human_review_queue_note_states_the_fixed_output_columns():
    note = m.NODE_TYPES["human_review_queue"]["notes"]
    # the runtime CONTRACT: output columns are fixed regardless of output_schema
    assert "output columns are FIXED" in note
    assert "decision" in note


def test_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = m.NODE_TYPES["human_review_queue"]["notes"]
    assert note in EDITING_SYSTEM_PROMPT
