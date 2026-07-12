"""Compile a methodology document into a DATA MODEL (a SchemaLibrary), headlessly.

The sibling of `app.compiler.compiler.compile_methodology` (prose → workflow stages):
this is prose → named schemas. It runs the model as a headless agent call
(`app.agent.generate_valid`) with the data-model system prompt, validating the agent's
JSON straight into a `SchemaLibrary` — so the result is a typed, reference-checked data
model, not a bag of dicts. Persisting it under a project's schemas/ is the caller's job.
"""
from __future__ import annotations

from app.agent.agent import generate_valid
from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT
from app.models.named_schemas import SchemaLibrary


async def compile_data_model(document: str, *, model: str = "sonnet") -> SchemaLibrary:
    """Generate the named-schema data model for `document` as a validated
    SchemaLibrary. Raises GenerationError (via generate_valid) if the agent cannot
    produce a valid library within the round budget — it never returns a partial or
    fabricated model."""
    return await generate_valid(
        system_prompt=DATA_MODEL_SYSTEM_PROMPT,
        seed=_seed_prompt(document),
        into=SchemaLibrary,
        model=model,
    )


def _seed_prompt(document: str) -> str:
    """Frame the methodology document as the material to model, delimited so the agent
    treats it as source, not instructions."""
    return (
        "Here is the methodology document. Author its data model — the named schemas — "
        "as one JSON object, following your instructions.\n\n"
        "----- DOCUMENT -----\n"
        f"{document}\n"
        "----- END DOCUMENT -----"
    )
