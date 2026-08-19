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
from app.core.errors import LLMError, StageWideFailure
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


class Deadline:
    """One unit of work's whole time budget. Every retry inside it spends the same clock."""

    def __init__(self, budget_s: float) -> None:
        self.budget_s = budget_s
        self._expires_at = time.monotonic() + budget_s

    def seconds_left(self) -> float:
        return self._expires_at - time.monotonic()


def open_row_deadline(llm_config: LLMConfig) -> Deadline:
    """A researching row searches and reads before it can answer, so it runs on its own clock."""
    return Deadline(RESEARCH_TIMEOUT_S if llm_config.tools else DEFAULT_TIMEOUT_S)


def open_chunk_deadline() -> Deadline:
    """No research budget: LLMConfig refuses tools with batch_size > 1."""
    return Deadline(DEFAULT_TIMEOUT_S)


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
        llm_config.max_retries, usage_out, open_row_deadline(llm_config),
        tools=llm_config.tools, thinking=llm_config.thinking,
    )


def call_llm_batch(
    stage_id: str,
    llm_config: LLMConfig,
    *,
    instructions: str,
    task: str,
    reply_schema: type[BaseModel],
    deadline: Deadline,
    model: str | None = None,
    usage_out: list[LlmUsage] | None = None,
) -> dict[str, Any]:
    """`deadline` is the CHUNK's, not this call's: its re-asks share one budget."""
    model_name = str(model or llm_config.model or DEFAULT_MODEL)
    # No tools by construction: LLMConfig refuses tools with batch_size > 1.
    return _run_agent(
        _compose_system(instructions), task, reply_schema, model_name,
        llm_config.max_retries, usage_out, deadline, thinking=llm_config.thinking,
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
    deadline: Deadline,
    tools: list[str] | None = None,
    thinking: ThinkingMode | None = None,
) -> dict[str, Any]:
    """Retries SHARE `deadline`: a slow failure spends the budget a fast one leaves for a re-try."""
    require_agent_backend()
    emit_llm_detail(LLM_PROMPT, text=task)
    # Captured HERE, on the caller's own thread, so it survives the thread hop
    # inside run_sync; RunLog.emit is itself thread-safe.
    forward = _forward_agent_events(current_detail_sink())
    attempts = max(1, (max_retries or 0) + 1)
    researching = bool(tools)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        timeout_s = deadline.seconds_left()
        if timeout_s <= 0:
            break
        agent: Agent[BaseModel] = Agent(
            system_prompt=system_prompt,
            target_schema=target_schema,
            task=task,
            model=model_name,
            builtin_tools=list(tools or []),
            max_turns=RESEARCH_MAX_TURNS if researching else None,
            thinking=_thinking_config(thinking),
        )
        try:
            answer = run_sync(
                asyncio.wait_for(agent.run(forward), timeout=timeout_s)
            )
            _record_usage(usage_out, agent, model_name)
            return answer.model_dump(mode="json")
        except StageWideFailure:
            # Not retryable by construction: the next attempt asks the same
            # exhausted account the same question. Its usage is still booked.
            _record_usage(usage_out, agent, model_name)
            raise
        except Exception as exc:  # noqa: BLE001 — retry any backend failure, record its usage, re-raise the last
            _record_usage(usage_out, agent, model_name)
            emit_llm_detail(LLM_ERROR, text=str(exc) or type(exc).__name__)
            last_exc = exc
            if attempt + 1 < attempts:
                _sleep_before_retry(attempt, deadline)
    raise _describe_exhausted_budget(last_exc, deadline)


def _sleep_before_retry(attempt: int, deadline: Deadline) -> None:
    """Bounded by what is left: sleeping past the deadline spends the budget on nothing."""
    time.sleep(max(0.0, min(4.0, 1.0 * (attempt + 1), deadline.seconds_left())))


def _describe_exhausted_budget(
    last_exc: Exception | None, deadline: Deadline
) -> Exception:
    """A budget spent without one attempt completing is a timeout, not a silent success."""
    if last_exc is not None:
        return last_exc
    return LLMError(
        f"the {deadline.budget_s:.0f}s budget for this call was already spent before "
        "an attempt could run"
    )


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
