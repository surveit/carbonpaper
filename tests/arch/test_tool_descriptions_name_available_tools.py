"""A tool description is prose the model reads as instruction, so every tool name in
it must be registered on the SAME surface. One shared registry (app/tools/tool_specs.py)
feeds two surfaces with different tool sets, so a pointer true on the MCP server
can be a dead instruction on the editing agent.
"""
from __future__ import annotations

import asyncio
import re

from app.tools.editing import EditingContext, build_editing_tools
from app.tools.tool_specs import find_tool_names

# Tool names are lowercase identifiers, and so are the parameter/field names the prose
# also mentions — intersecting with the known tool names is what tells them apart.
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def find_mcp_descriptions() -> dict[str, str]:
    from app.mcp.server import mcp

    return {tool.name: tool.description or "" for tool in asyncio.run(mcp.list_tools())}


def find_editing_descriptions() -> dict[str, str]:
    bound = build_editing_tools(EditingContext(project_id="any", base_url="http://reader.test/"))
    return {spec.name: spec.description for spec in bound}


# Every name that is a tool somewhere: the two surfaces plus the shared registry, so a
# spec entry dropped from both surfaces while still named in prose is still detected.
def find_known_tool_names() -> set[str]:
    return find_tool_names() | set(find_mcp_descriptions()) | set(find_editing_descriptions())


def find_names_a_description_uses(description: str, known: set[str]) -> set[str]:
    return set(_IDENTIFIER.findall(description)) & known


def find_uncallable_names(surface: dict[str, str], known: set[str]) -> dict[str, list[str]]:
    offenders = {}
    for name, description in surface.items():
        named = find_names_a_description_uses(description, known) - {name}
        missing = sorted(named - set(surface))
        if missing:
            offenders[name] = missing
    return offenders


def test_no_mcp_description_names_a_tool_that_surface_lacks() -> None:
    surface = find_mcp_descriptions()
    offenders = find_uncallable_names(surface, find_known_tool_names())
    assert not offenders, (
        "an MCP tool description tells the reader to call a tool the MCP server does "
        f"not register: {offenders} — it registers {sorted(surface)}"
    )


def test_no_editing_description_names_a_tool_that_surface_lacks() -> None:
    surface = find_editing_descriptions()
    offenders = find_uncallable_names(surface, find_known_tool_names())
    assert not offenders, (
        "an editing-agent tool description tells the agent to call a tool it does not "
        f"register: {offenders} — it registers {sorted(surface)}. Reword the shared "
        "spec so it states the fact without naming a tool this surface lacks, or "
        "register the tool here"
    )


def test_the_detector_sees_the_cross_references_the_descriptions_carry() -> None:
    # Without this the two tests above pass on an empty or unparsed surface.
    known = find_known_tool_names()
    for surface in (find_mcp_descriptions(), find_editing_descriptions()):
        named = find_names_a_description_uses(surface["add_stage"], known)
        assert {"edit_stage", "read_stage"} <= named
        described = find_names_a_description_uses(surface["read_workflow_summary"], known)
        assert "read_stage" in described
