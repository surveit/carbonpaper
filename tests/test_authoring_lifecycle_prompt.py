"""Every prompt that can invent schema or artifacts carries the gated authoring
lifecycle (app/models/authoring_lifecycle_note.py) — research, a plan the user
signs off (major stages + every intermediate concept with its why), the build,
a smoke run before the full one. The workflow-authoring surfaces get the full
lifecycle; the data-model prompt gets the intermediate-concepts slice."""
from __future__ import annotations

from app.models.authoring_lifecycle_note import (
    AUTHORING_LIFECYCLE_GUIDANCE,
    INTERMEDIATE_CONCEPTS_NOTE,
)


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
    # One source of truth: the full lifecycle carries the same slice the
    # data-model prompt embeds, so the two cannot drift apart.
    assert INTERMEDIATE_CONCEPTS_NOTE in AUTHORING_LIFECYCLE_GUIDANCE


def test_lifecycle_states_the_steps_and_their_gates() -> None:
    # The four steps, each gate's substance, and the on-artifact trace — the
    # lifecycle is only a lifecycle while every gate survives edits.
    assert "RESEARCH FIRST" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "PLAN, AND ASK QUESTIONS" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "major stages" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "sign-off gate" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "BUILD TO THE SIGNED-OFF PLAN" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "never silently into the output" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "`compiler_notes`" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "SMOKE BEFORE FULL" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "row limits" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "full-run budget" in AUTHORING_LIFECYCLE_GUIDANCE


def test_research_may_build_a_prototype_without_skipping_the_gates() -> None:
    # Prototyping over limited rows is research — to learn how the data shapes
    # out through the stages — but the gates govern committal, not exploration:
    # the prototype is scaffolding, never the deliverable.
    assert "prototype pipeline" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "IS research" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "how the data shapes out" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "committal, not exploration" in AUTHORING_LIFECYCLE_GUIDANCE
    assert "scaffolding" in AUTHORING_LIFECYCLE_GUIDANCE



def test_slice_states_the_reader_and_the_why() -> None:
    # The rule (a concept carries its reason), where it is headed (the plan),
    # and the reader checking it against their own data.
    assert "the reason it is needed" in INTERMEDIATE_CONCEPTS_NOTE
    assert "goes in the plan" in INTERMEDIATE_CONCEPTS_NOTE
    assert "THEIR OWN data" in INTERMEDIATE_CONCEPTS_NOTE
