"""Compiler bridge for stage-test derivation: builds the code-blind agent
that authors StageTest cases for one python transform stage.

The deriver's task is assembled from the methodology document and the
stage's declared identity and schemas ONLY — the stage's function code and
any existing tests are excluded by construction (they are never rendered
into the task), so expected outputs cannot be anchored on an
implementation."""
from __future__ import annotations

from pydantic import BaseModel

from app.compiler.stage_tests_prompt import STAGE_TESTS_SYSTEM_PROMPT
from app.core.agent.agent import Agent
from app.core.models import Stage
from app.core.models.stages.stage_tests import (
    STAGE_TEST_TYPES,
    build_stage_tests_model,
)


def build_stage_test_deriver(
    document: str, stage: Stage, *, model: str = "sonnet"
) -> Agent[BaseModel]:
    """The derivation agent for one stage: target schema is the stage-bound
    suite model, so a malformed suite bounces inside the agent loop."""
    if stage.type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"tests can only be derived for python transforms, not `{stage.type}`"
        )
    return Agent(
        system_prompt=STAGE_TESTS_SYSTEM_PROMPT,
        target_schema=build_stage_tests_model(
            stage.type, [ref.id for ref in stage.inputs]
        ),
        task=render_derivation_task(document, stage),
        model=model,
    )


def render_derivation_task(document: str, stage: Stage) -> str:
    """The deriver's task string: methodology + stage identity + schemas.
    Deliberately renders nothing else from the stage — not the function
    block, not existing tests. The agent correlates the stage id/name
    against the document itself to learn what the stage must do."""
    inputs = "\n\n".join(
        f"Input `{ref.id}` schema:\n{ref.table_schema.to_prompt()}"
        if ref.table_schema is not None
        else f"Input `{ref.id}` (no schema declared)"
        for ref in stage.inputs
    )
    assert stage.output_schema is not None  # python transforms declare their output schema
    return (
        f"----- METHODOLOGY DOCUMENT -----\n{document}\n"
        f"----- END DOCUMENT -----\n\n"
        f"Derive tests for stage `{stage.id}` ({stage.type}): {stage.name}\n\n"
        f"{inputs}\n\n"
        f"Output schema:\n{stage.output_schema.to_prompt()}"
    )
