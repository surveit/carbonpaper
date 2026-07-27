from __future__ import annotations

import re

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
    """The note tells an author how to filter on the verdict column. That
    guidance is only correct while the quoted strings it names are exactly the
    ones a queue stage's verdict column can hold — including `skipped`, which
    the handler writes for a row the filter passed through unreviewed and which
    is what makes a downstream filter safe without reasoning about a missing
    value. Pinned in BOTH directions against the enum: a member added or renamed
    there and not taught here fails, and so does a verdict the note still names
    after the enum stopped emitting it."""
    from app.models import ReviewVerdict

    quoted = set(re.findall(r'"([a-z_]+)"', m.HUMAN_REVIEW_QUEUE_CONTRACT_NOTE))
    assert quoted == {verdict.value for verdict in ReviewVerdict}


def test_hrq_note_names_every_queue_field_that_adds_a_column():
    """The note's job is to tell an author which columns to declare on
    output_schema. `QueueConfig` is what decides that set: `reviewed_columns`
    plus every `*_column` field. A field added there and not taught here would
    leave the note describing a smaller output than the runtime produces —
    the class of falsehood this note has already carried once."""
    from app.models import QueueConfig

    naming_fields = {
        name for name in QueueConfig.model_fields
        if name == "reviewed_columns" or name.endswith("_column")
    }
    for field in sorted(naming_fields):
        assert f"queue.{field}" in m.HUMAN_REVIEW_QUEUE_CONTRACT_NOTE, field


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
