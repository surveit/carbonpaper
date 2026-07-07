"""Adapt the project editing tools into an in-process claude_agent_sdk MCP
server, so the subscription CLI (which runs its own agent loop in a subprocess)
can call them. The tools themselves are unchanged; this is pure interface.

`build_project_mcp_server` also returns the wrapped `SdkMcpTool` list: the
`McpSdkServerConfig` is a plain dict whose `instance` (an `mcp.server.Server`)
exposes no public list of registered tools, so returning the list is the only
non-private way to reach a handler (used by tests to invoke a tool without a
CLI subprocess)."""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Annotated, Any, Callable

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)

from app.chat.project_tools import make_project_tools

# Input schemas keyed by tool __name__, verified against
# app/chat/project_tools.py::make_project_tools. Each parameter carries an
# `Annotated[type, "description"]` so the model knows what to pass — the SDK
# turns these into the JSON Schema the CLI sees. Empty dict = no parameters.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_projects": {},
    "describe_workflow": {},
    "read_stage": {
        "stage_id": Annotated[str, "The stage's id, as shown by describe_workflow."],
    },
    "edit_stage": {
        "stage_id": Annotated[str, "The id of the stage to change."],
        "changes_json": Annotated[
            str,
            "A JSON object (encoded as a string) of ONLY the fields to change — a "
            "JSON Merge Patch. Fields you omit are preserved verbatim; a null value "
            "deletes a field. Nested objects merge (they are not replaced whole). "
            'Examples: {"limit": 100} sets limit; {"llm": {"model": "opus"}} '
            "changes only llm.model. You cannot change a stage's id this way.",
        ],
    },
    "create_version": {
        "message": Annotated[str, "A short note describing this snapshot."],
    },
    "fetch_document": {
        "src_path": Annotated[str, "Path to a LOCAL file on disk (not a URL)."],
    },
    "read_section": {
        "doc_path": Annotated[str, "On-disk document path returned by fetch_document."],
        "heading": Annotated[str, "Text to match against a markdown heading; returns that section."],
    },
    "grep_doc": {
        "doc_path": Annotated[str, "On-disk document path returned by fetch_document."],
        "query": Annotated[str, "Case-insensitive substring to find; returns matching numbered lines."],
    },
    "compile_workflow": {
        "doc_path": Annotated[str, "On-disk source document path (from fetch_document) to compile."],
        "confirm_overwrite": Annotated[
            bool,
            "Set true to snapshot-and-overwrite when the workflow already has "
            "reviewed stages; omit or false otherwise.",
        ],
    },
}


# Present-tense labels shown in the chat while a tool runs (e.g. "Reading the
# workflow…"), keyed by the bare tool name. The full args/result stay available
# behind a click-to-expand disclosure in the UI. "ToolSearch" is the CLI's own
# built-in that loads a deferred MCP tool's schema before first use.
TOOL_LABELS: dict[str, str] = {
    "list_projects": "Listing projects",
    "describe_workflow": "Reading the workflow",
    "read_stage": "Reading a stage",
    "edit_stage": "Editing a stage",
    "create_version": "Snapshotting a version",
    "fetch_document": "Opening the source document",
    "read_section": "Reading a document section",
    "grep_doc": "Searching the document",
    "compile_workflow": "Compiling the workflow",
    "ToolSearch": "Looking up a tool",
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

    # The full docstring is the model-facing description — it carries the usage
    # guidance (read before edit, pass the full stage JSON, id must match). Using
    # only the first line dropped exactly what the model needs.
    description = inspect.getdoc(fn) or name
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
