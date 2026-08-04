"""Every prompt that can invent schema or artifacts carries
PROPOSED_ADDITIONS_GUIDANCE (app/models/proposed_additions_note.py): an addition
untraceable to the methodology prose or an explicit request is surfaced for
agreement — in the plan the human reviews and in the stage's compiler_notes —
never silently added. Mirrors tests/test_node_type_notes.py."""
from __future__ import annotations

from app.models.proposed_additions_note import PROPOSED_ADDITIONS_GUIDANCE


def test_data_model_prompt_carries_the_proposed_additions_guidance() -> None:
    from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT

    assert PROPOSED_ADDITIONS_GUIDANCE in DATA_MODEL_SYSTEM_PROMPT


def test_editing_prompt_carries_the_proposed_additions_guidance() -> None:
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert PROPOSED_ADDITIONS_GUIDANCE in EDITING_SYSTEM_PROMPT


def test_mcp_instructions_carry_the_proposed_additions_guidance() -> None:
    from app.mcp.server import INSTRUCTIONS

    assert PROPOSED_ADDITIONS_GUIDANCE in INSTRUCTIONS


def test_guidance_states_the_rule_the_reader_and_the_example() -> None:
    # The rule (traceability), both surfacing channels, the reader whose trust is
    # at stake, and one worked example spelling out the wrong move and the right
    # move — the guidance is only guidance while all four survive edits.
    assert "traceable to the methodology" in PROPOSED_ADDITIONS_GUIDANCE
    assert "PROPOSED ADDITION" in PROPOSED_ADDITIONS_GUIDANCE
    assert "`compiler_notes`" in PROPOSED_ADDITIONS_GUIDANCE
    assert "methodology's author" in PROPOSED_ADDITIONS_GUIDANCE
    assert "`filing_row_id`" in PROPOSED_ADDITIONS_GUIDANCE
    assert "silently emit" in PROPOSED_ADDITIONS_GUIDANCE
    assert "may I add" in PROPOSED_ADDITIONS_GUIDANCE
