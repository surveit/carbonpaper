"""Opening the editing agent on a project.

The 'Edit with agent' control and the tour's handoff link must land the reader in the
same conversation, so what that session IS — its agent, its context, its title and the
page it waits on — is stated once, here.
"""
from __future__ import annotations

from app.core.agent.session import create_agent_session


def open_editing_chat(project: str) -> str:
    """Returns the new session's page, root-relative: a caller with a base URL prefixes it."""
    sid = create_agent_session(
        "editing", {"project_id": project}, title=f"Editing: {project}")
    return f"/chat/{sid}"
