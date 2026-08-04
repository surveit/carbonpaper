"""Both authoring prompts carry OBSERVED_ENUM_GUIDANCE (app/tools/tool_specs.py):
consult list_distinct_values before declaring a categorical column's schema, then
decide per column — one worked example frozen as an enum, one left open — and keep
guard code for what a declaration cannot state. Mirrors tests/test_node_type_notes.py."""
from __future__ import annotations

from app.models.observation import DEFAULT_MAX_DISTINCT_VALUES
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


def test_guidance_warns_that_a_truncated_list_is_not_the_vocabulary() -> None:
    assert "TRUNCATED" in OBSERVED_ENUM_GUIDANCE
    assert "max_values" in OBSERVED_ENUM_GUIDANCE


def test_tool_description_states_when_the_values_are_complete() -> None:
    description = TOOL_SPECS["list_distinct_values"].description
    assert "COMPLETE" in description
    assert "distinct_count == len(values)" in description
    assert "truncated" in description
    assert "max_values" in description
    assert str(DEFAULT_MAX_DISTINCT_VALUES) in description
    assert "Fails loudly" in description
