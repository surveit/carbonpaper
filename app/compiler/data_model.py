"""Compile a methodology document into a DATA MODEL (named schemas), headlessly.

The sibling of `app.compiler.compiler.compile_methodology` (prose → workflow stages):
this is prose → named schemas. It runs the model as a headless agent call
(`app.agent.generate_valid`) with the data-model system prompt, parses the ```schema
fenced blocks it emits, and validates the whole set as a SchemaLibrary — feeding any
validation errors back to the agent until the data model is valid. Returns the
validated schema dicts; persisting them under a project's schemas/ is the caller's job.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.agent.agent import generate_valid
from app.compiler.data_model_prompt import DATA_MODEL_SYSTEM_PROMPT
from app.models import validate_schema_library

# A fenced block: ```<lang>\n<body>```. The lang tag selects `schema` blocks; the body
# is parsed as JSON. DOTALL so a block spans lines; non-greedy so blocks don't merge.
_FENCE_RE = re.compile(r"```([A-Za-z0-9_]+)[^\n]*\n(.*?)```", re.DOTALL)


async def compile_data_model(document: str, *, model: str = "sonnet") -> list[dict[str, Any]]:
    """Generate the named-schema data model for `document`. Raises GenerationError
    (via generate_valid) if the agent cannot produce a valid SchemaLibrary within the
    round budget — it never returns a partial or fabricated model."""
    return await generate_valid(
        system_prompt=DATA_MODEL_SYSTEM_PROMPT,
        seed=_seed_prompt(document),
        extract=parse_schema_blocks,
        validate=validate_schema_library,
        model=model,
    )


def parse_schema_blocks(text: str) -> list[dict[str, Any]]:
    """Extract every ```schema fenced block from `text` as a JSON object. Raises if no
    schema block is present or a block is not valid JSON — generate_valid turns that
    into feedback so the agent re-emits, so a malformed block is never silently
    dropped."""
    blocks = [body for lang, body in _FENCE_RE.findall(text) if lang == "schema"]
    if not blocks:
        raise ValueError("no ```schema blocks in the output")
    return [_load_schema_block(body) for body in blocks]


def _load_schema_block(body: str) -> dict[str, Any]:
    """Parse one fenced block's body as a single JSON schema object (raising on
    malformed JSON or a non-object, so the error is fed back to the agent)."""
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("a ```schema block must be a single JSON object")
    return parsed


def _seed_prompt(document: str) -> str:
    """Frame the methodology document as the material to model, delimited so the agent
    treats it as source, not instructions."""
    return (
        "Here is the methodology document. Author its data model as named schemas, "
        "following your instructions.\n\n"
        "----- DOCUMENT -----\n"
        f"{document}\n"
        "----- END DOCUMENT -----"
    )
