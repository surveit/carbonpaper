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
from typing import Any

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
    LLM_BACKEND,
    api_available,
    require_agent_backend,
    require_api_backend,
)

# Frames the calling convention only. Epistemic guidance (when a value is
# unknowable, how to weigh sources) is compiler-authored prompt content, not
# the runtime's voice.
SYSTEM_PROMPT = (
    "You are executing one transform step of a data pipeline. Work from the "
    "task input you are given. Produce the required output by calling the "
    "submit_answer tool exactly once; its input schema is the required reply."
)

# Same calling convention for the Messages API backend, which has no tool loop —
# it produces the structured reply the schema constrains rather than calling a tool.
API_SYSTEM_PROMPT = (
    "You are executing one transform step of a data pipeline. Work from the "
    "task input you are given and produce exactly the required structured output."
)

# The LLM_BACKEND value selecting the Messages API backend (vs the default
# "agent"). A named constant so the comparison sites can't drift apart.
_API_BACKEND = "api"

# Short model aliases (as authored on a stage's `llm.model`) → the API model IDs
# the Messages API accepts. A value already in full-ID form passes through.
_API_MODEL_IDS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}


def _compose_system(base: str, instructions: str) -> str:
    """A backend's base system prompt, plus the stage's row-invariant
    `prompt_instructions` if any — the stable, cacheable prefix shared across
    every row/chunk."""
    return base if not instructions else f"{base}\n\n{instructions}"


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

    Both backends produce the same thing — a validated `reply_model` instance
    dumped to a dict — from `input_row` and the stage prompt, selected by
    `CW_LLM_BACKEND` (see app.runtime.options.LLM_BACKEND):
      - "agent": a Claude Code (subscription) agent that submits the reply
        through a tool loop, structured by construction.
      - "api": one Messages API call whose `output_config.format` constrains the
        response to `reply_model`'s JSON schema.
    Neither fabricates: when the selected backend can't run, or the model never
    returns a valid reply within `max_retries`, this raises.

    If `usage_out` is given, each attempt's token/cost usage is appended to it —
    including a failed attempt's, since those tokens were still spent. Kept as an
    out-param so the reply-dict contract (and the tests that mock it) are
    unchanged."""
    if not llm_config.prompt_data_template:
        raise LLMError(f"stage {stage_id}: llm_transform has no prompt_data_template")
    if llm_config.tools:
        raise LLMError(
            f"stage {stage_id}: llm.tools is not supported by the LLM backend"
        )
    task = render_prompt(llm_config.prompt_data_template, input_row)
    model_name = str(model or llm_config.model or DEFAULT_MODEL)
    return _generate(
        llm_config, llm_config.prompt_instructions, task, model_name, reply_model, usage_out
    )


def call_llm_batch(
    stage_id: str,
    llm_config: LLMConfig,
    *,
    instructions: str,
    task: str,
    batch_model: type[BaseModel],
    model: str | None = None,
    usage_out: list[LlmUsage] | None = None,
) -> dict[str, Any]:
    """Invoke the model on a whole chunk at once. This is NOT a new invocation
    mechanism — it goes through the very same `_generate` seam (and thus the same
    backend, retries, and usage recording) as the per-row `call_llm`. The only
    differences are the caller's: `target_model` is a list-of-items model rather
    than a single-row reply, and `task`/`instructions` are pre-built by the batch
    driver (the numbered chunk rows + the copy-the-number contract). Returns the
    validated `{"results": [...]}` as a plain dict."""
    model_name = str(model or llm_config.model or DEFAULT_MODEL)
    return _generate(llm_config, instructions, task, model_name, batch_model, usage_out)


def _generate(
    llm_config: LLMConfig,
    instructions: str,
    task: str,
    model_name: str,
    target_model: type[BaseModel],
    usage_out: list[LlmUsage] | None,
) -> dict[str, Any]:
    """Dispatch to the selected backend (LLM_BACKEND): produce a validated
    `target_model` instance from `instructions` + `task`, dumped to a dict. Both
    the per-row (`call_llm`) and, in a later change, batched invocations funnel
    through here, so there is exactly one place the two backends diverge."""
    if LLM_BACKEND == _API_BACKEND:
        return _call_api(llm_config, instructions, task, model_name, target_model, usage_out)
    return _call_agent(llm_config, instructions, task, model_name, target_model, usage_out)


def _call_agent(
    llm_config: LLMConfig,
    instructions: str,
    task: str,
    model_name: str,
    target_model: type[BaseModel],
    usage_out: list[LlmUsage] | None,
) -> dict[str, Any]:
    """Subscription (Claude Code) backend: a fresh structured-output Agent submits
    a valid `target_model` instance via its tool loop. `max_retries` handles
    TRANSIENT backend failures; every attempt's usage (success or failure) is
    recorded, and the LAST failure is re-raised rather than fabricating a reply."""
    require_agent_backend()
    system_prompt = _compose_system(SYSTEM_PROMPT, instructions)
    attempts = max(1, (llm_config.max_retries or 0) + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        agent: Agent[BaseModel] = Agent(
            system_prompt=system_prompt,
            target_schema=target_model,
            task=task,
            model=model_name,
        )
        try:
            answer = run_sync(asyncio.wait_for(agent.run(), timeout=DEFAULT_TIMEOUT_S))
            _record_usage(usage_out, agent)
            return answer.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — retry any backend failure, record its usage, re-raise the last
            _record_usage(usage_out, agent)
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(min(4.0, 1.0 * (attempt + 1)))
    assert last_exc is not None  # attempts >= 1, so the loop ran and set this
    raise last_exc


def _call_api(
    llm_config: LLMConfig,
    instructions: str,
    task: str,
    model_name: str,
    target_model: type[BaseModel],
    usage_out: list[LlmUsage] | None,
) -> dict[str, Any]:
    """Messages API backend: one call constrained to `target_model`'s JSON schema
    via `messages.parse` (structured outputs). No tool loop and a small system
    prompt, so a call carries ~200 prompt tokens rather than the CLI harness's
    ~26k. Temperature is omitted — the newer API models reject it, and the schema
    plus a directive prompt pin the output. Retried on transient failure; each
    success records real token counts."""
    require_api_backend()
    client = _api_client()
    api_model = _API_MODEL_IDS.get(model_name, model_name)
    system = _compose_system(API_SYSTEM_PROMPT, instructions)
    attempts = max(1, (llm_config.max_retries or 0) + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.messages.parse(
                model=api_model,
                max_tokens=8192,  # headroom for larger structured replies
                system=system,
                messages=[{"role": "user", "content": task}],
                output_format=target_model,
            )
            parsed = response.parsed_output
            if parsed is None:
                raise LLMError(
                    f"model returned no schema-valid reply (stop_reason={response.stop_reason})"
                )
            if usage_out is not None:
                usage_out.append(_usage_from_api(response.usage))
            return parsed.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — retry any backend failure, then re-raise the last
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


def _usage_from_api(usage: Any) -> LlmUsage:
    """An LlmUsage from a Messages API `usage` block. `input_tokens` sums the fresh
    and cached input the call processed; `cost_usd` stays 0.0 — the Messages API
    returns no dollar cost and the runtime doesn't hardcode prices, so tokens are
    recorded truthfully and cost is left to a pricing-aware caller."""
    def _n(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    return LlmUsage(
        input_tokens=_n("input_tokens")
        + _n("cache_creation_input_tokens")
        + _n("cache_read_input_tokens"),
        output_tokens=_n("output_tokens"),
        cost_usd=0.0,
        calls=1,
    )


_API_CLIENT: Any = None


def _api_client() -> Any:
    """Lazily construct one Anthropic client, reused across rows/threads (the
    client is safe for concurrent use). Import is local so the `anthropic`
    dependency is only required when the API backend actually runs."""
    global _API_CLIENT
    if _API_CLIENT is None:
        from anthropic import Anthropic

        _API_CLIENT = Anthropic()
    return _API_CLIENT


def backend_status() -> dict[str, Any]:
    """For UI/diagnostics: report whether the SELECTED backend (LLM_BACKEND) can
    run, and why not if it can't."""
    require = require_api_backend if LLM_BACKEND == _API_BACKEND else require_agent_backend
    try:
        require()
        backend, backend_error = LLM_BACKEND, None
    except LLMError as exc:
        backend, backend_error = None, str(exc)
    return {
        "backend": backend,
        "backend_error": backend_error,
        "claude_cli": CLAUDE_BIN,
        "api_key_set": api_available(),
        "model_default": DEFAULT_MODEL,
        "parallel_default": DEFAULT_PARALLEL,
    }
