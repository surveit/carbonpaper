"""The MCP authoring surface: the same name-based service surface the in-app
editing agent uses, exposed as a streamable-HTTP MCP server ("sift") so an
EXTERNAL agent (Claude Code, or any MCP client) can author projects. Humans
review and approve in the web UI — no tool here approves anything.

Mounted by app.main at /mcp in the same server process: same event loop, which
the generation live-turns require."""
from app.mcp.server import mcp

__all__ = ["mcp"]
