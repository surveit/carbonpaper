"""The MCP authoring surface ("glassbox"): the name-based service surface exposed to
external MCP clients. No tool here approves anything — humans approve in the web UI.

Mounted by app.main at /mcp in the same server process: same event loop, which
the generation live-turns require."""
from app.mcp.server import mcp as mcp
