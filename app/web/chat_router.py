"""FastAPI routes for the chat subsystem -- generic and agent-agnostic: a session
binds an `agent_id` to an opaque `context`, and a turn rebuilds the engine from
the registry (app.core.agent.registry) rather than knowing any concrete agent.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from starlette.concurrency import run_in_threadpool

from app.core.llm_sdk import CLI_PATH
from app.services import project as project_service
from app.services.errors import FileOverCeiling, StoreOverQuota
from app.services.uploads import max_upload_bytes, save_upload
from app.web.file_sizes import describe_attachment, describe_refusal

from app.core.agent.registry import build_engine, opening_prompt
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
        "opens_itself": _has_unspoken_opening(data),
        "backend": _backend_label(),
        "backend_error": _backend_error(),
        "crumbs": build_chat_crumbs(data.get("title")),
        # What an attached file needs to know before it is sent: the ceiling, the
        # project this session works on (None until the agent settles one), and the
        # projects it could be given to.
        "session_project": (data.get("context") or {}).get("project_id"),
        "projects": [p.model_dump() for p in project_service.list_project_listings()],
        "max_upload_bytes": max_upload_bytes(),
    })


@router.post("/chat/{sid}/files")
async def upload_chat_file(sid: str, file: UploadFile = File(...),
                           project_id: str = Form("")):
    """Blank `project_id` leaves the file unclaimed; the agent gives it a home later."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    if not file.filename:
        return JSONResponse({"ok": False, "error": "no file provided"}, status_code=400)
    try:
        record = await run_in_threadpool(
            save_upload, file.filename, file.file, project_id.strip() or None)
    except (FileOverCeiling, StoreOverQuota) as exc:
        return JSONResponse({"ok": False, "error": describe_refusal(exc)}, status_code=400)
    # The line the conversation carries. The agent never sees the bytes and never sees
    # this page — it sees the next turn's text, so what the reader is shown and what the
    # agent is told have to be the same sentence.
    return JSONResponse({"ok": True, "sha256": record.sha256, "filename": record.filename,
                         "bytes": record.byte_count, "project_id": record.project_id,
                         "line": describe_attachment(
                             record, project_service.read_project_name(record.project_id)
                             if record.project_id else "")})


def _has_unspoken_opening(data: dict) -> bool:
    """True when this page must start the agent's opening turn on load."""
    agent_id = data.get("agent_id")
    if agent_id is None or data.get("messages") or data.get("active_turn"):
        return False
    return opening_prompt(agent_id) is not None


@router.post("/chat/{sid}/open")
async def open_conversation(sid: str):
    """409s once the session has spoken, so a reload cannot make it greet twice."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    data = _store.load(sid)
    if not _has_unspoken_opening(data):
        raise HTTPException(status_code=409, detail="session has already opened")
    agent_id = data["agent_id"]
    prompt = opening_prompt(agent_id)
    assert prompt is not None  # _has_unspoken_opening checked it
    engine = build_engine(agent_id, data.get("context") or {})
    turn_id = _turns.start(
        engine=engine, store=_store, session_id=sid, prompt=prompt, record_prompt=False
    )
    return JSONResponse({"ok": True, "turn_id": turn_id})


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


class RenderedSegment(BaseModel):
    text: str
    html: str


class RenderedReply(BaseModel):
    segments: list[RenderedSegment]


@router.get("/chat/{sid}/rendered-reply")
async def get_rendered_reply(sid: str) -> RenderedReply:
    """One segment per text block of the reply, in order; the client swaps only on an exact match."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return RenderedReply(segments=[
        RenderedSegment(text=text, html=str(render_markdown(text)))
        for text in _store.read_last_reply_texts(sid)
    ])


@router.get("/chat/{sid}/messages")
async def get_messages(sid: str):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(_store.load(sid))
