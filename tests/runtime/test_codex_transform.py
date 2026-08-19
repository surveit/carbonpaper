from __future__ import annotations

import json
import sys
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel

from app.core.agent.agent import Agent
from app.core.agent.codex_engine import AgentEvent
from app.core.agent.errors import CodexProtocolError
from app.core.agent.usage import LlmUsage
from app.core.errors import GenerationError
from app.core.llm.options import LLMModel


_SERVER_SCRIPT = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path


requests_path = Path(sys.argv[1])
mode = sys.argv[2]


def send(message):
    print(json.dumps(message), flush=True)


def send_tool_call(request_id, arguments):
    send({
        "id": request_id,
        "method": "item/tool/call",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": request_id,
            "tool": "submit_answer",
            "arguments": arguments,
        },
    })


def send_completed():
    send({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "completed"},
        },
    })


def count_started_turns():
    if not requests_path.exists():
        return 0
    return sum(
        json.loads(line).get("method") == "turn/start"
        for line in requests_path.read_text(encoding="utf-8").splitlines()
    )


for line in sys.stdin:
    message = json.loads(line)
    with requests_path.open("a", encoding="utf-8") as requests_file:
        requests_file.write(json.dumps(message) + "\n")

    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fake-codex"}})
    elif method == "thread/start":
        send({"id": message["id"], "result": {"thread": {"id": "thread-1"}}})
    elif method == "turn/start":
        send({
            "id": message["id"],
            "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}},
        })
        if mode == "no_submission":
            send({
                "method": "item/agentMessage/delta",
                "params": {"delta": '{"verdict":"supported"}'},
            })
            send_completed()
        elif mode == "retry_then_valid" and count_started_turns() == 1:
            send_completed()
        elif mode == "mcp":
            send({
                "id": "mcp-request-1",
                "method": "mcpServer/elicitation/request",
                "params": {},
            })
        elif mode == "lifecycle_notification_then_valid":
            send({
                "method": "remoteControl/status/changed",
                "params": {
                    "status": "connected",
                },
            })
            send_tool_call(
                "submit-1",
                {"verdict": "supported", "answer_is_complete": True},
            )
        elif mode == "thread_started_notification_then_valid":
            send({
                "method": "thread/started",
                "params": {"thread": {"id": "thread-1"}},
            })
            send_tool_call(
                "submit-1",
                {"verdict": "supported", "answer_is_complete": True},
            )
        elif mode == "stalled":
            pass
        elif mode == "invalid_then_valid":
            send_tool_call("submit-1", {"answer_is_complete": True})
        else:
            send_tool_call(
                "submit-1",
                {"verdict": "supported", "answer_is_complete": True},
            )
    elif message.get("id") == "submit-1" and mode == "invalid_then_valid":
        send_tool_call(
            "submit-2",
            {"verdict": "supported", "answer_is_complete": True},
        )
    elif message.get("id") == "mcp-request-1":
        send({
            "method": "item/agentMessage/delta",
            "params": {"delta": "refusal received"},
        })
        send_completed()
    elif message.get("id") in {"submit-1", "submit-2"}:
        send_completed()
'''


@dataclass(frozen=True)
class FakeCodexServer:
    script_path: Path
    requests_path: Path

    @property
    def requests(self) -> list[dict[str, object]]:
        if not self.requests_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.requests_path.read_text(encoding="utf-8").splitlines()
        ]

    def build_command(self, mode: str) -> tuple[str, ...]:
        return (
            sys.executable,
            "-u",
            str(self.script_path),
            str(self.requests_path),
            mode,
        )


@pytest.fixture
def fake_codex_server(tmp_path: Path) -> FakeCodexServer:
    script_path = tmp_path / "fake_codex_transform_server.py"
    script_path.write_text(_SERVER_SCRIPT, encoding="utf-8")
    return FakeCodexServer(script_path, tmp_path / "requests.jsonl")


class Reply(BaseModel):
    verdict: Literal["supported"]


class UncooperativeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin = None
        self.terminated = False
        self.killed = False
        self._waiter = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await self._waiter.wait()
        return 0


class UncooperativeMcpServer:
    instances: list[UncooperativeMcpServer] = []

    def __init__(self, _command: object, _env: object) -> None:
        self.process = UncooperativeProcess()
        self.instances.append(self)

    async def initialize(self) -> None:
        return None

    async def request(self, method: str, _params: object) -> dict[str, object]:
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        return {"turn": {"id": "turn-1"}}

    async def next_message(self) -> dict[str, object]:
        return {"method": "mcpServer/startupStatus/updated", "params": {}}

    async def close(self) -> None:
        from app.core.agent.codex_protocol import _stop_process

        await _stop_process(self.process)


def test_agent_builds_the_shared_submit_answer_spec() -> None:
    agent = Agent(system_prompt="system", target_schema=Reply, task="judge")

    spec = agent.build_submit_answer_spec()
    result = spec.fn(verdict="supported", answer_is_complete=True)

    assert spec.name == "submit_answer"
    assert result.startswith("Accepted")
    assert agent.answer == Reply(verdict="supported")


def test_codex_transform_submits_validated_reply(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    reply, usage = _run_codex_transform(monkeypatch, fake_codex_server, "valid")

    assert reply == {"verdict": "supported"}
    assert usage.calls == 1
    assert usage.model == "gpt-5.6-terra"
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cost_usd is None


def test_invalid_submission_receives_feedback_and_can_be_corrected(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    events: list[AgentEvent] = []

    reply, usage = _run_codex_transform(
        monkeypatch, fake_codex_server, "invalid_then_valid", emit=events.append
    )

    assert reply == {"verdict": "supported"}
    assert usage.calls == 1
    rejection = next(
        request for request in fake_codex_server.requests if request.get("id") == "submit-1"
    )
    assert rejection["result"]["success"] is False  # type: ignore[index]
    assert "Submission rejected" in rejection["result"]["contentItems"][0]["text"]  # type: ignore[index]
    assert [event["kind"] for event in events].count("tool_call") == 2


def test_free_text_without_submit_answer_fails(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    with pytest.raises(GenerationError, match="submitted no valid Reply"):
        _run_codex_transform(monkeypatch, fake_codex_server, "no_submission")


def test_thread_start_receives_the_configured_model_and_one_answer_tool(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    _run_codex_transform(monkeypatch, fake_codex_server, "valid")

    request = next(
        call for call in fake_codex_server.requests if call.get("method") == "thread/start"
    )
    params = request["params"]
    assert params["model"] == "gpt-5.6-terra"  # type: ignore[index]
    assert [tool["name"] for tool in params["dynamicTools"]] == ["submit_answer"]  # type: ignore[index]


def test_each_retry_resolves_the_backend_and_records_a_call(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    import app.runtime.codex_transform as codex_transform

    resolutions = 0

    def resolve_backend() -> tuple[str, ...]:
        nonlocal resolutions
        resolutions += 1
        return fake_codex_server.build_command("retry_then_valid")

    monkeypatch.setattr(codex_transform, "require_codex_backend", resolve_backend)
    reply, usage = codex_transform.call_codex_transform(
        "system",
        "judge",
        Reply,
        LLMModel.gpt_5_6_terra,
        max_retries=1,
        emit=None,
    )

    assert reply == {"verdict": "supported"}
    assert resolutions == 2
    assert usage.calls == 2
    assert usage.model == "gpt-5.6-terra"
    assert usage.cost_usd is None


def test_failed_retries_report_each_started_call(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    import app.runtime.codex_transform as codex_transform

    usage_out: list[LlmUsage] = []
    monkeypatch.setattr(
        codex_transform,
        "require_codex_backend",
        lambda: fake_codex_server.build_command("no_submission"),
    )

    with pytest.raises(GenerationError, match="submitted no valid Reply"):
        codex_transform.call_codex_transform(
            "system",
            "judge",
            Reply,
            LLMModel.gpt_5_6_terra,
            max_retries=1,
            emit=None,
            usage_out=usage_out,
        )

    assert usage_out == [
        LlmUsage(
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            calls=1,
            model="gpt-5.6-terra",
        ),
        LlmUsage(
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            calls=1,
            model="gpt-5.6-terra",
        ),
    ]


def test_unexpected_mcp_request_is_rejected_without_invocation(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    events: list[AgentEvent] = []

    with pytest.raises(CodexProtocolError, match="unsupported.*mcpServer"):
        _run_codex_transform(
            monkeypatch, fake_codex_server, "mcp", emit=events.append
        )

    assert {"kind": "text", "text": "refusal received"} in events
    rejection = next(
        request
        for request in fake_codex_server.requests
        if request.get("id") == "mcp-request-1"
    )
    assert rejection["error"]["code"] == -32601  # type: ignore[index]


def test_uncooperative_shutdown_preserves_unsupported_mcp_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.agent.codex_protocol as codex_protocol
    import app.runtime.codex_transform as codex_transform

    UncooperativeMcpServer.instances.clear()
    monkeypatch.setattr(codex_protocol, "_PROCESS_SHUTDOWN_TIMEOUT_S", 0.001, raising=False)
    monkeypatch.setattr(codex_transform, "CodexAppServer", UncooperativeMcpServer)

    with pytest.raises(CodexProtocolError, match="mcpServer/startupStatus/updated"):
        asyncio.run(
            asyncio.wait_for(
                codex_transform._run_attempt(
                    (), "system", "judge", Reply, LLMModel.gpt_5_6_terra, None, [], None
                ),
                timeout=0.1,
            )
        )

    process = UncooperativeMcpServer.instances[0].process
    assert process.terminated
    assert process.killed


def test_stalled_turn_fails_at_the_row_deadline(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    import app.runtime.codex_transform as codex_transform

    monkeypatch.setattr(codex_transform, "DEFAULT_TIMEOUT_S", 0.1, raising=False)

    with pytest.raises(GenerationError, match="turn timed out after 0.1 seconds"):
        asyncio.run(
            asyncio.wait_for(
                codex_transform._run_attempt(
                    fake_codex_server.build_command("stalled"),
                    "system",
                    "judge",
                    Reply,
                    LLMModel.gpt_5_6_terra,
                    None,
                    [],
                    None,
                ),
                timeout=2,
            )
        )


def test_remote_control_status_notification_is_ignored_before_submit_answer(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    reply, usage = _run_codex_transform(
        monkeypatch, fake_codex_server, "lifecycle_notification_then_valid"
    )

    assert reply == {"verdict": "supported"}
    assert usage.calls == 1


def test_thread_started_notification_is_ignored_before_submit_answer(
    monkeypatch: pytest.MonkeyPatch, fake_codex_server: FakeCodexServer,
) -> None:
    reply, usage = _run_codex_transform(
        monkeypatch, fake_codex_server, "thread_started_notification_then_valid"
    )

    assert reply == {"verdict": "supported"}
    assert usage.calls == 1


def _run_codex_transform(
    monkeypatch: pytest.MonkeyPatch,
    fake_codex_server: FakeCodexServer,
    mode: str,
    *,
    emit: Callable[[AgentEvent], None] | None = None,
) -> tuple[dict[str, object], LlmUsage]:
    import app.runtime.codex_transform as codex_transform

    monkeypatch.setattr(
        codex_transform,
        "require_codex_backend",
        lambda: fake_codex_server.build_command(mode),
    )
    return codex_transform.call_codex_transform(
        "system",
        "judge",
        Reply,
        LLMModel.gpt_5_6_terra,
        max_retries=0,
        emit=emit,
    )
