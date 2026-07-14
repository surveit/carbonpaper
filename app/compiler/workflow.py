"""Compile a methodology document into a WORKFLOW (a validated `Workflow`).

The sibling of `app.compiler.data_model` (prose → named schemas): this is prose → typed
stages. It builds an `app.agent.Agent` whose target schema is `Workflow`, so the agent SUBMITS
the workflow through the submit_answer tool — validated against `Workflow` (each stage's own
invariants + the cross-stage graph checks) — and a schema-invalid draft comes back as a tool
error the agent corrects IN THE SAME LOOP.

`start_workflow_turn` runs that agent as a LIVE chat turn on the app.agent spine, and is the
bridge onto it: app.compiler is an allowed importer of app.agent, so the orchestration in
app.services (generation) delegates here rather than reaching into the spine itself. When a
`data_model` (the approved SchemaLibrary) is given, its named schemas ground the task as the
nouns the workflow imports and generates. The submitted Workflow is handed back through a
callback; persisting it is the caller's job.
"""
from __future__ import annotations

import json
from typing import Callable

from app.agent.agent import Agent
from app.agent.store import open_session_store
from app.agent.turns import default_turn_manager
from app.compiler.workflow_prompt import WORKFLOW_SYSTEM_PROMPT
from app.models.named_schemas import SchemaLibrary
from app.models.workflow import Workflow


def start_workflow_turn(
    *,
    document: str,
    project_name: str,
    model: str,
    data_model: SchemaLibrary | None,
    on_answer: Callable[[Workflow | None], None],
) -> str:
    """Start the workflow agent as a LIVE chat turn and return the session id.

    Creates a view-only session (the framing prompt shown as the user's message) and streams
    the agent on the shared TurnManager, so the compile is watchable at /chat/<sid> while it
    happens and persists when it ends. Compiles ONLY the workflow, grounding it in `data_model`
    (the approved schemas) when given. When the turn finishes, `on_answer` is called with the
    submitted Workflow — or None if none was submitted. Must be called from the server event
    loop (it starts a turn there)."""
    store = open_session_store()
    session_id = store.create(
        title=f"Generation · workflow · {project_name}",
        agent_id=None,  # view-only: rendered + streamed, but no agent to continue it
        context={"project_id": project_name, "phase": "workflow"},
    )
    agent = build_workflow_agent(document, data_model=data_model, model=model)
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


def build_workflow_agent(
    document: str, *, data_model: SchemaLibrary | None = None, model: str = "sonnet"
) -> Agent[Workflow]:
    """Configure the workflow agent for `document`: it distils the process into typed stages
    and SUBMITS them as a `Workflow` via submit_answer — validated (each stage's own invariants
    + the cross-stage graph checks), so a schema-invalid draft comes back as a tool error the
    agent corrects until the workflow is clean. When `data_model` (the approved schemas) is
    given, it grounds the task as the nouns the workflow imports and generates. Read `.answer`
    after the run/turn for the validated Workflow (None if nothing valid was submitted)."""
    return Agent(
        system_prompt=WORKFLOW_SYSTEM_PROMPT,
        target_schema=Workflow,
        task=_frame(document, data_model),
        model=model,
    )


def _frame(document: str, data_model: SchemaLibrary | None) -> str:
    """Frame the document (and, when grounded, the approved data model) as the material to
    compile, delimited so the agent treats it as source, not instructions."""
    grounding = ""
    if data_model is not None:
        grounding = "\n\n" + _render_data_model_reference(data_model)
    return (
        "Here is the methodology document. Compile it into a workflow of typed stages and "
        "submit it with submit_answer.\n\n"
        "----- DOCUMENT -----\n"
        f"{document}\n"
        "----- END DOCUMENT -----"
        f"{grounding}"
    )


def _render_data_model_reference(data_model: SchemaLibrary) -> str:
    """Render the reviewed data model as a reference block: the named schemas verbatim, framed
    as the nouns the workflow is grounded in (not a governing constraint), with the instruction
    to note where the workflow diverges from or extends them."""
    schemas_json = json.dumps(
        [s.model_dump(mode="json", exclude_none=True) for s in data_model.schemas],
        indent=2,
        ensure_ascii=False,
    )
    return (
        "# Data model — the nouns this workflow is grounded in\n"
        "The named schemas below are the nouns the workflow IMPORTS and GENERATES — the\n"
        "reviewed, agreed-upon entities that intellectually ground this pipeline. They ground\n"
        "the workflow; they do not rigidly govern it: the workflow may introduce intermediate\n"
        "objects it needs, or extend these nouns with extra fields. Start from them as the\n"
        "canonical entities, and note where you diverge from or extend them, and why.\n\n"
        f"{schemas_json}"
    )
