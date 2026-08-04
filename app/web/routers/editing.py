"""Router for the 'Edit with agent' control: opens a chat session bound to the
"editing" agent with the project as its context, then redirects to that
session's chat page, where the generic chat surface takes over.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.core.agent.session import create_agent_session

router = APIRouter()


@router.post("/project/{name}/edit-agent")
async def open_editing_session(name: str):
    """Open an editing-agent chat session for project `name` and redirect to it."""
    sid = create_agent_session("editing", {"project_id": name}, title=f"Editing: {name}")
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)
