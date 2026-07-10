"""FastAPI routes for the chat subsystem — a generic, agent-agnostic surface.

Every session is bound to a registered agent by an `agent_id` and carries an
opaque `context` (whatever that agent needs to bind its tools). A message turn
looks the pair back up, builds the engine via the registry, and streams it. The
routes know nothing about any specific agent; a concrete agent registers itself
(see app.agent.registry) and a host route creates the session with its context.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.runtime.llm_agent_sdk import available as sdk_available

from app.agent.registry import build_engine
from app.agent.sdk_engine import CLI_MODEL

from .store import SessionStore
from .turns import TurnManager

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SESSIONS_DIR = Path(os.environ.get("CW_CHAT_SESSIONS_DIR", str(Path(__file__).resolve().parent / "_sessions")))

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_store = SessionStore(SESSIONS_DIR)
_turns = TurnManager()


def create_agent_session(agent_id: str, context: dict, *, title: str | None = None) -> str:
    """Create a chat session bound to `agent_id` carrying `context`, and return its
    id. The shared entry point a host route (e.g. an 'Edit with agent' button)
    calls to open a session it then redirects the browser to."""
    return _store.create(title=title or f"Agent: {agent_id}", agent_id=agent_id, context=context)


def _backend_label() -> str:
    if sdk_available():
        return f"claude-cli:{CLI_MODEL} (subscription)"
    return "claude-cli (unavailable)"


def _backend_error() -> str | None:
    if sdk_available():
        return None
    return (
        "The Claude CLI / Agent SDK isn't available. Install it and run "
        "`claude login` so the agent can run."
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_index(request: Request):
    return templates.TemplateResponse(request, "chat_index.html", {
        "sessions": _store.list_sessions(),
        "backend": _backend_label(),
    })


@router.post("/chat/agent/{agent_id}/sessions")
async def new_agent_session(agent_id: str, request: Request):
    """Open a chat session bound to `agent_id`. The body carries the opaque
    `context` (and optional `title`) as JSON. Redirects to the chat page."""
    body = await request.json()
    context = (body or {}).get("context") or {}
    title = (body or {}).get("title")
    sid = create_agent_session(agent_id, context, title=title)
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)


@router.get("/chat/{sid}", response_class=HTMLResponse)
async def chat_page(request: Request, sid: str):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    data = _store.load(sid)
    return templates.TemplateResponse(request, "chat.html", {
        "session_id": sid,
        "title": data.get("title"),
        "history": _store.history_view(sid),
        "pending_user": data.get("pending_user"),
        "active_turn": data.get("active_turn"),
        "backend": _backend_label(),
        "backend_error": _backend_error(),
    })


@router.post("/chat/{sid}/message")
async def post_message(sid: str, request: Request):
    """Send a message: the turn runs on the session's bound agent (its agent_id +
    context, looked up and handed to the registry to build the engine)."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    body = await request.json()
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    data = _store.load(sid)
    agent_id = data.get("agent_id")
    if not agent_id:
        raise HTTPException(status_code=400, detail="session has no bound agent")
    engine = build_engine(agent_id, data.get("context") or {})
    _store.set_pending_user(sid, text)
    turn_id = _turns.start(engine=engine, store=_store, session_id=sid, prompt=text)
    return JSONResponse({"ok": True, "turn_id": turn_id})


@router.get("/chat/{sid}/turn/{turn_id}/stream")
async def stream_turn(sid: str, turn_id: str, request: Request):
    try:
        from_index = int(request.query_params.get("from", "0"))
    except ValueError:
        from_index = 0

    async def gen():
        async for event in _turns.stream(turn_id, from_index):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/{sid}/messages")
async def get_messages(sid: str):
    """Raw history JSON (debug / re-slicing)."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(_store.load(sid))
