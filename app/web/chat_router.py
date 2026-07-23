"""FastAPI routes for the chat subsystem — a generic, agent-agnostic surface.

Every session is bound to a registered agent by an `agent_id` and carries an
opaque `context` (whatever that agent needs to bind its tools). A message turn
looks the pair back up, builds the engine via the registry, and streams it. The
routes know nothing about any specific agent; a concrete agent registers itself
(see app.core.agent.registry) and a host route creates the session with its context.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import app.web.config as web_config

from app.core.llm_sdk import CLI_PATH

from app.core.agent.registry import build_engine
from app.core.agent.sdk_engine import CLI_MODEL
from app.core.agent.session import create_agent_session
from app.core.agent.store import open_session_store
from app.core.agent.turns import default_turn_manager

TEMPLATES_DIR = Path(__file__).resolve().parent / "chat_templates"

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["friendly_time"] = web_config.friendly_time
_store = open_session_store()
_turns = default_turn_manager()


def _backend_label() -> str:
    if CLI_PATH is not None:
        return f"claude-cli:{CLI_MODEL} (subscription)"
    return "claude-cli (unavailable)"


def _backend_error() -> str | None:
    if CLI_PATH is not None:
        return None
    return (
        "The Claude CLI / Agent SDK isn't available. Install it and run "
        "`claude login` so the agent can run."
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_index(request: Request):
    all_sessions = _store.list_sessions()
    # Load full sessions to check context; filter out those marked hidden at the route level.
    visible_sessions = []
    for s in all_sessions:
        full_data = _store.load(s["session_id"])
        if full_data.get("context", {}).get("hidden") is not True:
            visible_sessions.append(s)
    return templates.TemplateResponse(request, "chat_index.html", {
        "sessions": visible_sessions,
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
        # No bound agent → the UI renders and streams the session, but there is no agent to
        # reply to a typed message (post_message 400s), so the composer is hidden.
        "view_only": data.get("agent_id") is None,
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
