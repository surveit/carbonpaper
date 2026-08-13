"""LLM dispatch for `llm_transform` stages.

The structured-output agent is the only backend: when it is unavailable
(`options.require_agent_backend`) the call raises rather than fabricating output.
Called once per row by the row driver, bounded parallelism (4, CARBON_PAPER_LLM_PARALLEL)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from pydantic import BaseModel

from app.core.agent.agent import Agent
from app.core.agent.usage import LlmUsage
from app.core.errors import LLMError
from app.core.llm_sdk import run_sync
from app.core.agent.sdk_engine import ThinkingConfig
from app.models.stages.llm_transform import LLMConfig, ThinkingMode

from .options import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    RESEARCH_MAX_TURNS,
    RESEARCH_TIMEOUT_S,
    require_agent_backend,
)
from .run_log import (
    LLM_ERROR,
    LLM_PROMPT,
    LLM_RESPONSE,
    LLM_TEXT,
    LLM_THINKING,
    LLM_SYSTEM,
    LLM_TOOL_RESULT,
    DetailSink,
    current_detail_sink,
    emit_llm_detail,
)

# Engine stream-event kind → the detail-log kind surfaced on the run page. The
# engine speaks in raw block types; the run log speaks in what a reader wants to
# see: the model's thinking, its free text, the answer it submitted (a
# submit_answer tool_call), the verdict that came back on that submission, and
# the CLI's own init inventory of what the model was offered.
# The verdict is logged because a call rejected upstream — against the tool's
# input schema, before dispatch — never reaches the tool function, so the
# tool_result is the only record that the model called the tool at all.
_LLM_EVENT_KINDS = {
    "thinking": LLM_THINKING,
    "text": LLM_TEXT,
    "tool_call": LLM_RESPONSE,
    "tool_result": LLM_TOOL_RESULT,
    "error": LLM_ERROR,
    "system": LLM_SYSTEM,
}

# Frames the calling convention only. Epistemic guidance (when a value is
# unknowable, how to weigh sources) is compiler-authored prompt content, not
# the runtime's voice.
SYSTEM_PROMPT = (
    "You are executing one transform step of a data pipeline. Work from the "
    "task input you are given. Produce the required output by calling the "
    "submit_answer tool exactly once; its input schema is the required reply."
)


def render_prompt(template: str, row: dict[str, Any]) -> str:
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
    """`usage_out` collects EVERY attempt's usage, failed ones included — those tokens were spent."""
    if not llm_config.prompt_data_template:
        raise LLMError(f"stage {stage_id}: llm_transform has no prompt_data_template")
    task = render_prompt(llm_config.prompt_data_template, input_row)
    model_name = str(model or llm_config.model or DEFAULT_MODEL)
    return _run_agent(
        _compose_system(llm_config.prompt_instructions), task, reply_model, model_name,
        llm_config.max_retries, usage_out, tools=llm_config.tools,
        thinking=llm_config.thinking,
    )


def call_llm_batch(
    stage_id: str,
    llm_config: LLMConfig,
    *,
    instructions: str,
    task: str,
    reply_schema: type[BaseModel],
    model: str | None = None,
    usage_out: list[LlmUsage] | None = None,
) -> dict[str, Any]:
    model_name = str(model or llm_config.model or DEFAULT_MODEL)
    # No tools by construction: LLMConfig refuses tools with batch_size > 1.
    return _run_agent(
        _compose_system(instructions), task, reply_schema, model_name,
        llm_config.max_retries, usage_out, thinking=llm_config.thinking,
    )


def _thinking_config(mode: "ThinkingMode | None") -> ThinkingConfig | None:
    if mode is None:
        return None
    return {"type": "disabled"} if mode == "disabled" else {"type": "adaptive"}


def _compose_system(instructions: str) -> str:
    return SYSTEM_PROMPT if not instructions else SYSTEM_PROMPT + "\n\n" + instructions


def _run_agent(
    system_prompt: str,
    task: str,
    target_schema: type[BaseModel],
    model_name: str,
    max_retries: int,
    usage_out: list[LlmUsage] | None,
    tools: list[str] | None = None,
    thinking: ThinkingMode | None = None,
) -> dict[str, Any]:
    require_agent_backend()
    emit_llm_detail(LLM_PROMPT, text=task)
    # Captured HERE, on the caller's own thread, so it survives the thread hop
    # inside run_sync; RunLog.emit is itself thread-safe.
    forward = _forward_agent_events(current_detail_sink())
    attempts = max(1, (max_retries or 0) + 1)
    researching = bool(tools)
    timeout_s = RESEARCH_TIMEOUT_S if researching else DEFAULT_TIMEOUT_S
    last_exc: Exception | None = None
    for attempt in range(attempts):
        agent: Agent[BaseModel] = Agent(
            system_prompt=system_prompt,
            target_schema=target_schema,
            task=task,
            model=model_name,
            granted_tools=list(tools or []),
            max_turns=RESEARCH_MAX_TURNS if researching else None,
            thinking=_thinking_config(thinking),
        )
        try:
            answer = run_sync(
                asyncio.wait_for(agent.run(forward), timeout=timeout_s)
            )
            _record_usage(usage_out, agent, model_name)
            return answer.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — retry any backend failure, record its usage, re-raise the last
            _record_usage(usage_out, agent, model_name)
            emit_llm_detail(LLM_ERROR, text=str(exc) or type(exc).__name__)
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(min(4.0, 1.0 * (attempt + 1)))
    assert last_exc is not None  # attempts >= 1, so the loop ran and set this
    raise last_exc


def _forward_agent_events(
    sink: DetailSink | None,
) -> Callable[[dict[str, Any]], None] | None:
    if sink is None:
        return None

    def emit(event: dict[str, Any]) -> None:
        kind = _LLM_EVENT_KINDS.get(event.get("kind", ""))
        if kind is None:
            return
        # A tool_call's `args` (the submitted answer) is the useful body, and a
        # tool_result's is its `content`; other kinds carry `text`. Normalize all
        # three onto `text` so the run page renders one shape; an event carrying
        # none has no body to show and is dropped rather than logged as empty.
        if "text" in event:
            body = event["text"]
        elif "args" in event:
            body = event["args"]
        elif "content" in event:
            body = event["content"]
        else:
            return
        fields: dict[str, Any] = {"text": body}
        if event.get("label"):
            fields["label"] = event["label"]
        sink.emit(kind, **fields)

    return emit


def _record_usage(
    usage_out: list[LlmUsage] | None, agent: Agent[BaseModel], model_name: str
) -> None:
    """`model_name` is stamped here because this is where `model or llm.model or DEFAULT_MODEL` resolved."""
    if usage_out is not None and agent.last_usage is not None:
        usage_out.append(agent.last_usage.model_copy(update={"model": model_name}))
