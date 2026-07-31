"""What a model reads about a tool must be an explicit argument, never a docstring.

A docstring silently becoming a prompt means a docstring edit is a prompt change no
reviewer reads as one, and the 100-char docstring ceiling would quietly truncate it.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.agents.compiler.tools import TOOL_DESCRIPTIONS, TOOL_LABELS, TOOL_SCHEMAS
from app.mcp.tool_descriptions import TOOL_DESCRIPTIONS as MCP_TOOL_DESCRIPTIONS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_SERVER = _REPO_ROOT / "app/mcp/server.py"
_EDITING_TOOLS = _REPO_ROOT / "app/agents/compiler/tools.py"


def find_mcp_tools(path: Path) -> list[tuple[str, ast.expr | None, str | None]]:
    """Each `@mcp.tool(...)`-decorated function as (name, its `description=` value, docstring)."""
    found = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr != "tool":
                continue
            described = next(
                (kw.value for kw in dec.keywords if kw.arg == "description"), None
            )
            found.append((node.name, described, ast.get_docstring(node)))
    return found


def find_editing_tool_names(path: Path) -> list[str]:
    maker = next(
        n for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "make_editing_tools"
    )
    return [n.name for n in maker.body if isinstance(n, ast.FunctionDef)]


def find_docstringed_editing_tools(path: Path) -> list[str]:
    maker = next(
        n for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "make_editing_tools"
    )
    return [
        n.name for n in maker.body
        if isinstance(n, ast.FunctionDef) and ast.get_docstring(n) is not None
    ]


def test_every_mcp_tool_declares_its_description() -> None:
    undeclared = [name for name, described, _ in find_mcp_tools(_MCP_SERVER) if described is None]
    assert not undeclared, (
        "an @mcp.tool without an explicit `description=` falls back to the function's "
        "docstring, making the model-facing prompt a side effect of how the function "
        f"is documented — add an entry to app/mcp/tool_descriptions.py for: {undeclared}"
    )


def test_no_mcp_tool_carries_a_docstring() -> None:
    documented = [name for name, _, doc in find_mcp_tools(_MCP_SERVER) if doc is not None]
    assert not documented, (
        "a docstring on an MCP tool reads as the model-facing description even when it "
        f"is not one — move it to app/mcp/tool_descriptions.py: {documented}"
    )


def test_mcp_descriptions_cover_exactly_the_registered_tools() -> None:
    registered = {name for name, _, _ in find_mcp_tools(_MCP_SERVER)}
    assert set(MCP_TOOL_DESCRIPTIONS) == registered


def test_no_editing_tool_carries_a_docstring() -> None:
    documented = find_docstringed_editing_tools(_EDITING_TOOLS)
    assert not documented, (
        "an editing tool's docstring is not what the model reads — TOOL_DESCRIPTIONS is; "
        f"a docstring here would drift from it unnoticed: {documented}"
    )


def test_editing_tool_registries_cover_exactly_the_tools() -> None:
    tools = set(find_editing_tool_names(_EDITING_TOOLS))
    assert set(TOOL_DESCRIPTIONS) == tools
    assert set(TOOL_SCHEMAS) == tools
    assert tools <= set(TOOL_LABELS)


def test_every_description_is_non_empty() -> None:
    blank = [
        name
        for registry in (MCP_TOOL_DESCRIPTIONS, TOOL_DESCRIPTIONS)
        for name, text in registry.items()
        if not text.strip()
    ]
    assert not blank
