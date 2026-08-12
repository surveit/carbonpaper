"""Router for the 'Edit with agent' control: opens a chat session bound to the
"editing" agent with the project as its context, then redirects to that
session's chat page, where the generic chat surface takes over.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.services.agent import open_agent_chat

router = APIRouter()


@router.post("/project/{name}/edit-agent")
async def open_editing_session(name: str):
    return RedirectResponse(url=open_agent_chat("editing", name), status_code=303)
