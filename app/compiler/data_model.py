"""Compile a methodology document into a DATA MODEL (a SchemaLibrary), headlessly.

The sibling of `app.compiler.compiler.compile_methodology` (prose → workflow stages):
this is prose → named schemas. It builds an `app.agent.Agent` whose target schema is
`SchemaLibrary`, so the agent SUBMITS the data model through the submit_answer tool
(validated against SchemaLibrary) rather than emitting free-text JSON. Running the agent
yields a typed, reference-checked data model; running it and persisting the result (and
the conversation) is the caller's job.
"""
from __future__ import annotations

from app.agent.agent import Agent
from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT
from app.models.named_schemas import SchemaLibrary


def build_data_model_agent(document: str, *, model: str = "sonnet") -> Agent[SchemaLibrary]:
    """Configure the data-model agent for `document`: it authors the named-schema data
    model and SUBMITS it as a SchemaLibrary via submit_answer. Drive it with `.run()`,
    which returns the validated SchemaLibrary or raises GenerationError (it never returns
    a partial or fabricated model); read `.transcript` afterwards to persist the
    conversation. Returning the agent (rather than just the library) lets the caller
    reach that transcript even when the run fails."""
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
