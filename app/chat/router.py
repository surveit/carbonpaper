"""FastAPI routes for the chat subsystem.

Mounts a minimal chat UI + the streaming transport onto the existing app.
The demo engine registers one real tool (`list_projects`, over the
workspace's own examples dir) to show "tools plugged in per context": a host
builds a ChatEngine with the tools its embedding needs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.runtime.llm_agent_sdk import available as sdk_available

from .engine import ChatBackendError, ChatEngine, backend_label
from .project_agent import get_project_agent, get_project_sdk_engine
from .store import SessionStore
from .turns import TurnManager

if TYPE_CHECKING:
    from .sdk_engine import ClaudeAgentSdkEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SESSIONS_DIR = Path(os.environ.get("CW_CHAT_SESSIONS_DIR", str(Path(__file__).resolve().parent / "_sessions")))

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_store = SessionStore(SESSIONS_DIR)
_turns = TurnManager()

_engine: ChatEngine | None = None
_engine_error: str | None = None


def _list_projects() -> list[str]:
    """List the projects available in this workspace."""
    examples = REPO_ROOT / "examples"
    if not examples.exists():
        return []
    return [p.name for p in sorted(examples.iterdir()) if (p / "compiled").is_dir()]


def get_engine() -> ChatEngine | None:
    """Build the demo engine lazily. A missing backend (e.g. no API key) is
    surfaced as an error, not silently mocked."""
    global _engine, _engine_error
    if _engine is None and _engine_error is None:
        try:
            _engine = ChatEngine(
                system_prompt=(
                    "You are embedded in the workflow app. Be concise and "
                    "cite the workspace's own data. Use tools to ground answers."
                ),
                tools=[_list_projects],
            )
        except ChatBackendError as exc:
            _engine_error = str(exc)
    return _engine


@router.get("/chat", response_class=HTMLResponse)
async def chat_index(request: Request):
    return templates.TemplateResponse(request, "chat_index.html", {
        "sessions": _store.list_sessions(),
        "backend": backend_label(),
    })


@router.post("/chat/sessions")
async def new_session():
    sid = _store.create()
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)


@router.post("/chat/project/{name}/sessions")
async def new_project_session(name: str):
    """Open a chat session bound to one project's editing agent. The session
    records its project in `context`, so the shared /chat/{sid} page (below)
    renders a composer that posts to this project's message route rather than
    the generic demo one."""
    sid = _store.create(title=f"Editing: {name}", context={"project": name})
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)


@router.get("/chat/{sid}", response_class=HTMLResponse)
async def chat_page(request: Request, sid: str):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    data = _store.load(sid)
    project = data.get("context", {}).get("project")
    if project:
        # A missing backend (e.g. no API key / no CLI) surfaces as a banner,
        # same as the demo engine below — never a raw 500 on this page.
        # Prefer the subscription SDK engine (Claude CLI) when available; it needs
        # no API key and can never raise ChatBackendError. Otherwise warm the
        # PydanticAI (API-key) agent, whose missing backend becomes the banner.
        try:
            if sdk_available():
                get_project_sdk_engine(project)  # warm
            else:
                get_project_agent(project)  # warm
            backend_error = None
        except ChatBackendError as exc:
            backend_error = str(exc)
    else:
        get_engine()  # warm; also populates _engine_error for the banner
        backend_error = _engine_error
    return templates.TemplateResponse(request, "chat.html", {
        "session_id": sid,
        "project": project,
        "title": data.get("title"),
        "history": _store.history_view(sid),
        "pending_user": data.get("pending_user"),
        "active_turn": data.get("active_turn"),
        "backend": backend_label(),
        "backend_error": backend_error,
    })


@router.post("/chat/{sid}/message")
async def post_message(sid: str, request: Request):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    body = await request.json()
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    engine = get_engine()
    if engine is None:
        return JSONResponse({"ok": False, "error": _engine_error}, status_code=400)
    _store.set_pending_user(sid, text)
    turn_id = _turns.start(engine=engine, store=_store, session_id=sid, prompt=text)
    return JSONResponse({"ok": True, "turn_id": turn_id})


@router.post("/chat/{sid}/project/{name}/message")
async def post_project_message(sid: str, name: str, request: Request):
    """Send a message on a project-scoped session: the turn runs on `name`'s
    editing agent (bound tools + system prompt), not the generic demo engine."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    body = await request.json()
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    # Prefer the subscription SDK engine (Claude CLI, no API key) when available;
    # fall back to the PydanticAI (API-key) agent otherwise. Both satisfy
    # stream_turn, so TurnManager.start is unchanged.
    try:
        engine: ChatEngine | ClaudeAgentSdkEngine
        if sdk_available():
            engine = get_project_sdk_engine(name)
        else:
            engine = get_project_agent(name)
    except ChatBackendError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
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
