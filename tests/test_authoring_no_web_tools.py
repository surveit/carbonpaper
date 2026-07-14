"""Invariant 1 of issue #100: NO code-authoring surface may be granted a
web/network tool.

The three surfaces that turn intent into workflow/code — the compiler, the
generation/editing agent, and the human-edit endpoint (which drives the same
editing agent) — must run with a tool allow-list that contains no way to reach
the network. If one of them could fetch untrusted web content, that content could
steer the code it authors: the 'lethal trifecta' (untrusted input + private data
+ code execution). These tests are the tripwire that fails the build if a
web/network tool is ever added to any of those allow-lists.
"""
from __future__ import annotations

# A tool is a web/network (or shell) tool if its name contains any of these
# tokens (case-insensitive). Covers the Claude SDK built-ins (WebSearch,
# WebFetch) and the general shape of anything that could reach the network or
# shell out. Deliberately broad — an authoring surface should carry none of them.
_FORBIDDEN_TOKENS = (
    "web", "fetch", "search", "http", "url", "curl", "wget",
    "network", "browser", "bash", "shell", "exec", "socket", "download",
)


def _bare_tool_name(name: str) -> str:
    """Strip the `mcp__<server>__` prefix so we check the actual tool name, not
    the in-process MCP server name."""
    if name.startswith("mcp__"):
        return name.split("__", 2)[-1]
    return name


def _web_tokens_in(name: str) -> list[str]:
    low = _bare_tool_name(name).lower()
    return [tok for tok in _FORBIDDEN_TOKENS if tok in low]


def test_compiler_grants_no_tools_at_all():
    """The compiler distils prose → workflow with a single no-tools completion.
    Its allow-list must stay empty (and therefore free of any web tool)."""
    from app.compiler.compiler import COMPILER_ALLOWED_TOOLS

    assert COMPILER_ALLOWED_TOOLS == [], (
        "the compiler must be granted NO tools; found: " f"{COMPILER_ALLOWED_TOOLS}"
    )
    for name in COMPILER_ALLOWED_TOOLS:
        assert not _web_tokens_in(name), f"compiler allow-list has a web tool: {name}"


def test_editing_agent_allow_list_has_no_web_tool():
    """The generation/editing agent (also the engine behind the human-edit chat
    endpoint) must be built with an allow-list of only its in-process workflow
    tools — never a web/network tool."""
    import app.compiler.agent.config  # noqa: F401 — import registers the "editing" agent
    from app.agent.registry import build_engine

    engine = build_engine("editing", {"project_id": "demo"})
    allowed = list(engine._allowed_tools)

    assert allowed, "editing agent should expose its in-process tools"
    offenders = {name: _web_tokens_in(name) for name in allowed if _web_tokens_in(name)}
    assert not offenders, f"editing agent allow-list contains web/network tool(s): {offenders}"


def test_editing_tool_definitions_have_no_web_tool():
    """Second layer, at the source of truth: the tool callables the editing agent
    is built from, and their declared schemas/labels, name no web tool."""
    from app.compiler.agent.tools import (
        TOOL_LABELS,
        TOOL_SCHEMAS,
        EditingContext,
        make_editing_tools,
    )

    tool_names = [fn.__name__ for fn in make_editing_tools(EditingContext(project_id="demo"))]
    for name in tool_names:
        assert not _web_tokens_in(name), f"editing tool is a web tool: {name}"

    # The schema keys must exactly match the built callables (no orphan web tool
    # smuggled into the schema map that the agent could be told to call).
    assert set(TOOL_SCHEMAS) == set(tool_names), (
        "TOOL_SCHEMAS keys drifted from the built tools: "
        f"{set(TOOL_SCHEMAS) ^ set(tool_names)}"
    )
    for name in TOOL_LABELS:
        # ToolSearch is the CLI's own schema-loader label, not a granted tool.
        if name == "ToolSearch":
            continue
        assert not _web_tokens_in(name), f"editing tool label names a web tool: {name}"
