"""Codex app-server chat engine with client-owned dynamic tools."""
from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable, Sequence
from typing import Literal, TypedDict

from pydantic import BaseModel, ValidationError

from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.codex_protocol import (
    CodexAppServer,
    CodexProtocolError,
    CodexRequestId,
    TypeUnsafeCodexJsonObject,
)
from app.core.agent.codex_availability import require_codex_backend
from app.core.agent.store import TranscriptMessage, TranscriptPart


class ProseEvent(TypedDict):
    kind: Literal["text", "thinking"]
    text: str


class ErrorEvent(TypedDict):
    kind: Literal["error"]
    text: str


class ToolCallEvent(TypedDict):
    kind: Literal["tool_call"]
    name: str
    args: str
    label: str


class ToolResultEvent(TypedDict):
    kind: Literal["tool_result"]
    content: str


type AgentEvent = ProseEvent | ErrorEvent | ToolCallEvent | ToolResultEvent
type EmitEvent = Callable[[AgentEvent], None]

_COMMAND_APPROVAL = "item/commandExecution/requestApproval"
_FILE_APPROVAL = "item/fileChange/requestApproval"
_PERMISSION_APPROVAL = "item/permissions/requestApproval"
_DYNAMIC_TOOL_CALL = "item/tool/call"
_REASONING_DELTAS = {
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
}


class CodexChatEngine:
    def __init__(
        self, system_prompt: str, tools: list[BoundToolSpec], command: Sequence[str]
    ) -> None:
        self._system_prompt = system_prompt
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Codex dynamic tool names must be unique")
        self._command = tuple(command)

    async def stream_turn(
        self,
        prompt: str,
        *,
        message_history: object,
        emit: EmitEvent,
        resume: str | None,
    ) -> tuple[list[TranscriptMessage], str | None]:
        del message_history
        if self._command[:1] == ("codex",):
            require_codex_backend()
        server = CodexAppServer(self._command, os.environ)
        assistant_parts: list[TranscriptPart] = []
        try:
            await server.initialize()
            thread_id = await self._open_thread(server, resume)
            await server.request("turn/start", _turn_params(thread_id, prompt))
            await self._stream_events(server, assistant_parts, emit)
        finally:
            await server.close()
        return _build_transcript(prompt, assistant_parts), thread_id

    async def _open_thread(self, server: CodexAppServer, resume: str | None) -> str:
        if resume is not None:
            result = await server.request("thread/resume", {"threadId": resume})
        else:
            result = await server.request("thread/start", self._start_params())
        return _read_thread_id(result)

    def _start_params(self) -> TypeUnsafeCodexJsonObject:
        return {
            "baseInstructions": self._system_prompt,
            "developerInstructions": "",
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "environments": [],
            "dynamicTools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.json_schema,
                }
                for tool in self._tools.values()
            ],
        }

    async def _stream_events(
        self,
        server: CodexAppServer,
        assistant_parts: list[TranscriptPart],
        emit: EmitEvent,
    ) -> None:
        while True:
            message = await server.next_message()
            if "id" in message:
                await self._handle_server_request(server, message, assistant_parts, emit)
            elif self._handle_notification(message, assistant_parts, emit):
                return

    async def _handle_server_request(
        self,
        server: CodexAppServer,
        message: TypeUnsafeCodexJsonObject,
        assistant_parts: list[TranscriptPart],
        emit: EmitEvent,
    ) -> None:
        method = _read_string(message, "method")
        request_id = _read_request_id(message)
        if method == _DYNAMIC_TOOL_CALL:
            await self._call_tool(
                server, request_id, _read_params(message), assistant_parts, emit
            )
        elif method in {_COMMAND_APPROVAL, _FILE_APPROVAL}:
            await _deny_approval(server, request_id, method, emit)
        elif method == _PERMISSION_APPROVAL:
            await _deny_permissions(server, request_id, emit)
        else:
            await _reject_request(server, request_id, method, emit)

    async def _call_tool(
        self,
        server: CodexAppServer,
        request_id: CodexRequestId,
        params: TypeUnsafeCodexJsonObject,
        assistant_parts: list[TranscriptPart],
        emit: EmitEvent,
    ) -> None:
        name = _read_string(params, "tool")
        arguments = params.get("arguments")
        args_text = json.dumps(arguments, separators=(",", ":"))
        spec = self._tools.get(name)
        _record_tool_call(name, args_text, spec, assistant_parts, emit)
        try:
            text = await _invoke_tool(spec, arguments)
        except (CodexProtocolError, ValidationError) as exc:
            await server.respond(request_id, _tool_response(f"ERROR: {exc}", False))
            emit({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
            return
        await server.respond(request_id, _tool_response(text, True))
        emit({"kind": "tool_result", "content": text})
        assistant_parts.append({"type": "tool_result", "content": text})

    def _handle_notification(
        self,
        message: TypeUnsafeCodexJsonObject,
        assistant_parts: list[TranscriptPart],
        emit: EmitEvent,
    ) -> bool:
        method = _read_string(message, "method")
        if method == "item/agentMessage/delta":
            _append_prose("text", _read_delta(message), assistant_parts, emit)
        elif method in _REASONING_DELTAS:
            _append_prose("thinking", _read_delta(message), assistant_parts, emit)
        elif method == "error":
            emit({"kind": "error", "text": _read_error_text(message)})
        elif method == "turn/completed":
            _emit_turn_failure(message, emit)
            return True
        return False


async def _invoke_tool(spec: BoundToolSpec | None, arguments: object) -> str:
    if spec is None:
        raise CodexProtocolError("Codex requested an unknown dynamic tool")
    if not isinstance(arguments, dict):
        raise CodexProtocolError("Codex dynamic tool arguments must be an object")
    result = spec.fn(**spec.parse_arguments(arguments))
    if inspect.isawaitable(result):
        result = await result
    return _encode_tool_result(result)


async def _deny_approval(
    server: CodexAppServer, request_id: CodexRequestId, method: str, emit: EmitEvent
) -> None:
    await server.respond(request_id, {"decision": "decline"})
    emit({"kind": "error", "text": f"Codex access request denied: {method}"})


async def _deny_permissions(
    server: CodexAppServer, request_id: CodexRequestId, emit: EmitEvent
) -> None:
    await server.respond(request_id, {"permissions": {}})
    emit({"kind": "error", "text": "Codex permission request denied"})


async def _reject_request(
    server: CodexAppServer, request_id: CodexRequestId, method: str, emit: EmitEvent
) -> None:
    text = f"unsupported Codex server request: {method}"
    await server.respond_error(request_id, -32601, text)
    emit({"kind": "error", "text": text})


def _turn_params(thread_id: str, prompt: str) -> TypeUnsafeCodexJsonObject:
    return {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]}


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


def _emit_turn_failure(message: TypeUnsafeCodexJsonObject, emit: EmitEvent) -> None:
    turn = _read_params(message).get("turn")
    if not isinstance(turn, dict):
        raise CodexProtocolError("Codex turn completion has no turn object")
    status = _read_string(turn, "status")
    if status == "completed":
        return
    if status not in {"failed", "interrupted"}:
        raise CodexProtocolError(f"Codex turn completed with unknown status: {status}")
    emit({"kind": "error", "text": f"Codex turn ended with status {status}"})


def _append_prose(
    kind: Literal["text", "thinking"],
    text: str,
    assistant_parts: list[TranscriptPart],
    emit: EmitEvent,
) -> None:
    emit({"kind": kind, "text": text})
    assistant_parts.append({"type": kind, "text": text})


def _tool_call_event(
    name: str, args: str, spec: BoundToolSpec | None
) -> ToolCallEvent:
    return {
        "kind": "tool_call",
        "name": name,
        "args": args,
        "label": spec.label if spec is not None else name,
    }


def _record_tool_call(
    name: str,
    args: str,
    spec: BoundToolSpec | None,
    assistant_parts: list[TranscriptPart],
    emit: EmitEvent,
) -> None:
    event = _tool_call_event(name, args, spec)
    emit(event)
    assistant_parts.append({
        "type": "tool_call",
        "name": name,
        "args": args,
        "label": event["label"],
    })


def _tool_response(text: str, success: bool) -> TypeUnsafeCodexJsonObject:
    return {"contentItems": [{"type": "inputText", "text": text}], "success": success}


def _encode_tool_result(value: object) -> str:
    dumpable = (
        value.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(value, BaseModel)
        else value
    )
    return json.dumps(dumpable, ensure_ascii=False)


def _build_transcript(
    prompt: str, assistant_parts: list[TranscriptPart]
) -> list[TranscriptMessage]:
    return [
        {"role": "user", "parts": [{"type": "text", "text": prompt}]},
        {"role": "assistant", "parts": assistant_parts},
    ]
