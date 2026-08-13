"""Opening a chat with one of the registered agents on a project.

Both the 'Edit with agent' control and the tour's handoff link must land the reader in
the same conversation, so what that session IS — its agent, its context, its title and
the page it waits on — is stated once, here.
"""
from __future__ import annotations

from app.core.agent.session import create_agent_session


def open_agent_chat(agent_id: str, project_id: str) -> str:
    """Returns the new session's page, root-relative: a caller with a base URL prefixes it."""
    sid = create_agent_session(
        agent_id, {"project_id": project_id}, title=f"{agent_id.capitalize()}: {project_id}")
    return f"/chat/{sid}"


def open_unbound_agent_chat(agent_id: str) -> str:
    """No project in context: the agent asks which one, or makes one with create_project."""
    return f"/chat/{create_agent_session(agent_id, {}, title='New chat')}"
