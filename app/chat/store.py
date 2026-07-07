"""File-based chat session store (axis-1 persistence).

One JSON file per session under the sessions dir, holding session metadata plus
the PydanticAI message history serialised with ``to_jsonable_python``; reload
validates it back to typed messages via ``ModelMessagesTypeAdapter``. The
transcript lives here, in the app's own files, not in a vendor session store.

Single-machine, filesystem-backed. In-flight turns live in memory (see
app.chat.turns); surviving a server restart mid-turn is out of scope.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_core import to_jsonable_python


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.root / f"{sid}.json"

    def exists(self, sid: str) -> bool:
        return self._path(sid).exists()

    def _read(self, sid: str) -> dict:
        return json.loads(self._path(sid).read_text(encoding="utf-8"))

    def _write(self, sid: str, data: dict) -> None:
        self._path(sid).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def create(self, *, title: str | None = None, context: dict | None = None) -> str:
        sid = uuid.uuid4().hex[:12]
        self._write(sid, {
            "session_id": sid,
            "created_at": _now(),
            "title": title or "New chat",
            "context": context or {},
            "messages": [],
            "active_turn": None,
            "pending_user": None,
        })
        return sid

    def load(self, sid: str) -> dict:
        return self._read(sid)

    @staticmethod
    def _is_project(doc: dict) -> bool:
        """A project (SDK) session persists neutral {role, parts} transcripts;
        a demo session persists PydanticAI messages."""
        return bool((doc.get("context") or {}).get("project"))

    def load_messages(self, sid: str) -> list[ModelMessage]:
        doc = self._read(sid)
        # Project sessions are stateless per turn: the SDK engine ignores
        # message_history and the tools read durable on-disk project state, so
        # nothing is fed back into the next turn.
        if self._is_project(doc):
            return []
        raw = doc.get("messages") or []
        if not raw:
            return []
        return list(ModelMessagesTypeAdapter.validate_python(raw))

    def save_messages(self, sid: str, messages) -> None:
        data = self._read(sid)
        if self._is_project(data):
            # `messages` is already the neutral {role, parts} transcript from the
            # SDK engine; it is plain JSON, so persist it verbatim.
            data["messages"] = messages
        else:
            data["messages"] = to_jsonable_python(messages)
        data["pending_user"] = None
        self._write(sid, data)

    def set_active_turn(self, sid: str, turn_id: str | None) -> None:
        data = self._read(sid)
        data["active_turn"] = turn_id
        self._write(sid, data)

    def set_pending_user(self, sid: str, text: str | None) -> None:
        data = self._read(sid)
        data["pending_user"] = text
        self._write(sid, data)

    def list_sessions(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json"), reverse=True):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({
                "session_id": d.get("session_id"),
                "title": d.get("title"),
                "created_at": d.get("created_at"),
            })
        return out

    def history_view(self, sid: str) -> list[dict]:
        """Stored messages rendered as simple bubbles for the template."""
        if self._is_project(self._read(sid)):
            return _summarize_neutral(self._read(sid).get("messages") or [])
        return summarize(self.load_messages(sid))


def summarize(messages) -> list[dict]:
    bubbles: list[dict] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    bubbles.append({"role": "user", "text": _content_str(p.content)})
        elif isinstance(m, ModelResponse):
            thinking = "".join(p.content for p in m.parts if isinstance(p, ThinkingPart))
            text = "".join(p.content for p in m.parts if isinstance(p, TextPart))
            tools = [{"name": p.tool_name, "args": p.args_as_json_str()}
                     for p in m.parts if isinstance(p, ToolCallPart)]
            bubbles.append({"role": "assistant", "thinking": thinking,
                            "text": text, "tools": tools})
    return bubbles


def _summarize_neutral(messages: list[dict]) -> list[dict]:
    """Render a project session's neutral transcript (``{role, parts}`` with
    part types ``text|thinking|tool_call|tool_result``) into the same bubble
    dicts ``summarize`` produces, so ``chat.html`` renders them identically.

    The template's history loop only reads ``role``, ``text``, ``thinking`` and
    ``tools[].name/.args`` — matching the demo, tool results have no history
    slot and are not rendered on reload.
    """
    bubbles: list[dict] = []
    for message in messages:
        role = message.get("role")
        parts = message.get("parts") or []
        if role == "user":
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            bubbles.append({"role": "user", "text": text})
        elif role == "assistant":
            thinking = "".join(p.get("text", "") for p in parts if p.get("type") == "thinking")
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            tools = [{"name": p.get("name", ""), "args": p.get("args", "")}
                     for p in parts if p.get("type") == "tool_call"]
            bubbles.append({"role": "assistant", "thinking": thinking,
                            "text": text, "tools": tools})
    return bubbles


def _content_str(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(c if isinstance(c, str) else str(c) for c in content)
    return str(content)
