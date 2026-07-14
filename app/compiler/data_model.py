"""Compile a methodology document into a DATA MODEL (a SchemaLibrary).

The sibling of `app.compiler.compiler.compile_methodology` (prose → workflow stages):
this is prose → named schemas. It builds an `app.agent.Agent` whose target schema is
`SchemaLibrary`, so the agent SUBMITS the data model through the submit_answer tool
(validated against SchemaLibrary) rather than emitting free-text JSON.

`start_data_model_turn` runs that agent as a LIVE chat turn on the app.agent spine, and is
the bridge onto it: app.compiler is an allowed importer of app.agent, so the higher-level
orchestration in app.services (generation) delegates here rather than reaching into the
spine itself. The submitted SchemaLibrary is handed back through a callback; persisting it
is the caller's job.
"""
from __future__ import annotations

from typing import Callable

from app.agent.agent import Agent
from app.agent.store import open_session_store
from app.agent.turns import default_turn_manager
from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT
from app.models.named_schemas import SchemaLibrary


def start_data_model_turn(
    *,
    document: str,
    project_name: str,
    model: str,
    on_answer: Callable[[SchemaLibrary | None], None],
) -> str:
    """Start the data-model agent as a LIVE chat turn and return the session id.

    Creates a view-only session (the framing prompt shown as the user's message) and
    streams the agent on the shared TurnManager, so the run is watchable at /chat/<sid>
    while it happens and persists when it ends. When the turn finishes, `on_answer` is
    called with the submitted SchemaLibrary — or None if none was submitted — so the
    caller persists the result or handles the failure. Must be called from the server
    event loop (it starts a turn there)."""
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
    """Configure the data-model agent for `document`: it authors the named-schema data
    model and SUBMITS it as a SchemaLibrary via submit_answer. Read `.answer` after the
    run/turn for the validated library (None if nothing valid was submitted). `.run()`
    drives it headlessly; driving `.build_engine()` through the TurnManager runs it as a
    live turn (see start_data_model_turn)."""
    return Agent(
        system_prompt=DATA_MODEL_SYSTEM_PROMPT,
        target_schema=SchemaLibrary,
        task=_frame(document),
        model=model,
    )


def _frame(document: str) -> str:
    """Frame the methodology document as the material to model, delimited so the agent
    treats it as source, not instructions."""
    return (
        "Here is the methodology document. Author its data model — the named schemas — "
        "and submit it with submit_answer.\n\n"
        "----- DOCUMENT -----\n"
        f"{document}\n"
        "----- END DOCUMENT -----"
    )
