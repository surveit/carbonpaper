"""Compile a methodology document into a DATA MODEL (a SchemaLibrary), headlessly.

The sibling of `app.compiler.compiler.compile_methodology` (prose → workflow stages):
this is prose → named schemas. It runs an `app.agent.Agent` whose target schema is
`SchemaLibrary`, so the agent SUBMITS the data model through the submit_answer tool
(validated against SchemaLibrary) rather than emitting free-text JSON. The result is a
typed, reference-checked data model; persisting it under a project's schemas/ is the
caller's job.
"""
from __future__ import annotations

from app.agent.agent import Agent
from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT
from app.models.named_schemas import SchemaLibrary


async def compile_data_model(document: str, *, model: str = "sonnet") -> SchemaLibrary:
    """Generate the named-schema data model for `document` as a validated
    SchemaLibrary. Raises GenerationError (via Agent.run) if the agent cannot submit a
    valid library within its attempt budget — it never returns a partial or fabricated
    model."""
    agent: Agent[SchemaLibrary] = Agent(
        system_prompt=DATA_MODEL_SYSTEM_PROMPT,
        target_schema=SchemaLibrary,
        task=_frame(document),
        model=model,
    )
    return await agent.run()


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
