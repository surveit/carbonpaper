from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


_SERVER_SCRIPT = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path


requests_path = Path(sys.argv[1])
mode = sys.argv[2]
turn_started = False
tool_answered = False


def send(message):
    print(json.dumps(message), flush=True)


def finish_turn_if_ready():
    if not turn_started or not tool_answered:
        return
    if mode == "plain":
        send({
            "method": "item/reasoning/summaryTextDelta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "thinking"},
        })
    send({
        "method": "item/agentMessage/delta",
        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "done"},
    })
    send({
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
    })


for line in sys.stdin:
    message = json.loads(line)
    with requests_path.open("a", encoding="utf-8") as requests_file:
        requests_file.write(json.dumps(message) + "\n")

    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fake-codex"}})
    elif method == "initialized":
        if mode == "malformed":
            print("not json", flush=True)
        elif mode == "invalid_response":
            send({"id": None, "result": {}})
        elif mode == "eof":
            break
        elif mode == "plain":
            tool_answered = True
        else:
            request_methods = {
                "normal": "item/tool/call",
                "shell": "item/commandExecution/requestApproval",
                "file": "item/fileChange/requestApproval",
                "permission": "item/permissions/requestApproval",
                "mcp": "mcpServer/elicitation/request",
            }
            send({
                "id": "tool-request-1",
                "method": request_methods[mode],
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "tool-1",
                    "tool": "echo",
                    "arguments": {},
                },
            })
    elif method in {"thread/start", "thread/resume"}:
        send({"id": message["id"], "result": {"thread": {"id": "thread-1"}}})
    elif method == "turn/start":
        turn_started = True
        send({
            "id": message["id"],
            "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}},
        })
        finish_turn_if_ready()
    elif message.get("id") == "tool-request-1":
        tool_answered = True
        finish_turn_if_ready()
'''


@dataclass(frozen=True)
class FakeCodexServer:
    script_path: Path
    requests_path: Path

    @property
    def command(self) -> list[str]:
        return self.command_for("normal")

    @property
    def requests(self) -> list[dict[str, object]]:
        if not self.requests_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.requests_path.read_text(encoding="utf-8").splitlines()
        ]

    def command_for(self, mode: str) -> list[str]:
        return [sys.executable, "-u", str(self.script_path), str(self.requests_path), mode]


@pytest.fixture
def fake_codex_server(tmp_path: Path) -> FakeCodexServer:
    script_path = tmp_path / "fake_codex_server.py"
    script_path.write_text(_SERVER_SCRIPT, encoding="utf-8")
    return FakeCodexServer(script_path, tmp_path / "requests.jsonl")
