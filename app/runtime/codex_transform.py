from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence

from pydantic import BaseModel

from app.core.agent.agent import Agent, SUBMIT_ANSWER_TOOL
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.codex_availability import require_codex_backend
from app.core.agent.codex_engine import AgentEvent
from app.core.agent.codex_protocol import (
    CodexAppServer,
    CodexProtocolError,
    CodexRequestId,
    TypeUnsafeCodexJsonObject,
)
from app.core.agent.usage import LlmUsage
from app.core.errors import GenerationError
from app.core.llm.options import LLMModel
from app.core.llm_sdk import run_sync


_DYNAMIC_TOOL_CALL = "item/tool/call"
_REASONING_DELTAS = {
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
}


def call_codex_transform(
    system_prompt: str,
    task: str,
    reply_model: type[BaseModel],
    model: LLMModel,
    max_retries: int,
    emit: Callable[[AgentEvent], None] | None,
) -> tuple[dict[str, object], LlmUsage]:
    if model.backend != "codex":
        raise ValueError(f"{model.value} does not select the Codex backend")
    usage_parts: list[LlmUsage] = []
    last_error: CodexProtocolError | GenerationError | OSError | None = None
    for _attempt in range(max(1, max_retries + 1)):
        command = require_codex_backend()
        try:
            reply = run_sync(
                _run_attempt(
                    command,
                    system_prompt,
                    task,
                    reply_model,
                    model,
                    emit,
                    usage_parts,
                )
            )
        except (CodexProtocolError, GenerationError, OSError) as exc:
            last_error = exc
            continue
        return reply, LlmUsage.summed(usage_parts)
    assert last_error is not None
    raise last_error


async def _run_attempt(
    command: Sequence[str],
    system_prompt: str,
    task: str,
    reply_model: type[BaseModel],
    model: LLMModel,
    emit: Callable[[AgentEvent], None] | None,
    usage_parts: list[LlmUsage],
) -> dict[str, object]:
    agent: Agent[BaseModel] = Agent(
        system_prompt=system_prompt,
        target_schema=reply_model,
        task=task,
        model=model.value,
    )
    answer_spec = agent.build_submit_answer_spec()
    server = CodexAppServer(command, os.environ)
    try:
        await server.initialize()
        thread = await server.request(
            "thread/start", _build_start_params(system_prompt, model, answer_spec)
        )
        thread_id = _read_thread_id(thread)
        await server.request("turn/start", _build_turn_params(thread_id, task))
        usage_parts.append(_build_unknown_usage(model))
        await _stream_turn(server, answer_spec, emit)
    finally:
        await server.close()
    answer = agent.answer
    if answer is None:
        raise GenerationError(f"Codex transform submitted no valid {reply_model.__name__}.")
    return answer.model_dump(mode="json")


async def _stream_turn(
    server: CodexAppServer,
    answer_spec: BoundToolSpec,
    emit: Callable[[AgentEvent], None] | None,
) -> None:
    while True:
        message = await server.next_message()
        method = _read_string(message, "method")
        if "id" in message:
            await _handle_server_request(server, message, method, answer_spec, emit)
        elif _handle_notification(message, method, emit):
            return


async def _handle_server_request(
    server: CodexAppServer,
    message: TypeUnsafeCodexJsonObject,
    method: str,
    answer_spec: BoundToolSpec,
    emit: Callable[[AgentEvent], None] | None,
) -> None:
    request_id = _read_request_id(message)
    if method != _DYNAMIC_TOOL_CALL:
        await _reject_server_request(server, request_id, method)
    params = _read_params(message)
    tool_name = _read_string(params, "tool")
    if tool_name != SUBMIT_ANSWER_TOOL:
        await _reject_server_request(server, request_id, f"tool {tool_name}")
    await _call_submit_answer(
        server, request_id, params.get("arguments"), answer_spec, emit
    )


async def _call_submit_answer(
    server: CodexAppServer,
    request_id: CodexRequestId,
    arguments: object,
    answer_spec: BoundToolSpec,
    emit: Callable[[AgentEvent], None] | None,
) -> None:
    args_text = json.dumps(arguments, separators=(",", ":"))
    _emit(
        emit,
        {
            "kind": "tool_call",
            "name": SUBMIT_ANSWER_TOOL,
            "args": args_text,
            "label": answer_spec.label,
        },
    )
    if not isinstance(arguments, dict):
        await _return_tool_error(
            server, request_id, "submit_answer arguments must be an object", emit
        )
        return
    try:
        result = answer_spec.fn(**answer_spec.parse_arguments(arguments))
    except ValueError as exc:
        await _return_tool_error(server, request_id, str(exc), emit)
        return
    text = str(result)
    await server.respond(request_id, _build_tool_response(text, success=True))
    _emit(emit, {"kind": "tool_result", "content": text})


async def _return_tool_error(
    server: CodexAppServer,
    request_id: CodexRequestId,
    message: str,
    emit: Callable[[AgentEvent], None] | None,
) -> None:
    text = f"ERROR: {message}"
    await server.respond(request_id, _build_tool_response(text, success=False))
    _emit(emit, {"kind": "tool_result", "content": text})


async def _reject_server_request(
    server: CodexAppServer, request_id: CodexRequestId, method: str
) -> None:
    text = f"unsupported Codex server request: {method}"
    await server.respond_error(request_id, -32601, text)
    raise CodexProtocolError(text)


def _handle_notification(
    message: TypeUnsafeCodexJsonObject,
    method: str,
    emit: Callable[[AgentEvent], None] | None,
) -> bool:
    if method == "item/agentMessage/delta":
        _emit(emit, {"kind": "text", "text": _read_delta(message)})
    elif method in _REASONING_DELTAS:
        _emit(emit, {"kind": "thinking", "text": _read_delta(message)})
    elif method == "error":
        _emit(emit, {"kind": "error", "text": _read_error_text(message)})
    elif method == "turn/completed":
        _check_turn_status(message)
        return True
    else:
        raise CodexProtocolError(f"unsupported Codex notification: {method}")
    return False


def _build_start_params(
    system_prompt: str, model: LLMModel, answer_spec: BoundToolSpec
) -> TypeUnsafeCodexJsonObject:
    return {
        "baseInstructions": system_prompt,
        "developerInstructions": "",
        "model": model.value,
        "sandbox": "read-only",
        "approvalPolicy": "never",
        "environments": [],
        "dynamicTools": [
            {
                "name": answer_spec.name,
                "description": answer_spec.description,
                "inputSchema": answer_spec.json_schema,
            }
        ],
    }


def _build_turn_params(thread_id: str, task: str) -> TypeUnsafeCodexJsonObject:
    return {"threadId": thread_id, "input": [{"type": "text", "text": task}]}


def _build_tool_response(text: str, success: bool) -> TypeUnsafeCodexJsonObject:
    return {"contentItems": [{"type": "inputText", "text": text}], "success": success}


def _build_unknown_usage(model: LLMModel) -> LlmUsage:
    return LlmUsage(
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        calls=1,
        model=model.value,
    )


def _read_thread_id(result: TypeUnsafeCodexJsonObject) -> str:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise CodexProtocolError("Codex thread response has no thread object")
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise CodexProtocolError("Codex thread response has no thread id")
    return thread_id


def _read_request_id(message: TypeUnsafeCodexJsonObject) -> CodexRequestId:
    request_id = message.get("id")
    if not isinstance(request_id, (int, str)):
        raise CodexProtocolError("Codex server request has no valid id")
    return request_id


def _read_params(message: TypeUnsafeCodexJsonObject) -> TypeUnsafeCodexJsonObject:
    params = message.get("params")
    if not isinstance(params, dict):
        raise CodexProtocolError("Codex message has no params object")
    return params


def _read_string(message: TypeUnsafeCodexJsonObject, key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str) or not value:
        raise CodexProtocolError(f"Codex message has no {key}")
    return value


def _read_delta(message: TypeUnsafeCodexJsonObject) -> str:
    return _read_string(_read_params(message), "delta")


def _read_error_text(message: TypeUnsafeCodexJsonObject) -> str:
    error = _read_params(message).get("error")
    if not isinstance(error, dict):
        raise CodexProtocolError("Codex error notification has no error object")
    return _read_string(error, "message")


def _check_turn_status(message: TypeUnsafeCodexJsonObject) -> None:
    turn = _read_params(message).get("turn")
    if not isinstance(turn, dict):
        raise CodexProtocolError("Codex turn completion has no turn object")
    status = _read_string(turn, "status")
    if status == "completed":
        return
    if status not in {"failed", "interrupted"}:
        raise CodexProtocolError(f"Codex turn completed with unknown status: {status}")
    raise CodexProtocolError(f"Codex transform turn ended with status {status}")


def _emit(
    emit: Callable[[AgentEvent], None] | None, event: AgentEvent
) -> None:
    if emit is not None:
        emit(event)
