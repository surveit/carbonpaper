from __future__ import annotations

import re

from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.stage_types import (
    AUTHORABLE_CODE_CARRYING_TYPES,
    STAGE_TYPES,
    APPROVAL_REQUIRED_TYPES,
)


def test_human_review_queue_note_states_the_fingerprint_matching():
    note = STAGE_TYPES["human_review_queue"].notes
    assert note, "human_review_queue must carry a `notes` explanation"
    # the authoring agent needs to know editing filter/reviewer_instructions
    # invalidates every decision cached for this stage
    assert "fingerprint" in note
    assert "reviewer_instructions" in note


def test_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = STAGE_TYPES["human_review_queue"].notes
    assert note in EDITING_SYSTEM_PROMPT


def test_hrq_note_names_the_decision_values_the_runtime_actually_emits():
    from app.models.stages.human_review_queue import ReviewVerdict

    quoted = set(re.findall(r'"([a-z_]+)"', STAGE_TYPES["human_review_queue"].notes))
    assert quoted == {verdict.value for verdict in ReviewVerdict}


def test_hrq_note_names_every_queue_field_that_adds_a_column():
    # Read off `find_added_columns`, so a column-adding field breaking the `*_column` name counts.
    from app.models.stages.human_review_queue import QueueConfig, find_added_columns

    queue = QueueConfig(
        reviewed_columns={"src": "reviewed_src"}, verdict_column="v",
        reviewer_column="r", reviewed_at_column="at", review_notes_column="n",
    )
    # `find_added_columns` labels a reviewed target `queue.reviewed_columns['src']`;
    # the field itself is the part before the subscript.
    adding_fields = {field.split("[")[0] for field, _ in find_added_columns(queue)}
    mentioned = {f"queue.{name}" for name in re.findall(
        r"queue\.(\w+)", STAGE_TYPES["human_review_queue"].notes)}

    assert adding_fields <= mentioned, adding_fields - mentioned
    assert mentioned <= {f"queue.{name}" for name in QueueConfig.model_fields}, mentioned


def test_summary_budget_note_states_the_limit_the_write_path_refuses_on():
    from app.models.stages.code import SUMMARY_MAX_CHARS

    assert str(SUMMARY_MAX_CHARS) in CODE_SUMMARY_CONTRACT_NOTE
    assert "corner_cases" in CODE_SUMMARY_CONTRACT_NOTE


def test_the_shared_code_notes_reach_each_surface_exactly_once():
    # Once per prompt: a copy per type is what made the catalog unreadable.
    for prompt in _authoring_prompts():
        for note in (CODE_SUMMARY_CONTRACT_NOTE, CODE_CORNER_CASES_CONTRACT_NOTE):
            assert _flat(prompt).count(_flat(note)) == 1


def test_each_governed_type_is_marked_where_the_shared_note_is_stated():
    # Nothing else now connects the hoisted rule to the types it binds.
    for prompt in _authoring_prompts():
        for stage_type in AUTHORABLE_CODE_CARRYING_TYPES:
            assert f"`{stage_type}`" in prompt, stage_type


def test_a_withheld_type_is_named_but_never_offered_as_an_entry():
    """Named so a stuck model asks; never an entry, or it reads as available."""
    for prompt in _authoring_prompts():
        for stage_type in APPROVAL_REQUIRED_TYPES:
            assert stage_type in prompt, stage_type
            assert f"- {stage_type} —" not in prompt, stage_type


def test_both_prompts_say_how_a_withheld_type_is_turned_on():
    for prompt in _authoring_prompts():
        assert "approve_code_execution" in prompt
        assert "WAIT for their answer" in prompt


def test_no_type_note_still_carries_the_shared_text():
    # Guards the hoist: a note that re-absorbs the paragraph duplicates it again.
    for stage_type, spec in STAGE_TYPES.items():
        assert CODE_SUMMARY_CONTRACT_NOTE not in spec.notes, stage_type
        assert CODE_CORNER_CASES_CONTRACT_NOTE not in spec.notes, stage_type


def _authoring_prompts():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
    from app.mcp.server import INSTRUCTIONS

    return (EDITING_SYSTEM_PROMPT, INSTRUCTIONS)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_report_note_names_the_citation_call():
    note = STAGE_TYPES["report"].notes
    assert note, "report must carry a `notes` explanation"
    # the authoring agent has to know the keyword to declare and the call to make
    assert "citation_provider" in note
    assert "citation_provider.cite_value(" in note


def test_the_offered_report_type_is_the_sandboxed_one():
    """`report` is withheld pending approval, so its note is not what the agent reads."""
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert STAGE_TYPES["starlark_report"].notes in EDITING_SYSTEM_PROMPT
    assert STAGE_TYPES["report"].notes not in EDITING_SYSTEM_PROMPT


def test_the_sandboxed_report_prompt_names_every_builtin_the_runtime_binds():
    """A builtin the prompt omits is one an author never calls; one it invents fails to compile."""
    from app.models.stages.starlark_report import BUILTIN_SURFACE_NOTE, REPORT_BUILTINS

    for builtin in REPORT_BUILTINS:
        assert f"`{builtin}(" in BUILTIN_SURFACE_NOTE, builtin
