from __future__ import annotations

from app.models import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
    HUMAN_REVIEW_QUEUE_CONTRACT_NOTE,
)
from app.models.stages.node_types import CODE_CARRYING_TYPES, NODE_TYPES


def test_human_review_queue_note_states_the_fingerprint_matching():
    note = NODE_TYPES["human_review_queue"].get("notes")
    assert note, "human_review_queue must carry a `notes` explanation"
    # the authoring agent needs to know editing filter/reviewer_instructions
    # invalidates every decision cached for this stage
    assert "fingerprint" in note
    assert "reviewer_instructions" in note


def test_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = NODE_TYPES["human_review_queue"]["notes"]
    assert note in EDITING_SYSTEM_PROMPT


def test_fixed_output_columns_contract_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert HUMAN_REVIEW_QUEUE_CONTRACT_NOTE in EDITING_SYSTEM_PROMPT


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
        assert f'"{value}"' in HUMAN_REVIEW_QUEUE_CONTRACT_NOTE, value


def test_summary_budget_note_states_the_limit_the_write_path_refuses_on():
    # the note tells an author to fit the behaviour in `summary` plus `corner_cases`;
    # naming a number stage_edit does not refuse above would send them to a wrong budget
    from app.models.stages.code import SUMMARY_MAX_CHARS

    assert str(SUMMARY_MAX_CHARS) in CODE_SUMMARY_CONTRACT_NOTE
    assert "corner_cases" in CODE_SUMMARY_CONTRACT_NOTE


def test_summary_budget_note_reaches_every_code_carrying_type():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    for stage_type in CODE_CARRYING_TYPES:
        assert CODE_SUMMARY_CONTRACT_NOTE in NODE_TYPES[stage_type]["notes"], stage_type
    assert CODE_SUMMARY_CONTRACT_NOTE in EDITING_SYSTEM_PROMPT


def test_corner_cases_note_reaches_every_code_carrying_type():
    # stage_edit refuses a write that omits `corner_cases`; this is the note that tells
    # an author the key is mandatory and `[]` is the way to say "none"
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    for stage_type in CODE_CARRYING_TYPES:
        assert CODE_CORNER_CASES_CONTRACT_NOTE in NODE_TYPES[stage_type]["notes"], stage_type
    assert CODE_CORNER_CASES_CONTRACT_NOTE in EDITING_SYSTEM_PROMPT


def test_publish_note_names_the_trace_link_helper():
    note = NODE_TYPES["publish"].get("notes")
    assert note, "publish must carry a `notes` explanation"
    # the authoring agent has to know the keyword to declare and the call to make
    assert "trace_links" in note
    assert "build_row_trace_url" in note


def test_publish_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = NODE_TYPES["publish"]["notes"]
    assert note in EDITING_SYSTEM_PROMPT
