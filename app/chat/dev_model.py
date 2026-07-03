"""Scripted dev chat model — NOT a real LLM.

Streams a fixed thinking -> (optional tool call) -> text sequence so the chat
pipeline (streaming, thinking on the FE, tool calls, persistence, reconnect) can
be exercised end-to-end without an API key. Selected only when the caller opts
in (``CW_CHAT_BACKEND=dev``); it is never a silent fallback, and every chunk is
prefixed so dev output can't be mistaken for a real model answer.

When a tool is registered, the first request streams a call to the first tool
with empty arguments (the demo tool takes none); after the tool result returns,
the second request streams the final text.
"""
from __future__ import annotations

from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel

BANNER = "[dev model - not a real LLM] "


async def _dev_stream(messages, info):
    tool_done = any(
        isinstance(p, ToolReturnPart)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
    )
    tool_names = [t.name for t in info.function_tools]

    if tool_names and not tool_done:
        yield {0: DeltaThinkingPart(content=BANNER + "The user asked something. ")}
        yield {0: DeltaThinkingPart(
            content=f"I'll call `{tool_names[0]}` to ground the answer in real data.")}
        yield {1: DeltaToolCall(name=tool_names[0], json_args="{}", tool_call_id="dev_tool_1")}
        return

    yield {0: DeltaThinkingPart(content=BANNER + "I have what I need; ")}
    yield {0: DeltaThinkingPart(content="composing the reply now.")}
    yield BANNER
    yield ("streaming, thinking, tool calls, persistence and reconnect are all real here - "
           "only these tokens are scripted. Set CW_CHAT_BACKEND=anthropic (with a key) "
           "for real Claude.")


def make_dev_model() -> FunctionModel:
    return FunctionModel(stream_function=_dev_stream, model_name="dev")
