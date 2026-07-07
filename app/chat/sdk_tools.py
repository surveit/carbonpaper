"""Adapt the project editing tools into an in-process claude_agent_sdk MCP
server, so the subscription CLI (which runs its own agent loop in a subprocess)
can call them. The tools themselves are unchanged; this is pure interface.

`build_project_mcp_server` also returns the wrapped `SdkMcpTool` list: the
`McpSdkServerConfig` is a plain dict whose `instance` (an `mcp.server.Server`)
exposes no public list of registered tools, so returning the list is the only
non-private way to reach a handler (used by tests to invoke a tool without a
CLI subprocess)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)

from app.chat.project_tools import make_project_tools

# Exact input schemas, keyed by tool __name__ (see the plan's signature table,
# verified against app/chat/project_tools.py::make_project_tools). Empty dict =
# no parameters.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_projects": {},
    "describe_workflow": {},
    "read_stage": {"stage_id": str},
    "edit_stage": {"stage_id": str, "spec_json": str},
    "create_version": {"message": str},
    "fetch_document": {"src_path": str},
    "read_section": {"doc_path": str, "heading": str},
    "grep_doc": {"doc_path": str, "query": str},
    "compile_workflow": {"doc_path": str, "confirm_overwrite": bool},
}


def _as_content(value: object) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, default=str, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _wrap(fn: Callable[..., Any]) -> SdkMcpTool[Any]:
    name = fn.__name__
    schema = TOOL_SCHEMAS[name]

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _as_content(fn(**args))
        except Exception as exc:  # surface loudly to the model, never a default
            return {
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "is_error": True,
            }

    description = (fn.__doc__ or name).strip().split("\n")[0]
    return tool(name, description, schema)(handler)


def build_project_mcp_server(
    name: str, *, examples_dir: Path
) -> tuple[McpSdkServerConfig, list[str], list[SdkMcpTool[Any]]]:
    """Wrap the 9 project tools as an in-process SDK-MCP server.

    Returns `(server, allowed_tool_names, wrapped_tools)`:
    - `server` goes into `ClaudeAgentOptions.mcp_servers`;
    - each allowed name is `f"mcp__project__{fn.__name__}"`;
    - `wrapped_tools` is the `SdkMcpTool` list (the server dict exposes no public
      accessor for it) so callers can invoke a handler directly.
    """
    callables = make_project_tools(name, examples_dir=examples_dir)
    wrapped = [_wrap(fn) for fn in callables]
    server = create_sdk_mcp_server("project", tools=wrapped)
    allowed = [f"mcp__project__{fn.__name__}" for fn in callables]
    return server, allowed, wrapped
