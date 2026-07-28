from __future__ import annotations

from app import models as m


def test_human_review_queue_note_states_the_fingerprint_matching():
    note = m.NODE_TYPES["human_review_queue"].get("notes")
    assert note, "human_review_queue must carry a `notes` explanation"
    # the authoring agent needs to know editing filter/reviewer_instructions
    # invalidates every decision cached for this stage
    assert "fingerprint" in note
    assert "reviewer_instructions" in note


def test_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = m.NODE_TYPES["human_review_queue"]["notes"]
    assert note in EDITING_SYSTEM_PROMPT


def test_fixed_output_columns_contract_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert m.HUMAN_REVIEW_QUEUE_CONTRACT_NOTE in EDITING_SYSTEM_PROMPT


def test_hrq_note_names_the_decision_values_the_runtime_actually_emits():
    """The note tells an author to exclude rejected rows with
    `decision != "reject"`. That instruction is only correct while the strings
    it names are the ones the queue handler writes — including the value it puts
    on a row the queue filter passed through unreviewed, which is what makes the
    documented filter safe without reasoning about a missing value. Pinned
    against the handler's own constants so the guidance cannot drift from the
    runtime it describes."""
    from app.models import RowReviewDecision
    from app.runtime.stages.human_review_queue import NOT_REVIEWED

    for value in (RowReviewDecision.reject.value, RowReviewDecision.approve.value,
                  RowReviewDecision.modify.value, NOT_REVIEWED):
        assert f'"{value}"' in m.HUMAN_REVIEW_QUEUE_CONTRACT_NOTE, value


def test_publish_note_names_the_trace_link_helper():
    note = m.NODE_TYPES["publish"].get("notes")
    assert note, "publish must carry a `notes` explanation"
    # the authoring agent has to know the keyword to declare and the call to make
    assert "trace_links" in note
    assert "build_row_trace_url" in note


def test_publish_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = m.NODE_TYPES["publish"]["notes"]
    assert note in EDITING_SYSTEM_PROMPT
