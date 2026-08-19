"""JSONL transport for Codex app-server's bidirectional JSON-RPC messages."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any, Protocol

from app.core.agent.errors import CodexProtocolError as CodexProtocolError


type TypeUnsafeCodexJsonObject = dict[str, Any]
type CodexRequestId = int | str


_PROCESS_SHUTDOWN_TIMEOUT_S = 1


class _ProcessToStop(Protocol):
    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class CodexAppServer:
    def __init__(self, command: Sequence[str], env: Mapping[str, str]) -> None:
        self._command = tuple(command)
        self._env = dict(env)
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._messages: asyncio.Queue[TypeUnsafeCodexJsonObject | CodexProtocolError] = (
            asyncio.Queue()
        )
        self._waiting: dict[CodexRequestId, asyncio.Future[TypeUnsafeCodexJsonObject]] = {}
        self._terminal_error: CodexProtocolError | None = None
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._closing = False

    async def initialize(self) -> None:
        if self._process is not None:
            raise CodexProtocolError("Codex app-server is already initialized")
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._reader = asyncio.create_task(self._read_messages())
        await self.request("initialize", _initialize_params())
        await self._send({"method": "initialized", "params": {}})

    async def request(
        self, method: str, params: Mapping[str, object]
    ) -> TypeUnsafeCodexJsonObject:
        if self._terminal_error is not None:
            raise self._terminal_error
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": dict(params)})
            return await future
        finally:
            self._waiting.pop(request_id, None)

    async def next_message(self) -> TypeUnsafeCodexJsonObject:
        message = await self._messages.get()
        if isinstance(message, CodexProtocolError):
            raise message
        return message

    async def respond(
        self, request_id: CodexRequestId, result: Mapping[str, object]
    ) -> None:
        await self._send({"id": request_id, "result": dict(result)})

    async def respond_error(
        self, request_id: CodexRequestId, code: int, message: str
    ) -> None:
        await self._send({"id": request_id, "error": {"code": code, "message": message}})

    async def close(self) -> None:
        self._closing = True
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            await _stop_process(process)
        await self._stop_reader()
        self._fail_waiting(CodexProtocolError("Codex app-server closed"))
        self._process = None

    async def _read_messages(self) -> None:
        stdout = self._stdout()
        while line := await stdout.readline():
            try:
                message = _read_message(line)
                self._route(message)
            except CodexProtocolError as exc:
                self._fail(exc)
                return
        if not self._closing:
            self._fail(CodexProtocolError("Codex app-server closed stdout"))

    def _route(self, message: TypeUnsafeCodexJsonObject) -> None:
        if "method" in message:
            self._messages.put_nowait(message)
            return
        request_id = _read_response_id(message)
        future = self._waiting.get(request_id)
        if future is None:
            self._fail(CodexProtocolError(f"unexpected response id: {request_id!r}"))
            return
        _resolve_response(future, message)

    async def _send(self, message: Mapping[str, object]) -> None:
        stdin = self._stdin()
        try:
            encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        except (TypeError, ValueError) as exc:
            raise CodexProtocolError(f"invalid outbound JSON: {exc}") from exc
        async with self._write_lock:
            stdin.write(encoded)
            await stdin.drain()

    def _fail(self, error: CodexProtocolError) -> None:
        self._terminal_error = error
        self._fail_waiting(error)
        self._messages.put_nowait(error)

    def _fail_waiting(self, error: CodexProtocolError) -> None:
        for future in self._waiting.values():
            if not future.done():
                future.set_exception(error)

    async def _stop_reader(self) -> None:
        if self._reader is None or self._reader.done():
            return
        self._reader.cancel()
        with suppress(asyncio.CancelledError):
            await self._reader

    def _stdin(self) -> asyncio.StreamWriter:
        if self._process is None or self._process.stdin is None:
            raise CodexProtocolError("Codex app-server stdin is unavailable")
        return self._process.stdin

    def _stdout(self) -> asyncio.StreamReader:
        if self._process is None or self._process.stdout is None:
            raise CodexProtocolError("Codex app-server stdout is unavailable")
        return self._process.stdout


def _initialize_params() -> TypeUnsafeCodexJsonObject:
    return {
        "clientInfo": {"name": "carbon-paper", "version": "0"},
        "capabilities": {"experimentalApi": True},
    }


def _read_message(line: bytes) -> TypeUnsafeCodexJsonObject:
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexProtocolError(f"invalid JSON from Codex app-server: {exc}") from exc
    if not isinstance(message, dict):
        raise CodexProtocolError("invalid JSON from Codex app-server: expected an object")
    return message


def _read_response_id(message: TypeUnsafeCodexJsonObject) -> CodexRequestId:
    request_id = message.get("id")
    if not isinstance(request_id, (int, str)):
        raise CodexProtocolError("Codex app-server response has no valid id")
    return request_id


def _resolve_response(
    future: asyncio.Future[TypeUnsafeCodexJsonObject],
    message: TypeUnsafeCodexJsonObject,
) -> None:
    error = message.get("error")
    if error is not None:
        future.set_exception(CodexProtocolError(f"Codex app-server error: {error}"))
        return
    result = message.get("result")
    if not isinstance(result, dict):
        future.set_exception(CodexProtocolError("Codex app-server response has no object result"))
        return
    future.set_result(result)


async def _stop_process(process: _ProcessToStop) -> None:
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_SHUTDOWN_TIMEOUT_S)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=_PROCESS_SHUTDOWN_TIMEOUT_S)
