"""Both authoring prompts carry OBSERVED_ENUM_GUIDANCE (app/tools/tool_specs.py):
run a workflow test, read a stage's real output with list_distinct_values, then decide
per column — one worked example frozen as an enum, one left open — plus the two stage
types whose observed values cannot corroborate a declaration."""
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


def test_guidance_points_at_a_run_of_the_working_copy_as_the_evidence() -> None:
    # Nothing is observable until a run has produced it, and requiring a saved
    # version first would push every enum decision to a second pass.
    assert "run_workflow_test(project_id, use_working_copy=True)" in OBSERVED_ENUM_GUIDANCE
    assert "run_id" in OBSERVED_ENUM_GUIDANCE
    assert "row_count" in OBSERVED_ENUM_GUIDANCE


def test_guidance_names_the_two_columns_observation_cannot_corroborate() -> None:
    # Both would otherwise read as confirmation: the LLM stage returns a subset of
    # the declaration compiled into its own reply model, and the queue's decisions
    # in a test run are the auto-approval, not the data.
    assert "`llm_transform`" in OBSERVED_ENUM_GUIDANCE
    assert "reply model" in OBSERVED_ENUM_GUIDANCE
    assert "`human_review_queue`" in OBSERVED_ENUM_GUIDANCE
    assert "auto-approves every row in a TEST run" in OBSERVED_ENUM_GUIDANCE


def test_tool_description_states_when_the_values_are_complete() -> None:
    description = TOOL_SPECS["list_distinct_values"].description
    assert "COMPLETE" in description
    assert "distinct_count == len(values)" in description
    assert "truncated" in description
    assert "max_values" in description
    assert str(DEFAULT_MAX_DISTINCT_VALUES) in description
    assert "Fails loudly" in description
