"""LLM dispatch for `llm_transform` stages.

`call_llm` routes one input row to the active backend chosen by
`options.get_llm_call_type()`: a headless structured-output agent
(`app.agent.agent.Agent`) whose `target_schema` is the stage's reply model —
the reply arrives as a validated Pydantic instance submitted through the
agent's submit_answer tool — or the opt-in offline mock (`llm_mock`). Backends
never silently fall back to the mock: a missing or failed live backend raises
rather than fabricating output.

Batching: the runtime's row driver (`app/runtime/stages/execution.py`) calls
`call_llm` once per row under bounded parallelism (default 4, override via
CW_LLM_PARALLEL).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from pydantic import BaseModel

from app.agent.agent import Agent
from app.core.errors import LLMError
from app.core.llm_sdk import run_sync
from app.core.models import LLMConfig

from . import llm_mock
from .options import (
    CLAUDE_BIN,
    DEFAULT_MODEL,
    DEFAULT_PARALLEL,
    DEFAULT_TIMEOUT_S,
    get_llm_call_type,
)

# Frames the calling convention only. Epistemic guidance (when a value is
# unknowable, how to weigh sources) is compiler-authored prompt content, not
# the runtime's voice.
SYSTEM_PROMPT = (
    "You are executing one transform step of a data pipeline. Work from the "
    "task input you are given. Produce the required output by calling the "
    "submit_answer tool exactly once; its input schema is the required reply."
)


def render_prompt(template: str, row: dict[str, Any]) -> str:
    """Render the prompt template safely. Missing placeholders are left
    as-is so we can still call the LLM rather than KeyError out."""
    class _Defaults(dict[str, Any]):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"
    try:
        return template.format_map(_Defaults(row))
    except (ValueError, IndexError, KeyError):
        # last-ditch: a malformed template (bad or positional placeholder) still
        # calls the LLM — append a JSON dump of the row so the model has access
        return template + "\n\n[row data]:\n" + json.dumps(
            {k: (str(v)[:1000] if not isinstance(v, (int, float, bool, type(None))) else v)
             for k, v in row.items()},
            indent=2,
        )


def call_llm(
    stage_id: str,
    llm_config: LLMConfig,
    input_row: dict[str, Any],
    *,
    reply_model: type[BaseModel],
    use_real: bool | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Single-row LLM call; returns the reply as a plain dict.

    Live path: a structured-output Agent must submit a valid `reply_model`
    instance (validated by construction, retried in-loop on rejection).
    `use_real=False` (or CW_LLM_FORCE_MOCK=1) selects the offline mock — the
    only way to reach it. A live backend that errors raises rather than
    degrading to the mock, so a fabricated answer never masquerades as a real
    model reply."""
    backend = "mock" if use_real is False else get_llm_call_type()

    if backend == "mock":
        reply = llm_mock.mock_llm_call(stage_id, llm_config, input_row)
        if not isinstance(reply, dict):
            raise LLMError(
                f"stage {stage_id}: mock returned {type(reply).__name__}, expected a dict"
            )
        return reply

    if not llm_config.prompt_template:
        raise LLMError(f"stage {stage_id}: llm_transform has no prompt_template")
    if llm_config.tools:
        raise LLMError(
            f"stage {stage_id}: llm.tools is not supported by the agent backend"
        )
    prompt = render_prompt(llm_config.prompt_template, input_row)
    agent: Agent[BaseModel] = Agent(
        system_prompt=SYSTEM_PROMPT,
        target_schema=reply_model,
        task=prompt,
        model=str(model or llm_config.model or DEFAULT_MODEL),
    )
    answer = run_sync(asyncio.wait_for(agent.run(), timeout=DEFAULT_TIMEOUT_S))
    return answer.model_dump(mode="json")


def backend_status() -> dict[str, Any]:
    """For UI/diagnostics: report which backend is active (or why none is)."""
    try:
        backend: str | None = get_llm_call_type()
        backend_error = None
    except LLMError as exc:
        backend = None
        backend_error = str(exc)
    return {
        "backend": backend,
        "backend_error": backend_error,
        "claude_cli": CLAUDE_BIN,
        "model_default": DEFAULT_MODEL,
        "parallel_default": DEFAULT_PARALLEL,
        "force_mock": os.environ.get("CW_LLM_FORCE_MOCK") == "1",
    }
