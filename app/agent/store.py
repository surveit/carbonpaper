"""File-based chat session store (axis-1 persistence).

One JSON file per session under the sessions dir, holding session metadata plus
one engine-agnostic transcript: a list of ``{role, parts}`` messages (part types
``text|thinking|tool_call|tool_result``) plus the resume token that carries the
agent's cross-turn memory. The transcript lives here, in the app's own files, not
in a vendor session store.

Single-machine, filesystem-backed. In-flight turns live in memory (see
app.agent.turns); surviving a server restart mid-turn is out of scope.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# The process-wide sessions directory the chat UI reads/writes. Env-overridable so a
# test (or an alternate deployment) can point it elsewhere.
SESSIONS_DIR = Path(
    os.environ.get("CW_CHAT_SESSIONS_DIR", str(Path(__file__).resolve().parent / "_sessions"))
)


def open_session_store() -> SessionStore:
    """The canonical session store the chat UI reads/writes (rooted at SESSIONS_DIR).
    Both the chat routes and headless writers (e.g. generation) use this so their
    sessions land in the same place and list together."""
    return SessionStore(SESSIONS_DIR)


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

    def create(
        self,
        *,
        title: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> str:
        """Create a session bound to `agent_id` (which agent answers) carrying an
        opaque `context` (what that agent needs to bind its tools). Both are read
        back by the message route to build the engine for each turn."""
        sid = uuid.uuid4().hex[:12]
        self._write(sid, {
            "session_id": sid,
            "created_at": _now(),
            "title": title or "New chat",
            "agent_id": agent_id,
            "context": context or {},
            "messages": [],
            "active_turn": None,
            "pending_user": None,
        })
        return sid

    def load(self, sid: str) -> dict:
        return self._read(sid)

    def load_messages(self, sid: str) -> list[dict[str, Any]]:
        """Always empty: the agent's cross-turn memory comes from resuming the CLI
        session (see resume_token), not from replaying a transcript. Kept so the
        turn manager can pass a uniform ``message_history`` the engine ignores."""
        del sid
        return []

    def save_messages(self, sid: str, messages: list[dict[str, Any]]) -> None:
        """Persist the engine's neutral ``{role, parts}`` transcript verbatim — it
        is already plain JSON."""
        data = self._read(sid)
        data["messages"] = messages
        data["pending_user"] = None
        self._write(sid, data)

    def set_active_turn(self, sid: str, turn_id: str | None) -> None:
        data = self._read(sid)
        data["active_turn"] = turn_id
        self._write(sid, data)

    def resume_token(self, sid: str) -> str | None:
        """The CLI session id to resume for this chat session's next turn, or None
        on the first turn. Carries conversation memory across turns."""
        return self._read(sid).get("sdk_session_id")

    def set_resume_token(self, sid: str, token: str) -> None:
        data = self._read(sid)
        data["sdk_session_id"] = token
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
        """The stored transcript rendered as simple bubbles for the template."""
        return _render_history_bubbles(self._read(sid).get("messages") or [])


def save_transcript_session(
    store: SessionStore,
    *,
    transcript: list[dict[str, Any]],
    title: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Persist a finished headless conversation (e.g. a generation run) as a VIEW-ONLY
    chat session and return its id. `agent_id` is left unset, so the chat UI renders the
    transcript but the message route refuses to continue it — there is no agent bound to
    answer a follow-up."""
    sid = store.create(title=title, agent_id=None, context=context)
    store.save_messages(sid, transcript)
    return sid


def _render_history_bubbles(messages: list[dict]) -> list[dict]:
    """Render a session's neutral transcript (``{role, parts}`` with part types
    ``text|thinking|tool_call|tool_result``) into bubble dicts ``chat.html``
    renders.

    The template's history loop only reads ``role``, ``text``, ``thinking`` and
    ``tools[].name/.args`` — tool results have no history slot and are not
    rendered on reload.
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
            tools = [{"name": p.get("name", ""), "args": p.get("args", ""),
                      "label": p.get("label") or p.get("name", "")}
                     for p in parts if p.get("type") == "tool_call"]
            bubbles.append({"role": "assistant", "thinking": thinking,
                            "text": text, "tools": tools})
    return bubbles
