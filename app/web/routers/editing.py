"""Router for the 'Edit with agent' control: opens a chat session bound to the
"editing" agent with the project as its context, then redirects to that
session's chat page, where the generic chat surface takes over.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.core.agent.session import create_agent_session
from app.runtime.observation import profile_input_stage
from app.services import observation

router = APIRouter()

# The observation seam for the editing agent's list_distinct_values tool: the
# services layer must not import app.runtime, so this router — part of app.web,
# a composition root the import contracts allow to import the runtime — injects
# the frame profiler at import time. See app.services.observation.
observation.set_input_profiler(profile_input_stage)


@router.post("/project/{name}/edit-agent")
async def open_editing_session(name: str):
    """Open an editing-agent chat session for project `name` and redirect to it."""
    sid = create_agent_session("editing", {"project_id": name}, title=f"Editing: {name}")
    return RedirectResponse(url=f"/chat/{sid}", status_code=303)
