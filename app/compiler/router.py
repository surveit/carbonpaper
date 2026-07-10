"""The compiler subsystem's web entry for chat-driven editing.

The 'Edit with agent' control on a project posts here; this opens a chat session
bound to the "editing" agent, carrying the project as its context, and redirects
the browser to that session's chat page. The generic chat surface (app.agent)
takes over from there."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.agent.router import create_agent_session

router = APIRouter()


@router.post("/project/{name}/edit-agent")
async def open_editing_session(name: str):
    """Open an editing-agent chat session for project `name` and redirect to it."""
    sid = create_agent_session("editing", {"project_id": name}, title=f"Editing: {name}")
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)
