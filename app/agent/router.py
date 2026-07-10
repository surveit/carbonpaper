"""FastAPI routes for the chat subsystem.

Mounts a minimal chat UI + the streaming transport onto the existing app. Every
session is bound to a project's editing agent: the subscription SDK engine
(Claude CLI) runs the in-process MCP tools for that project.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.runtime.llm_agent_sdk import available as sdk_available

from app.compiler.agent.config import get_project_sdk_engine

from .sdk_engine import CLI_MODEL
from .store import SessionStore
from .turns import TurnManager

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SESSIONS_DIR = Path(os.environ.get("CW_CHAT_SESSIONS_DIR", str(Path(__file__).resolve().parent / "_sessions")))

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_store = SessionStore(SESSIONS_DIR)
_turns = TurnManager()


def _backend_label() -> str:
    if sdk_available():
        return f"claude-cli:{CLI_MODEL} (subscription)"
    return "claude-cli (unavailable)"


def _backend_error() -> str | None:
    if sdk_available():
        return None
    return (
        "The Claude CLI / Agent SDK isn't available. Install it and run "
        "`claude login` so the editing agent can run."
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_index(request: Request):
    return templates.TemplateResponse(request, "chat_index.html", {
        "sessions": _store.list_sessions(),
        "backend": _backend_label(),
    })


@router.post("/chat/project/{name}/sessions")
async def new_project_session(name: str):
    """Open a chat session bound to one project's editing agent. The session
    records its project in `context`, so the shared /chat/{sid} page renders a
    composer that posts to this project's message route."""
    sid = _store.create(title=f"Editing: {name}", context={"project": name})
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)


@router.get("/chat/{sid}", response_class=HTMLResponse)
async def chat_page(request: Request, sid: str):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    data = _store.load(sid)
    project = data.get("context", {}).get("project")
    return templates.TemplateResponse(request, "chat.html", {
        "session_id": sid,
        "project": project,
        "title": data.get("title"),
        "history": _store.history_view(sid),
        "pending_user": data.get("pending_user"),
        "active_turn": data.get("active_turn"),
        "backend": _backend_label(),
        "backend_error": _backend_error(),
    })


@router.post("/chat/{sid}/project/{name}/message")
async def post_project_message(sid: str, name: str, request: Request):
    """Send a message on a project-scoped session: the turn runs on `name`'s
    editing agent (bound tools + system prompt) via the subscription SDK engine."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    body = await request.json()
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    engine = get_project_sdk_engine(name)
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
