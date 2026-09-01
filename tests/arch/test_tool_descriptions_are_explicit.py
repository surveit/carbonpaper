"""What a model reads about a tool must be an explicit argument, never a docstring.

A docstring silently becoming a prompt means a docstring edit is a prompt change no
reviewer reads as one, and the 100-char docstring ceiling would quietly truncate it.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.tools.editing import TOOL_LABELS, TOOL_SCHEMAS
from app.tools.tool_specs import (
    AGENT_TOOLS,
    SURFACE_TOOL_DESCRIPTIONS,
    bind,
    find_tool_names,
    read_tool_description,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_SERVER = _REPO_ROOT / "app/mcp/server.py"
_EDITING_TOOLS = _REPO_ROOT / "app/tools/editing.py"


def find_mcp_tools(path: Path) -> list[tuple[str, ast.expr | None, str | None]]:
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
        if isinstance(n, ast.FunctionDef) and n.name == "build_editing_tools"
    )
    return [n.name for n in maker.body if isinstance(n, ast.FunctionDef)]


def find_docstringed_editing_tools(path: Path) -> list[str]:
    maker = next(
        n for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "build_editing_tools"
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
        f"is documented — add an entry to app/tools/tool_specs.py for: {undeclared}"
    )


def test_no_mcp_tool_carries_a_docstring() -> None:
    documented = [name for name, _, doc in find_mcp_tools(_MCP_SERVER) if doc is not None]
    assert not documented, (
        "a docstring on an MCP tool reads as the model-facing description even when it "
        f"is not one — move it to app/tools/tool_specs.py: {documented}"
    )


def test_mcp_descriptions_cover_exactly_the_registered_tools() -> None:
    registered = {name for name, _, _ in find_mcp_tools(_MCP_SERVER)}
    assert registered - find_tool_names() == set()
    # Spec entries the other surface owns and this one deliberately lacks:
    # `sleep` exists for in-process agents, which run with the CLI's built-ins disabled —
    # the CLI client connecting HERE brings its own. The list may shrink; a new name on it
    # is the two surfaces diverging again, which is what this file exists to catch.
    assert find_tool_names() - registered <= {"get_current_url", "sleep"}, (
        "get_current_url is a fact about a reader sitting in a browser — which page they "
        "have open — and an MCP client is not one."
    )


def test_a_tool_carries_its_body_here_or_on_its_surface_never_both() -> None:
    both = set(AGENT_TOOLS) & set(SURFACE_TOOL_DESCRIPTIONS)
    assert not both, (
        "a name in both tables makes read_tool_description's answer depend on which "
        f"table wins, and the two can disagree: {sorted(both)}"
    )


def test_every_agent_tool_binds_to_the_body_it_names() -> None:
    bound = {spec.name for spec in bind(*AGENT_TOOLS)}
    # bind_by_signature raises on parameter prose the fn does not take, so this is
    # what catches prose left behind by a changed signature.
    assert bound == set(AGENT_TOOLS)


def test_no_editing_tool_carries_a_docstring() -> None:
    documented = find_docstringed_editing_tools(_EDITING_TOOLS)
    assert not documented, (
        "an editing tool's docstring is not what the model reads — the shared registry "
        "in app/tools/tool_specs.py is; "
        f"a docstring here would drift from it unnoticed: {documented}"
    )


def test_editing_tool_registries_cover_exactly_the_tools() -> None:
    tools = set(find_editing_tool_names(_EDITING_TOOLS))
    assert find_tool_names() >= tools
    assert set(TOOL_SCHEMAS) == tools
    assert tools <= set(TOOL_LABELS)


def test_every_description_is_non_empty() -> None:
    blank = [name for name in find_tool_names() if not read_tool_description(name).strip()]
    assert not blank
