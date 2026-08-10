"""Compile a methodology document into a DATA MODEL (a SchemaLibrary).

The agent SUBMITS the data model through the submit_answer tool (validated against
`SchemaLibrary`) rather than emitting free-text JSON. The result is handed back through
a callback; persisting it is the caller's job.
"""
from __future__ import annotations

from typing import Callable

from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT
from app.core.agent.agent import Agent
from app.core.agent.store import open_session_store
from app.core.agent.turns import default_turn_manager
from app.models.named_schemas import SchemaLibrary


def start_data_model_generation_agent(
    *,
    document: str,
    project_name: str,
    model: str,
    on_answer: Callable[[SchemaLibrary | None], None],
) -> str:
    """Must be called from the server event loop — it starts a turn there."""
    store = open_session_store()
    session_id = store.create(
        title=f"Generation · data model · {project_name}",
        agent_id=None,  # view-only: rendered + streamed, but no agent to continue it
        context={"project_id": project_name, "phase": "data_model"},
    )
    agent = build_data_model_agent(document, model=model)
    # Show the framing prompt as the user's message so the live view doesn't lose it.
    store.set_pending_user(session_id, agent.task)

    async def _on_done() -> None:
        on_answer(agent.answer)

    default_turn_manager().start(
        engine=agent.build_engine(),
        store=store,
        session_id=session_id,
        prompt=agent.task,
        on_done=_on_done,
    )
    return session_id


def build_data_model_agent(document: str, *, model: str = "sonnet") -> Agent[SchemaLibrary]:
    return Agent(
        system_prompt=DATA_MODEL_SYSTEM_PROMPT,
        target_schema=SchemaLibrary,
        task=_frame(document),
        model=model,
    )


def _frame(document: str) -> str:
    """Delimited so the agent treats the document as source, not as instructions."""
    return (
        "Here is the methodology document. Author its data model — the named schemas — "
        "and submit it with submit_answer.\n\n"
        "----- DOCUMENT -----\n"
        f"{document}\n"
        "----- END DOCUMENT -----"
    )
