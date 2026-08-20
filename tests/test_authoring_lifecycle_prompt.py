"""Every prompt that can invent schema or artifacts carries the gated authoring
lifecycle (app/models/authoring_lifecycle_note.py) — one step per CompilerPhase,
each with its gate. The workflow-authoring surfaces get the full lifecycle; the
data-model prompt gets the intermediate-concepts slice."""
from __future__ import annotations

from app.models.authoring_lifecycle_note import (
    AUTHORING_LIFECYCLE_GUIDANCE,
    INTERMEDIATE_CONCEPTS_NOTE,
    CompilerPhase,
)
from app.models.stages.stage_types import AUTHORABLE_TYPES


def test_editing_prompt_carries_the_full_lifecycle() -> None:
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert AUTHORING_LIFECYCLE_GUIDANCE in EDITING_SYSTEM_PROMPT


def test_mcp_instructions_carry_the_full_lifecycle() -> None:
    from app.mcp.server import INSTRUCTIONS

    assert AUTHORING_LIFECYCLE_GUIDANCE in INSTRUCTIONS


def test_data_model_prompt_carries_the_intermediate_concepts_slice() -> None:
    from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT

    assert INTERMEDIATE_CONCEPTS_NOTE in DATA_MODEL_SYSTEM_PROMPT


def test_lifecycle_embeds_the_slice_verbatim() -> None:
    assert INTERMEDIATE_CONCEPTS_NOTE in AUTHORING_LIFECYCLE_GUIDANCE


def test_lifecycle_heads_a_step_with_every_phase_name() -> None:
    # The prose and CompilerPhase are one vocabulary, or this fails.
    for phase in CompilerPhase:
        assert phase.name in AUTHORING_LIFECYCLE_GUIDANCE, phase


def test_lifecycle_states_the_steps_and_their_gates() -> None:
    assert "RESEARCH FIRST" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "AGREE THE WORDS BEFORE THE PLAN" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "PLAN, AND ASK QUESTIONS" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "major stages" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "sign-off gate" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "BUILD TO THE SIGNED-OFF PLAN" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "never silently into the output" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "`compiler_notes`" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "SMOKE BEFORE FULL" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "row limits" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "full-run budget" in AUTHORING_LIFECYCLE_GUIDANCE


def test_the_words_are_agreed_before_the_plan_is_written() -> None:
    # Why TERMS is its own phase, and what an agent may put in it.
    assert "Ask, never invent" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "not in the document" in AUTHORING_LIFECYCLE_GUIDANCE


def test_a_verb_may_not_restate_a_word_the_app_already_spends() -> None:
    # The examples are the app's real stage types, so a renamed type fails here.
    for stage_type in ("aggregate", "starlark_filter_rows", "enrich"):
        assert stage_type in AUTHORING_LIFECYCLE_GUIDANCE, stage_type
        assert stage_type in AUTHORABLE_TYPES, stage_type


def test_stage_tests_are_built_before_the_run_not_after() -> None:
    # BUILD owns the example tests; after the run is too late.
    assert "example tests pass here, not after the run" in AUTHORING_LIFECYCLE_GUIDANCE


def test_the_guide_is_written_after_the_smoke_run() -> None:
    # Why TEST_RUN_REVIEW is its own phase: the run rewrites what the guide covers.
    assert "WRITE THE GUIDE LAST" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "the run has since changed" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "warnings you did not clear" in AUTHORING_LIFECYCLE_GUIDANCE


def test_editing_prompt_tells_the_agent_when_to_write_the_guide() -> None:
    # The lifecycle names the phase; the prompt has to name the tool call.
    from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT

    assert "write_review_guide" in EDITING_SYSTEM_PROMPT
    assert "after the smoke run, never straight off save_version" in EDITING_SYSTEM_PROMPT


def test_research_may_build_a_prototype_without_skipping_the_gates() -> None:
    assert "prototype pipeline" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "IS research" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "how the data shapes out" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "committal, not exploration" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "scaffolding" in AUTHORING_LIFECYCLE_GUIDANCE



def test_slice_states_the_reader_and_the_why() -> None:
    assert "the reason it is needed" in INTERMEDIATE_CONCEPTS_NOTE
    assert "goes in the plan" in INTERMEDIATE_CONCEPTS_NOTE
    assert "THEIR OWN data" in INTERMEDIATE_CONCEPTS_NOTE
