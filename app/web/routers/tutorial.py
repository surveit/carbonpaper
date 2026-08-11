"""Router for the home zero state's tour control: opens a chat session bound to the
"tutorial" agent, then redirects to that session's chat page, where the generic chat
surface takes over.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.core.agent.session import create_agent_session

router = APIRouter()


@router.post("/tutorial")
async def open_tutorial_session(request: Request):
    # Every link the tour quotes is built from this base_url.
    sid = create_agent_session(
        "tutorial", {"base_url": str(request.base_url)}, title="Guided tour"
    )
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)
