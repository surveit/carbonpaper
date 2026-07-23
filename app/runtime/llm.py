"""LLM dispatch for `llm_transform` stages.

`call_llm` runs one input row through a headless structured-output agent
(`app.core.agent.agent.Agent`) whose `target_schema` is the stage's reply model —
the reply arrives as a validated Pydantic instance submitted through the
agent's submit_answer tool. The agent is the only backend: when it isn't
available (`options.require_agent_backend`), the call raises rather than
fabricating output.

Batching: the runtime's row driver (`app/runtime/stages/execution.py`) calls
`call_llm` once per row under bounded parallelism (default 4, override via
CW_LLM_PARALLEL).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypedDict

from pydantic import BaseModel

from app.core.agent.agent import Agent
from app.core.agent.usage import LlmUsage
from app.core.errors import LLMError
from app.core.llm_sdk import run_sync
from app.models import LLMConfig

from .options import (
    CLAUDE_BIN,
    DEFAULT_MODEL,
    DEFAULT_PARALLEL,
    DEFAULT_TIMEOUT_S,
    require_agent_backend,
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
    """Render the prompt by injecting row columns as {column}. A placeholder
    naming a column not in the row, or a malformed template, is a loud error —
    the model must never be called with a half-rendered prompt."""
    try:
        return template.format_map(row)
    except (KeyError, ValueError, IndexError) as exc:
        raise LLMError(
            f"prompt template could not be rendered ({type(exc).__name__}: {exc}); "
            f"a {{placeholder}} must name a row column, and literal braces must be escaped as {{{{ }}}}"
        ) from exc


def call_llm(
    stage_id: str,
    llm_config: LLMConfig,
    input_row: dict[str, Any],
    *,
    reply_model: type[BaseModel],
    model: str | None = None,
    usage_out: list[LlmUsage] | None = None,
) -> dict[str, Any]:
    """Single-row LLM call; returns the reply as a plain dict.

    A structured-output Agent must submit a valid `reply_model` instance
    (validated by construction, retried in-loop on rejection). When no agent
    backend is available this raises — there is no fallback, so a fabricated
    answer can never masquerade as a real model reply.

    If `usage_out` is given, each attempt's token/cost usage is appended to it —
    including a failed attempt's, since those tokens were still spent. Kept as an
    out-param rather than the return value so the reply-dict contract (and the
    tests that mock it) are unchanged."""
    require_agent_backend()

    if not llm_config.prompt_data_template:
        raise LLMError(f"stage {stage_id}: llm_transform has no prompt_data_template")
    if llm_config.tools:
        raise LLMError(
            f"stage {stage_id}: llm.tools is not supported by the agent backend"
        )
    task = render_prompt(llm_config.prompt_data_template, input_row)
    system_prompt = (
        SYSTEM_PROMPT
        if not llm_config.prompt_instructions
        else SYSTEM_PROMPT + "\n\n" + llm_config.prompt_instructions
    )
    model_name = str(model or llm_config.model or DEFAULT_MODEL)

    # Honor llm_config.max_retries for TRANSIENT backend failures (a dropped CLI
    # connection, a timeout) — distinct from the Agent's own in-loop retry on
    # schema rejection. max_retries=N allows N+1 total attempts; a fresh Agent is
    # built each attempt, and the LAST failure is re-raised so the caller still
    # records a real error rather than a fabricated reply.
    attempts = max(1, (llm_config.max_retries or 0) + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        agent: Agent[BaseModel] = Agent(
            system_prompt=system_prompt,
            target_schema=reply_model,
            task=task,
            model=model_name,
        )
        try:
            answer = run_sync(asyncio.wait_for(agent.run(), timeout=DEFAULT_TIMEOUT_S))
            _record_usage(usage_out, agent)
            return answer.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — retry any backend failure up to
            # max_retries, then re-raise the last so the caller records it. The
            # attempt still spent tokens (a rejected schema, a mid-turn drop), so
            # record its usage before retrying.
            _record_usage(usage_out, agent)
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(min(4.0, 1.0 * (attempt + 1)))
    assert last_exc is not None  # attempts >= 1, so the loop ran and set this
    raise last_exc


def _record_usage(usage_out: list[LlmUsage] | None, agent: Agent[BaseModel]) -> None:
    """Append this attempt's usage to the sink, if both are present. A turn that
    produced no ResultMessage (e.g. a timeout) leaves agent.last_usage None —
    nothing is recorded rather than a fabricated zero."""
    if usage_out is not None and agent.last_usage is not None:
        usage_out.append(agent.last_usage)


class LlmBackendStatus(TypedDict):
    """`backend_status()`'s return shape, and the value type of
    `RunContext.llm_backend` (app.runtime.context) — one per stage that ran an
    llm_transform."""

    backend: str | None
    backend_error: str | None
    claude_cli: str | None
    model_default: str
    parallel_default: int


def backend_status() -> LlmBackendStatus:
    """For UI/diagnostics: report whether the agent backend is available."""
    try:
        require_agent_backend()
        backend, backend_error = "agent", None
    except LLMError as exc:
        backend, backend_error = None, str(exc)
    return {
        "backend": backend,
        "backend_error": backend_error,
        "claude_cli": CLAUDE_BIN,
        "model_default": DEFAULT_MODEL,
        "parallel_default": DEFAULT_PARALLEL,
    }
