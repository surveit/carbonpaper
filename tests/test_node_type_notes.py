from __future__ import annotations

import re

from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.node_types import CODE_CARRYING_TYPES, NODE_TYPES


def test_human_review_queue_note_states_the_fingerprint_matching():
    note = NODE_TYPES["human_review_queue"].notes
    assert note, "human_review_queue must carry a `notes` explanation"
    # the authoring agent needs to know editing filter/reviewer_instructions
    # invalidates every decision cached for this stage
    assert "fingerprint" in note
    assert "reviewer_instructions" in note


def test_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = NODE_TYPES["human_review_queue"].notes
    assert note in EDITING_SYSTEM_PROMPT


def test_hrq_note_names_the_decision_values_the_runtime_actually_emits():
    # The note tells an author how to filter on the verdict column. That guidance is only
    # correct while the quoted strings it names are exactly the ones a queue stage's
    # verdict column can hold — including `skipped`, which the handler writes for a row
    # the filter passed through unreviewed and which is what makes a downstream filter
    # safe without reasoning about a missing value. Pinned in BOTH directions against the
    # enum: a member added or renamed there and not taught here fails, and so does a
    # verdict the note still names after the enum stopped emitting it.
    from app.models.stages.human_review_queue import ReviewVerdict

    quoted = set(re.findall(r'"([a-z_]+)"', NODE_TYPES["human_review_queue"].notes))
    assert quoted == {verdict.value for verdict in ReviewVerdict}


def test_hrq_note_names_every_queue_field_that_adds_a_column():
    # The note's job is to tell an author which columns to declare on output_schema, and
    # `_list_added_columns` is the code that decides that set — read here rather than
    # inferred from a name pattern, so a column-adding field that breaks the `*_column`
    # convention still counts. Pinned in BOTH directions: a field added there and not
    # taught here leaves the note describing a smaller output than the runtime produces
    # (the class of falsehood this note has already carried once), and a `queue.<field>`
    # the note names after `QueueConfig` dropped it no longer resolves.
    from app.models.stages.human_review_queue import QueueConfig, find_added_columns

    queue = QueueConfig(
        reviewed_columns={"src": "reviewed_src"}, verdict_column="v",
        reviewer_column="r", reviewed_at_column="at", review_notes_column="n",
    )
    # `find_added_columns` labels a reviewed target `queue.reviewed_columns['src']`;
    # the field itself is the part before the subscript.
    adding_fields = {field.split("[")[0] for field, _ in find_added_columns(queue)}
    mentioned = {f"queue.{name}" for name in re.findall(
        r"queue\.(\w+)", NODE_TYPES["human_review_queue"].notes)}

    assert adding_fields <= mentioned, adding_fields - mentioned
    assert mentioned <= {f"queue.{name}" for name in QueueConfig.model_fields}, mentioned


def test_summary_budget_note_states_the_limit_the_write_path_refuses_on():
    # the note tells an author to fit the behaviour in `summary` plus `corner_cases`;
    # naming a number stage_edit does not refuse above would send them to a wrong budget
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
        for stage_type in CODE_CARRYING_TYPES:
            assert f"`{stage_type}`" in prompt, stage_type


def test_no_type_note_still_carries_the_shared_text():
    # Guards the hoist: a note that re-absorbs the paragraph duplicates it again.
    for stage_type, spec in NODE_TYPES.items():
        assert CODE_SUMMARY_CONTRACT_NOTE not in spec.notes, stage_type
        assert CODE_CORNER_CASES_CONTRACT_NOTE not in spec.notes, stage_type


def _authoring_prompts():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
    from app.mcp.server import INSTRUCTIONS

    return (EDITING_SYSTEM_PROMPT, INSTRUCTIONS)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_publish_note_names_the_trace_link_helper():
    note = NODE_TYPES["publish"].notes
    assert note, "publish must carry a `notes` explanation"
    # the authoring agent has to know the keyword to declare and the call to make
    assert "trace_links" in note
    assert "build_row_trace_url" in note


def test_publish_note_reaches_the_editing_agent_prompt():
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    note = NODE_TYPES["publish"].notes
    assert note in EDITING_SYSTEM_PROMPT
