"""
edit_tools.py — in-process MCP EDIT-TOOLS for the DATA MODEL.

These are the tools the authoring AI calls to make TARGETED edits to existing
named schemas during a Phase-1 (data-model) authoring turn. Each tool is a thin
wrapper over one app.staging mutator, with the project working copy bound by
closure. Crucially the tools STAGE edits (into <project_dir>/data_model_staging.json)
for a human to review and Save — they NEVER write schema files directly. Adding a
brand-new table is NOT done here; that stays a ```schema fenced block the compiler
persists (see app/compiler/chat.py). The system prompt tells the model this split.

WHY IN-PROCESS (SDK MCP): create_sdk_mcp_server runs the tools inside this Python
process (no subprocess/IPC), so a tool has direct access to app.staging and the
project dir. The tools are exposed to the model as
`mcp__methodology_edit__<toolname>` and MUST be listed in
ClaudeAgentOptions.allowed_tools (app.compiler.chat does that when
enable_edit_tools).

CARDINAL RULE (fail loud, never silent): every tool calls a mutator that RAISES
(staging.StagingError) when its target schema/column does not exist. The tool
catches that and returns an ERROR result (is_error=True) whose text names exactly
what was missing — so the MODEL sees the failure and can correct, rather than the
edit silently no-op'ing. There is no fallback that pretends success.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from app import staging

# The server name; tools are exposed to the model as mcp__methodology_edit__<name>.
# "methodology_edit" is the domain sense — editing the methodology's data model —
# not the container (which is a project); it names what the tools change, and the
# system-prompt guidance references the same route prefix.
MCP_SERVER_NAME = "methodology_edit"


def _ok(text: str) -> dict[str, Any]:
    """A successful tool result: one text block the model reads as confirmation."""
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    """A LOUD tool error result: is_error=True so the model sees a failure, plus the
    text explaining exactly what was wrong (the staging mutator's message). This is
    the fail-loud surface — never a silent success on a bad target."""
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _confirm(project_dir: str | Path, schema_name: str, did: str) -> dict[str, Any]:
    """Standard success text: what was staged + the current one-line staged-vs-disk
    diff for that schema, so the model (and the chat transcript) see the effect. The
    'staged for human review' phrasing keeps the model aware these are proposals."""
    summary = staging.diff_summary_line(project_dir, schema_name)
    return _ok(f"Staged (for human review — not written to disk): {did}. {summary}")


def build_edit_tools(project_dir: str | Path) -> list[Any]:
    """Build the list of SdkMcpTool objects (the @tool-decorated edit-tools) bound to
    ONE project working copy via closure. Split out from make_edit_tools_server so
    tests can invoke a tool's `.handler(args)` DIRECTLY (bypassing the LLM + the MCP
    transport) — the deterministic self-test relies on that. make_edit_tools_server
    wraps these into an in-process server.

    project_dir is captured by closure into every tool, so a tool call during an
    SSE stream stages into THIS project's data_model_staging.json."""

    # ── One @tool per staging mutator. input_schema maps param -> Python type; the
    #    SDK turns it into the JSON schema the model sees. Handlers are async (the
    #    SDK awaits them) but the staging calls are sync + fast. ──

    @tool(
        "set_column_type",
        "EDIT an existing column's data type in an existing named schema. Stages the "
        "change for human review; does NOT write files. Args: schema (schema name), "
        "column (column name), new_type (e.g. str, int, float, bool, datetime, date, "
        "or list[<type>]). Errors loudly if the schema or column does not exist.",
        {"schema": str, "column": str, "new_type": str},
    )
    async def set_column_type(args: dict[str, Any]) -> dict[str, Any]:
        try:
            staging.set_column_type(
                project_dir, args["schema"], args["column"], args["new_type"]
            )
        except staging.StagingError as exc:
            return _err(f"Edit refused: {exc}")
        return _confirm(
            project_dir, args["schema"],
            f"set {args['schema']}.{args['column']} type to '{args['new_type']}'",
        )

    @tool(
        "add_column",
        "ADD a new column to an existing named schema. Stages the change for human "
        "review; does NOT write files. Use this to add a field to a table that "
        "already exists — NOT to create a whole new table (emit a ```schema block for "
        "that). Args: schema, column (new column name), type (default str), nullable "
        "(default true), description (optional), references (optional 'other.col'). "
        "Errors loudly if the schema is missing or the column already exists.",
        {
            "schema": str, "column": str, "type": str, "nullable": bool,
            "description": str, "references": str,
        },
    )
    async def add_column(args: dict[str, Any]) -> dict[str, Any]:
        # Optional args: only pass through what the model provided, so defaults in the
        # mutator apply and we never fabricate a description/reference.
        kwargs: dict[str, Any] = {}
        if args.get("type") is not None:
            kwargs["type"] = args["type"]
        if args.get("nullable") is not None:
            kwargs["nullable"] = args["nullable"]
        if args.get("description") is not None:
            kwargs["description"] = args["description"]
        if args.get("references") is not None:
            kwargs["references"] = args["references"]
        try:
            staging.add_column(project_dir, args["schema"], args["column"], **kwargs)
        except staging.StagingError as exc:
            return _err(f"Edit refused: {exc}")
        return _confirm(
            project_dir, args["schema"],
            f"added column '{args['column']}' to {args['schema']}",
        )

    @tool(
        "remove_column",
        "REMOVE a column from an existing named schema (also drops it from the "
        "primary key if present). Stages the change for human review; does NOT write "
        "files. Args: schema, column. Errors loudly if the schema or column does not "
        "exist.",
        {"schema": str, "column": str},
    )
    async def remove_column(args: dict[str, Any]) -> dict[str, Any]:
        try:
            staging.remove_column(project_dir, args["schema"], args["column"])
        except staging.StagingError as exc:
            return _err(f"Edit refused: {exc}")
        return _confirm(
            project_dir, args["schema"],
            f"removed column '{args['column']}' from {args['schema']}",
        )

    @tool(
        "rename_column",
        "RENAME a column in an existing named schema (updates the primary key "
        "membership too). Stages the change for human review; does NOT write files. "
        "Args: schema, old_name, new_name. Errors loudly if the schema or old_name "
        "does not exist, or if new_name already names another column.",
        {"schema": str, "old_name": str, "new_name": str},
    )
    async def rename_column(args: dict[str, Any]) -> dict[str, Any]:
        try:
            staging.rename_column(
                project_dir, args["schema"], args["old_name"], args["new_name"]
            )
        except staging.StagingError as exc:
            return _err(f"Edit refused: {exc}")
        return _confirm(
            project_dir, args["schema"],
            f"renamed {args['schema']}.{args['old_name']} -> '{args['new_name']}'",
        )

    @tool(
        "set_column_description",
        "SET (or replace) the description of an existing column in an existing named "
        "schema. Stages the change for human review; does NOT write files. Args: "
        "schema, column, description. Errors loudly if the schema or column does not "
        "exist.",
        {"schema": str, "column": str, "description": str},
    )
    async def set_column_description(args: dict[str, Any]) -> dict[str, Any]:
        try:
            staging.set_column_description(
                project_dir, args["schema"], args["column"], args["description"]
            )
        except staging.StagingError as exc:
            return _err(f"Edit refused: {exc}")
        return _confirm(
            project_dir, args["schema"],
            f"set description of {args['schema']}.{args['column']}",
        )

    @tool(
        "set_schema_description",
        "SET (or replace) the schema-level description of an existing named schema. "
        "Stages the change for human review; does NOT write files. Args: schema, "
        "description. Errors loudly if the schema does not exist.",
        {"schema": str, "description": str},
    )
    async def set_schema_description(args: dict[str, Any]) -> dict[str, Any]:
        try:
            staging.set_schema_description(
                project_dir, args["schema"], args["description"]
            )
        except staging.StagingError as exc:
            return _err(f"Edit refused: {exc}")
        return _confirm(
            project_dir, args["schema"],
            f"set description of schema '{args['schema']}'",
        )

    @tool(
        "set_primary_key",
        "SET the primary key of an existing named schema to a list of its column "
        "names. Stages the change for human review; does NOT write files. Args: "
        "schema, primary_key (a list of column-name strings). Errors loudly if the "
        "schema is missing or any named column is not declared on the schema.",
        {"schema": str, "primary_key": list},
    )
    async def set_primary_key(args: dict[str, Any]) -> dict[str, Any]:
        pk = args.get("primary_key")
        if not isinstance(pk, list):
            return _err(
                "Edit refused: primary_key must be a list of column-name strings, "
                f"got {type(pk).__name__}"
            )
        try:
            staging.set_primary_key(project_dir, args["schema"], pk)
        except staging.StagingError as exc:
            return _err(f"Edit refused: {exc}")
        return _confirm(
            project_dir, args["schema"],
            f"set primary_key of '{args['schema']}' to {pk}",
        )

    return [
        set_column_type,
        add_column,
        remove_column,
        rename_column,
        set_column_description,
        set_schema_description,
        set_primary_key,
    ]


def tool_names_for(tools: list[Any]) -> list[str]:
    """The fully-qualified names the model sees + must be allow-listed, e.g.
    ["mcp__methodology_edit__set_column_type", ...]. The @tool decorator stores the
    short name on each SdkMcpTool; we prefix with the server route."""
    return [f"mcp__{MCP_SERVER_NAME}__{t.name}" for t in tools]


def make_edit_tools_server(project_dir: str | Path) -> tuple[Any, list[str]]:
    """Build the in-process MCP server exposing the data-model edit-tools bound to
    ONE project working copy (examples/<name>/). Returns (server, tool_names) where
    tool_names are the fully-qualified names to put in allowed_tools, i.e.
    ["mcp__methodology_edit__set_column_type", ...].

    One server is built per stream (cheap, in-process). Use build_edit_tools if you
    need the raw SdkMcpTool objects (e.g. to call a handler directly in a test)."""
    tools = build_edit_tools(project_dir)
    server = create_sdk_mcp_server(MCP_SERVER_NAME, tools=tools)
    return server, tool_names_for(tools)


__all__ = [
    "MCP_SERVER_NAME",
    "build_edit_tools",
    "tool_names_for",
    "make_edit_tools_server",
]
