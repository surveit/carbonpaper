"""Compiler bridge for stage-test derivation: builds the code-blind agent
that authors StageTest cases for one python transform stage, and runs it as a
live chat turn on the app.core.agent spine (`start_stage_test_derivation_agent`).

The deriver's task is assembled from the methodology document and the
stage's declared identity and schemas ONLY — the stage's function code and
any existing tests are excluded by construction (they are never rendered
into the task), so expected outputs cannot be anchored on an
implementation. The submitted suite is handed back through a callback;
persisting it (via app.services.stage_edit) is the caller's job."""
from __future__ import annotations

from typing import Callable

from pydantic import BaseModel

from app.compiler.stage_tests_prompt import STAGE_TESTS_SYSTEM_PROMPT
from app.core.agent.agent import Agent
from app.core.agent.store import SessionStore, open_session_store
from app.core.agent.turns import default_turn_manager
from app.core.models import Stage
from app.core.models.stages.stage_tests import (
    STAGE_TEST_TYPES,
    build_stage_tests_model,
)


def start_stage_test_derivation_agent(
    *,
    document: str,
    stage: Stage,
    project_id: str,
    model: str,
    on_answer: Callable[[BaseModel | None], None],
) -> str:
    """Start the stage-test deriver as a LIVE chat turn and return the session id.

    The session is HIDDEN and VIEW-ONLY (`agent_id=None`, `context["hidden"] = True`):
    it streams on the shared TurnManager like a workflow/data-model generation turn, but
    is a background derivation the project chat index does not list — there is no one to
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
    agent = build_stage_test_deriver(document, stage, model=model)
    # Show the framing prompt as the user's message so the live view doesn't lose it.
    store.set_pending_user(session_id, agent.task)

    async def _on_done() -> None:
        try:
            on_answer(agent.answer)
        except Exception as exc:
            _persist_derivation_failure(store, session_id, exc)
            raise

    default_turn_manager().start(
        engine=agent.build_engine(),
        store=store,
        session_id=session_id,
        prompt=agent.task,
        on_done=_on_done,
    )
    return session_id


def _persist_derivation_failure(store: SessionStore, session_id: str, error: Exception) -> None:
    """Append a synthetic assistant message reporting `error` to `session_id`'s stored
    transcript, so the failure survives past the in-memory turn buffer: a client that
    was not watching the live turn still sees it on reload. Runs before the caller
    re-raises `error`."""
    messages = list(store.load(session_id)["messages"])
    messages.append({
        "role": "assistant",
        "parts": [{"type": "text", "text": f"derivation failed: {error}"}],
    })
    store.save_messages(session_id, messages)


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
