"""FastAPI routes for the chat subsystem -- generic and agent-agnostic: a session
binds an `agent_id` to an opaque `context`, and a turn rebuilds the engine from
the registry (app.core.agent.registry) rather than knowing any concrete agent.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from starlette.concurrency import run_in_threadpool

from app.core.llm_sdk import CLI_PATH
from app.services import project as project_service
from app.services.project import ProjectListing
from app.services.project_record import read_project_name
from app.core.errors import FileOverCeiling, StoreOverQuota
from app.core.files import max_upload_bytes, save_upload
from app.web.file_sizes import describe_attachment, describe_refusal

from app.core.agent import registry
from app.core.agent.session import build_session_engine, create_agent_session
from app.core.agent.store import (
    Bubble,
    MessageRole,
    Offer,
    OffersBlock,
    ProseBlock,
    ToolBlock,
    open_session_store,
)
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


def read_chat_context(request: Request) -> dict:
    """Both draft routes build the context the same way, so a chat opens the same either way."""
    context = dict(request.query_params) | {"base_url": str(request.base_url)}
    if "project_id" not in context:
        found = _read_project_on_page(context.get("opened_on", ""))
        if found:
            context["project_id"] = found
    return context


def _read_project_on_page(page: str) -> str | None:
    # Every project page is /project/<id>/…, so a chat started on one opens bound to it.
    parts = urlparse(page).path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "project":
        return None
    return parts[1] if project_service.project_exists(parts[1]) else None


def _backend_error() -> str | None:
    if CLI_PATH is not None:
        return None
    return (
        "The Claude CLI / Agent SDK isn't available. Install it and run "
        "`claude login` so the agent can run."
    )


class ChatPanelConfig(BaseModel):
    """Everything the panel's client needs before it can mount. See static/chat-panel.js."""

    title: str | None
    session_id: str | None
    # None until the agent settles one; that is what makes an attachment ask where it goes.
    session_project: str | None
    projects: list[ProjectListing]
    max_upload_bytes: int
    active_turn: str | None
    # A draft page stores nothing until the reader replies; these three materialize it.
    draft_agent_id: str | None = None
    draft_context: dict[str, str] | None = None
    # Sent by the panel on mount, as the reader's own first message.
    opening_message: str | None = None


def _read_panel_context(sid: str, data: dict) -> dict:
    """The panel renders identically wherever it is drawn, so both hosts read this."""
    return {
        "history": _store.history_view(sid),
        "pending_user": data.get("pending_user"),
        # No bound agent: post_message 400s, so there is no composer to draw.
        "view_only": data.get("agent_id") is None,
        "backend_error": _backend_error(),
        "config": ChatPanelConfig(
            title=data.get("title"),
            session_id=sid,
            session_project=(data.get("context") or {}).get("project_id"),
            projects=project_service.list_project_listings(),
            max_upload_bytes=max_upload_bytes(),
            active_turn=data.get("active_turn"),
        ),
    }


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
        "crumbs": build_home_crumbs("Chats"),
    })


def _draft_opening_bubble(opening: registry.OpeningTurn) -> Bubble:
    """Mirrors what create_agent_session stores, so a draft reads like the session it becomes."""
    blocks: list[ProseBlock | ToolBlock | OffersBlock] = [
        ProseBlock(kind="text", text=opening.text)]
    if opening.offers:
        blocks.append(OffersBlock(options=[Offer(text=t) for t in opening.offers]))
    return Bubble(role=MessageRole.assistant, blocks=blocks)


def _draft_title(agent_id: str, context: dict) -> str:
    """The agent says what it is called; app.web holds no agent's name. registry.display_name."""
    name = registry.read_display_name(agent_id)
    project_id = context.get("project_id")
    return f"{name}: {project_id}" if project_id else name


def _read_draft_panel_context(agent_id: str, context: dict) -> dict:
    """A conversation that is only on screen: nothing is stored until the reader replies."""
    opening = registry.render_opening_turn(agent_id, context)
    return {
        "history": [_draft_opening_bubble(opening)] if opening and opening.text else [],
        "pending_user": None,
        "view_only": False,
        "backend_error": _backend_error(),
        "config": ChatPanelConfig(
            title=_draft_title(agent_id, context),
            session_id=None,
            session_project=context.get("project_id"),
            projects=project_service.list_project_listings(),
            max_upload_bytes=max_upload_bytes(),
            active_turn=None,
            draft_agent_id=agent_id,
            draft_context=context,
            opening_message=context.get("task"),
        ),
    }


@router.get("/chat/agent/{agent_id}/new", response_class=HTMLResponse)
async def draft_agent_chat(agent_id: str, request: Request):
    """Visiting creates nothing; the composer materializes on the first reply."""
    if not registry.is_registered(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent")
    context = read_chat_context(request)
    title = _draft_title(agent_id, context)
    return templates.TemplateResponse(request, "chat.html", {
        **_read_draft_panel_context(agent_id, context),
        "title": title,
        "crumbs": build_chat_crumbs(title),
    })


@router.get("/chat/agent/{agent_id}/new/panel", response_class=HTMLResponse)
async def new_chat_panel(agent_id: str, request: Request):
    """The draft page's panel with no page around it, for a host that has its own."""
    if not registry.is_registered(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent")
    context = read_chat_context(request)
    return templates.TemplateResponse(
        request, "_chat_panel.html", _read_draft_panel_context(agent_id, context))


@router.post("/chat/agent/{agent_id}/sessions")
async def new_agent_session(agent_id: str, request: Request):
    """Draft page -> real, stored session. See ensureSession() in chat.html."""
    if not registry.is_registered(agent_id):
        raise HTTPException(status_code=404, detail="Unknown agent")
    body = await request.json()
    context = (body or {}).get("context") or {}
    title = (body or {}).get("title")
    sid = create_agent_session(
        agent_id, context, base_url=str(request.base_url), title=title)
    return JSONResponse({"ok": True, "sid": sid})


@router.get("/chat/{sid}", response_class=HTMLResponse)
async def chat_page(request: Request, sid: str):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    data = _store.load(sid)
    return templates.TemplateResponse(request, "chat.html", {
        **_read_panel_context(sid, data),
        "title": data.get("title"),
        "crumbs": build_chat_crumbs(data.get("title")),
    })


@router.get("/chat/{sid}/panel", response_class=HTMLResponse)
async def chat_panel(request: Request, sid: str):
    """The panel with no page around it, for a host that has its own. static/chat-rail.js."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return templates.TemplateResponse(
        request, "_chat_panel.html", _read_panel_context(sid, _store.load(sid)))


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
    return JSONResponse({"ok": True, "file_id": record.id, "filename": record.filename,
                         "bytes": record.byte_count, "project_id": record.project_id,
                         "line": describe_attachment(
                             record, read_project_name(record.project_id)
                             if record.project_id else "")})


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
    engine = build_session_engine(
        sid, str(request.base_url), page=(body or {}).get("page"))
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


class RenderMarkdownRequest(BaseModel):
    text: str


class RenderMarkdownReply(BaseModel):
    html: str


@router.post("/chat/{sid}/render-markdown")
async def render_markdown_text(sid: str, body: RenderMarkdownRequest) -> RenderMarkdownReply:
    """Renders one already-complete text block, for a client re-rendering it mid-turn."""
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return RenderMarkdownReply(html=str(render_markdown(body.text)))


@router.get("/chat/{sid}/messages")
async def get_messages(sid: str):
    if not _store.exists(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(_store.load(sid))
