"""Both authoring prompts carry OBSERVED_ENUM_GUIDANCE (app/tools/tool_specs.py):
run a workflow test, read a stage's real output with list_distinct_values, then decide
on the two questions — constrained generation, discrete consumption — never on how many
distinct values came back. Plus the two stage types observation cannot corroborate."""
from __future__ import annotations

import asyncio
import re

from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.models.observation import DEFAULT_MAX_DISTINCT_VALUES
from app.tools.tool_specs import OBSERVED_ENUM_GUIDANCE, TOOL_SPECS


def find_mcp_tool_names() -> set[str]:
    from app.mcp.server import mcp

    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def find_editing_tool_names() -> set[str]:
    from app.tools.editing import EditingContext, make_editing_tools

    return {spec.name for spec in make_editing_tools(EditingContext(project_id="any"))}


def find_tools_the_guidance_names(registered: set[str]) -> set[str]:
    return set(re.findall(r"[a-z_][a-z0-9_]*", OBSERVED_ENUM_GUIDANCE)) & registered


def test_editing_prompt_carries_the_observed_enum_guidance() -> None:
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert OBSERVED_ENUM_GUIDANCE in EDITING_SYSTEM_PROMPT


def test_mcp_instructions_carry_the_observed_enum_guidance() -> None:
    from app.mcp.server import INSTRUCTIONS

    assert OBSERVED_ENUM_GUIDANCE in INSTRUCTIONS


def test_every_tool_the_guidance_names_exists_on_both_surfaces() -> None:
    # One prose block embedded in two prompts can only stay honest if every tool it
    # tells the reader to call is registered on both. run_workflow_test was MCP-only
    # while the guidance taught it, so the editing agent read an uncallable
    # instruction.
    on_mcp, on_editing = find_mcp_tool_names(), find_editing_tool_names()
    named = find_tools_the_guidance_names(on_mcp | on_editing)
    assert {"list_distinct_values", "run_workflow_test", "edit_stage"} <= named
    assert named <= on_mcp, f"guidance names tools the MCP server lacks: {sorted(named - on_mcp)}"
    assert named <= on_editing, (
        "guidance names tools the editing agent lacks: "
        f"{sorted(named - on_editing)} — it registers {sorted(on_editing)}"
    )


def test_guidance_decides_on_generation_and_consumption_not_on_the_count() -> None:
    # The count is what the old wording keyed on, and it is the wrong question: a
    # closed set can be huge and an open one tiny.
    assert "COUNT is evidence, never the criterion" in OBSERVED_ENUM_GUIDANCE
    assert "GENERATION constrained to a discrete set" in OBSERVED_ENUM_GUIDANCE
    assert "thousands of values and still be closed" in OBSERVED_ENUM_GUIDANCE
    assert "three\n   and still be open" in OBSERVED_ENUM_GUIDANCE
    assert "list_distinct_values" in OBSERVED_ENUM_GUIDANCE


def test_guidance_makes_the_enum_mandatory_when_a_later_stage_consumes_the_set() -> None:
    # A downstream switch or a join into reference data makes the vocabulary
    # load-bearing: without the declaration an unlisted value is silently wrong.
    assert "the enum is MANDATORY whatever was\n   observed" in OBSERVED_ENUM_GUIDANCE
    assert "else-branch or joins to" in OBSERVED_ENUM_GUIDANCE
    assert "loud failure" in OBSERVED_ENUM_GUIDANCE


def test_guidance_works_one_example_for_each_question() -> None:
    assert "- By generation: `filing_type`" in OBSERVED_ENUM_GUIDANCE
    assert "fixed list on the source form" in OBSERVED_ENUM_GUIDANCE
    assert "- By consumption: `country_code`" in OBSERVED_ENUM_GUIDANCE
    assert "reference table" in OBSERVED_ENUM_GUIDANCE
    assert "never replaces guard code" in OBSERVED_ENUM_GUIDANCE


def test_guidance_warns_that_a_truncated_list_is_not_the_vocabulary() -> None:
    assert "TRUNCATED" in OBSERVED_ENUM_GUIDANCE
    assert "max_values" in OBSERVED_ENUM_GUIDANCE
    assert "is a SAMPLE, not the set" in OBSERVED_ENUM_GUIDANCE


def test_guidance_scopes_the_run_to_the_input_stage_to_see_a_whole_input_column() -> None:
    # An unscoped workflow test injects a limit-row slice of every input, so the
    # vocabulary it shows for an input column is a sample. Scoping the run to that
    # input stage executes it over the whole bound file instead.
    assert "run_workflow_test(project_id, use_working_copy=True)" in OBSERVED_ENUM_GUIDANCE
    assert 'only_stages=["<the input stage id>"]' in OBSERVED_ENUM_GUIDANCE
    assert "whole\nbound file" in OBSERVED_ENUM_GUIDANCE
    assert "`limit`-row slice" in OBSERVED_ENUM_GUIDANCE
    assert "run_id" in OBSERVED_ENUM_GUIDANCE
    assert "row_count is the rows that stage's output actually held" in OBSERVED_ENUM_GUIDANCE


def test_guidance_names_the_observation_run_as_the_lifecycle_research_step() -> None:
    # Both prompts also carry the gated lifecycle, which puts a run behind a
    # signed-off plan. Observing to decide a schema happens before any plan
    # exists, and reads one input whole — so the guidance says which gate it is
    # under, or the two blocks read as competing procedures.
    assert "IS research, so it precedes the signed-off plan" in OBSERVED_ENUM_GUIDANCE
    assert "the smoke gate does not govern it" in OBSERVED_ENUM_GUIDANCE


def test_the_two_questions_split_across_the_lifecycle_gate() -> None:
    # Question 1 is a claim about the world, which research confirms; question 2
    # is a design commitment the human signs off on. Without the split the enum
    # reads as a private decision the build makes on its own.
    assert "research settles it" in OBSERVED_ENUM_GUIDANCE
    assert "observed vocabulary is what confirms it" in OBSERVED_ENUM_GUIDANCE
    assert "design commitment, so it goes in the PLAN" in OBSERVED_ENUM_GUIDANCE
    assert "signs off on the enum,\n   not just the stage list" in OBSERVED_ENUM_GUIDANCE


def test_the_enum_guidance_continues_the_lifecycle_on_both_surfaces() -> None:
    # One procedure, not two adjacent ones: observation belongs to the research
    # phase, so the guidance follows the lifecycle with no section opening between.
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
    from app.mcp.server import INSTRUCTIONS

    for prompt in (EDITING_SYSTEM_PROMPT, INSTRUCTIONS):
        lifecycle_at = prompt.index(AUTHORING_LIFECYCLE_GUIDANCE)
        guidance_at = prompt.index(OBSERVED_ENUM_GUIDANCE)
        assert lifecycle_at < guidance_at
        between = prompt[lifecycle_at + len(AUTHORING_LIFECYCLE_GUIDANCE) : guidance_at]
        assert "\n#" not in between, f"a section opens between the two: {between!r}"


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


def test_workflow_test_description_says_which_stages_a_scoped_run_reads_whole() -> None:
    description = TOOL_SPECS["run_workflow_test"].description
    assert "only_stages" in description
    assert "reads its WHOLE bound file" in description
    assert "`limit`/`offset` do not apply" in description
