"""Builds the code-blind agent that authors StageTest cases for one python transform stage.

The stage's function code and any existing tests are excluded by construction, so
expected outputs cannot be anchored on an implementation. The submitted suite comes back
through a callback; persisting it is the caller's job."""
from __future__ import annotations

from typing import Callable

from pydantic import BaseModel

from app.compiler.stage_tests_prompt import STAGE_TESTS_SYSTEM_PROMPT
from app.compiler.turn_failure import persist_generation_failure
from app.core.agent.agent import Agent
from app.core.agent.store import open_session_store
from app.core.agent.turns import default_turn_manager
from app.models import Stage
from app.models.stage_base import find_stage_test_class
from app.models.stages.stage_tests import build_stage_tests_model


def start_stage_test_generation_agent(
    *,
    document: str,
    stage: Stage,
    project_id: str,
    model: str,
    on_answer: Callable[[BaseModel | None], None],
) -> str:
    """Start the stage-test generator as a LIVE chat turn and return the session id.

    The session is HIDDEN and VIEW-ONLY (`agent_id=None`, `context["hidden"] = True`):
    it streams on the shared TurnManager like a workflow/data-model generation turn, but
    is a background generation the project chat index does not list — there is no one to
    reply to it. When the turn finishes, `on_answer` is called with the submitted suite
    (a `StageTestSuite` bound to `stage`'s type/inputs) — or None if none was submitted.
    If `on_answer` raises (e.g. the finisher's patch is refused), the error is appended to
    the session's persisted transcript as an assistant message BEFORE it propagates — so
    the failure is visible on session reload, not only to a client watching the live turn.
    Must be called from the server event loop (it starts a turn there)."""
    store = open_session_store()
    session_id = store.create(
        title=f"Generation · stage tests · {stage.id}",
        agent_id=None,  # view-only: rendered + streamed, but no agent to continue it
        context={
            "project_id": project_id,
            "phase": "stage_tests",
            "stage_id": stage.id,
            "hidden": True,
        },
    )
    agent = build_stage_test_generator(document, stage, model=model)
    # Show the framing prompt as the user's message so the live view doesn't lose it.
    store.set_pending_user(session_id, agent.task)

    async def _on_done() -> None:
        try:
            on_answer(agent.answer)
        except Exception as exc:
            persist_generation_failure(store, session_id, exc)
            raise

    default_turn_manager().start(
        engine=agent.build_engine(),
        store=store,
        session_id=session_id,
        prompt=agent.task,
        on_done=_on_done,
    )
    return session_id


def build_stage_test_generator(
    document: str, stage: Stage, *, model: str = "sonnet"
) -> Agent[BaseModel]:
    """The test-generation agent for one stage: target schema is the stage-bound
    suite model, so a malformed suite bounces inside the agent loop."""
    if not stage.CARRIES_RUNNABLE_TESTS:
        raise ValueError(
            f"tests can only be generated for stage types that can run them, "
            f"not `{stage.type}`"
        )
    task = render_generation_task(document, stage)  # raises if there is no output schema
    assert stage.output_schema is not None
    return Agent(
        system_prompt=STAGE_TESTS_SYSTEM_PROMPT,
        target_schema=build_stage_tests_model(
            find_stage_test_class(type(stage)),
            {ref.id: ref.table_schema for ref in stage.inputs},
            stage.output_schema,
        ),
        task=task,
        model=model,
    )


def render_generation_task(document: str, stage: Stage) -> str:
    """The generator's task string: the stage's DESCRIPTION — its `summary` and its
    `corner_cases` — plus its identity and schemas.

    The description, not the methodology document, is deliberately the whole
    input. The examples exist to answer one question: does this step's code do what
    its description says? An agent that had read the methodology could write a
    correct-looking case the description never implies, and the suite would then
    certify the methodology rather than the description a reviewer actually reads.
    So the generator is shown exactly what the reviewer is shown, and nothing else —
    not the code, not the document, not existing examples.

    `document` is accepted and unused for that reason; it stays in the signature
    because the caller holds it and removing it would invite passing it back in.

    Raises ValueError when the stage has no summary: there is no description to
    work from, and a suite written from something else would make the panel's
    "checked against the code" claim untrue."""
    summary = _authored_summary(stage)
    if not summary:
        raise ValueError(
            f"stage `{stage.id}` has no summary — examples are written from a step's "
            f"description, so write one first (there is nothing to check the code against)"
        )
    if stage.output_schema is None:
        raise ValueError(
            f"stage `{stage.id}` has no output schema — tests need one to state expected rows"
        )
    inputs = "\n\n".join(
        f"Input `{ref.id}` schema:\n{ref.table_schema.to_prompt()}"
        for ref in stage.inputs
    )
    return (
        f"----- DESCRIPTION OF `{stage.id}` -----\n{summary}\n"
        f"{_render_corner_cases(stage)}"
        f"----- END DESCRIPTION -----\n\n"
        f"Write examples for stage `{stage.id}` ({stage.type}): {stage.name}\n\n"
        f"{inputs}\n\n"
        f"Output schema:\n{stage.output_schema.to_prompt()}"
    )


def _authored_summary(stage: Stage) -> str | None:
    """The stage's plain-language summary, off whichever authored-code block it
    carries."""
    block = stage.find_authored_code_block()
    return block.summary if block is not None else None


def _render_corner_cases(stage: Stage) -> str:
    """The declared corner cases, each an input and the outcome it must produce.
    Empty string when none are declared — the generator still has to find edge cases
    itself, it just has none stated for it."""
    block = stage.find_authored_code_block()
    if block is None or not block.corner_cases:
        return ""
    cases = "\n".join(
        f"- {case.case} -> {case.expected}" for case in block.corner_cases
    )
    return f"\nStated corner cases (each MUST become at least one example):\n{cases}\n"
