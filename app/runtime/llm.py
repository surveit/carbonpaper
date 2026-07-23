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
import json
import os
import threading
import time
from typing import Any, Callable

from pydantic import BaseModel

from app.core.agent.agent import Agent
from app.core.errors import LLMError
from app.core.llm_sdk import run_sync
from app.core.models import LLMConfig

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

# The Messages API backend has no tool loop, so its system prompt frames the
# same calling convention in terms of the structured reply the schema enforces.
API_SYSTEM_PROMPT = (
    "You are executing one transform step of a data pipeline. Work from the "
    "task input you are given and produce exactly the required structured output."
)

# Short model aliases (as authored on a stage's `llm.model`) → the API model IDs
# the Messages API accepts. A value already in full-ID form passes through.
_API_MODEL_IDS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}


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
    model: str | None = None,
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
    returns a valid reply within `max_retries`, this raises — there is no
    fallback value that could masquerade as a real reply."""
    if not llm_config.prompt_template:
        raise LLMError(f"stage {stage_id}: llm_transform has no prompt_template")
    if llm_config.tools:
        raise LLMError(
            f"stage {stage_id}: llm.tools is not supported by the LLM backend"
        )
    prompt = render_prompt(llm_config.prompt_template, input_row)
    model_name = str(model or llm_config.model or DEFAULT_MODEL)
    if LLM_BACKEND == "api":
        return _call_api(llm_config, prompt, model_name, reply_model)
    return _call_agent(llm_config, prompt, model_name, reply_model)


def _retry(max_retries: int | None, attempt: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run `attempt` up to `max_retries` + 1 times for TRANSIENT failures; return
    its first success, or re-raise the LAST exception so the caller records a real
    error rather than a fabricated reply. `max_retries=N` allows N+1 total tries."""
    attempts = max(1, (max_retries or 0) + 1)
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return attempt()
        except Exception as exc:  # noqa: BLE001 — retry any backend failure, then re-raise the last
            last_exc = exc
            if i + 1 < attempts:
                time.sleep(min(4.0, 1.0 * (i + 1)))
    assert last_exc is not None  # attempts >= 1, so the loop ran and set this
    raise last_exc


def _call_agent(
    llm_config: LLMConfig, prompt: str, model_name: str, reply_model: type[BaseModel]
) -> dict[str, Any]:
    """Subscription (Claude Code) backend: a fresh structured-output Agent submits
    a valid `reply_model` instance via its tool loop. A fresh Agent per attempt."""
    require_agent_backend()

    def attempt() -> dict[str, Any]:
        agent: Agent[BaseModel] = Agent(
            system_prompt=SYSTEM_PROMPT,
            target_schema=reply_model,
            task=prompt,
            model=model_name,
        )
        answer = run_sync(asyncio.wait_for(agent.run(), timeout=DEFAULT_TIMEOUT_S))
        return answer.model_dump(mode="json")

    return _retry(llm_config.max_retries, attempt)


def _call_api(
    llm_config: LLMConfig, prompt: str, model_name: str, reply_model: type[BaseModel]
) -> dict[str, Any]:
    """Messages API backend: one call constrained to `reply_model`'s JSON schema
    via `messages.parse` (structured outputs). No tool loop and a small system
    prompt, so a call carries ~200 prompt tokens rather than the CLI harness's
    ~26k. Temperature is intentionally omitted — the newer API models reject it,
    and the schema plus a directive prompt already pin the output."""
    require_api_backend()
    client = _api_client()
    api_model = _API_MODEL_IDS.get(model_name, model_name)

    def attempt() -> dict[str, Any]:
        response = client.messages.parse(
            model=api_model,
            max_tokens=2048,
            system=API_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=reply_model,
        )
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(
                f"model returned no schema-valid reply (stop_reason={response.stop_reason})"
            )
        _record_usage(api_model, response.usage)
        return parsed.model_dump(mode="json")

    return _retry(llm_config.max_retries, attempt)


_USAGE_LOG_LOCK = threading.Lock()


def _record_usage(model: str, usage: Any) -> None:
    """Opt-in cost side-channel: when CW_COST_LOG names a file, append this call's
    token usage (as the API reports it) as one JSONL line. Observability only —
    never alters the reply or fails the call; safe under the row driver's threads."""
    path = os.environ.get("CW_COST_LOG")
    if not path or usage is None:
        return
    try:
        line = json.dumps({"model": model, "usage": usage.model_dump()}, default=str)
        with _USAGE_LOG_LOCK, open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, AttributeError):
        pass


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
    require = require_api_backend if LLM_BACKEND == "api" else require_agent_backend
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
