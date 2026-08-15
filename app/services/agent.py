"""URLs for a chat with one of the registered agents, stated once so 'Edit with agent'
and the tour's handoff link agree on what they open.

Neither call creates anything: the link opens a draft page, and the reader's first
reply is what materializes a stored session — see ensureSession() in chat.html."""
from __future__ import annotations

from urllib.parse import urlencode


def open_agent_chat(agent_id: str, project_id: str) -> str:
    """Returns the draft page, root-relative: a caller with a base URL prefixes it."""
    return f"/chat/agent/{agent_id}/new?{urlencode({'project_id': project_id})}"


def open_unbound_agent_chat(agent_id: str) -> str:
    """No project in context: the agent asks which one, or makes one with create_project."""
    return f"/chat/agent/{agent_id}/new"
