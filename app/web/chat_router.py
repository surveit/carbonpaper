"""FastAPI routes for the chat subsystem -- generic and agent-agnostic: a session
binds an `agent_id` to an opaque `context`, and a turn rebuilds the engine from
the registry (app.core.agent.registry) rather than knowing any concrete agent.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app.core.llm_sdk import CLI_PATH

from app.core.agent.registry import build_engine
from app.core.agent.sdk_engine import CLI_MODEL
from app.core.agent.session import create_agent_session
from app.core.agent.store import open_session_store
from app.core.agent.turns import default_turn_manager
from app.web.breadcrumbs import build_chat_crumbs, build_home_crumbs
from app.web.config import templates
from app.web.markdown_render import render_markdown

# The one place the sealed renderer is bound. app.web.config owns the shared env, so
# the filter is registered here, beside the only page that uses it.
templates.env.filters["markdown"] = render_markdown

router = APIRouter()
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
        "crumbs": build_home_crumbs("Chats"),
    })


@router.post("/chat/new")
async def new_chat():
    """Open an editing session bound to no project; the agent asks which one it needs."""
    sid = create_agent_session("editing", {}, title="New chat")
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)


@router.post("/chat/agent/{agent_id}/sessions")
async def new_agent_session(agent_id: str, request: Request):
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
        "crumbs": build_chat_crumbs(data.get("title")),
    })


@router.post("/chat/{sid}/message")
async def post_message(sid: str, request: Request):
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


class RenderedReply(BaseModel):
    text: str
    html: str


@router.get("/chat/{sid}/rendered-reply")
async def get_rendered_reply(sid: str) -> RenderedReply:
    """The client swaps only when `text` equals what it streamed — never a stale one."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    text = _store.read_last_assistant_text(sid)
    return RenderedReply(text=text, html=str(render_markdown(text)))


@router.get("/chat/{sid}/messages")
async def get_messages(sid: str):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(_store.load(sid))
