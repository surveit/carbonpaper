"""Both authoring prompts carry OBSERVED_ENUM_GUIDANCE (app/tools/tool_specs.py):
consult list_distinct_values before declaring a categorical column's schema, then
decide per column — one worked example frozen as an enum, one left open — and keep
guard code for what a declaration cannot state. Mirrors tests/test_node_type_notes.py."""
from __future__ import annotations

from app.tools.tool_specs import OBSERVED_ENUM_GUIDANCE, TOOL_SPECS


def test_editing_prompt_carries_the_observed_enum_guidance() -> None:
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert OBSERVED_ENUM_GUIDANCE in EDITING_SYSTEM_PROMPT


def test_mcp_instructions_carry_the_observed_enum_guidance() -> None:
    from app.mcp.server import INSTRUCTIONS

    assert OBSERVED_ENUM_GUIDANCE in INSTRUCTIONS


def test_guidance_teaches_the_decision_not_a_rule() -> None:
    # One worked example in each direction — a set worth freezing, and a small
    # but open set left alone — plus the tool to consult and the guard-code line.
    assert "list_distinct_values" in OBSERVED_ENUM_GUIDANCE
    assert "`permit_status`" in OBSERVED_ENUM_GUIDANCE
    assert '["filed", "granted", "denied"]' in OBSERVED_ENUM_GUIDANCE
    assert "`city`" in OBSERVED_ENUM_GUIDANCE
    assert "Leave it a bare `str`" in OBSERVED_ENUM_GUIDANCE
    assert "never replaces guard code" in OBSERVED_ENUM_GUIDANCE


def test_tool_description_states_the_cap_semantics() -> None:
    description = TOOL_SPECS["list_distinct_values"].description
    assert "COMPLETE" in description
    assert "sample" in description
    assert "Fails loudly" in description
