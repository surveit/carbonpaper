"""No surface infers which draft an edit lands in: the caller names it, chat and MCP alike."""
from __future__ import annotations

import typing

from app.mcp import server as mcp_server
from app.tools.editing import EditingContext, build_editing_tools

_EDITS_A_DRAFT = [
    "read_workflow_draft", "read_draft_stage",
    "edit_stages", "add_stage", "delete_stage", "save_version",
]


def _chat_tool_parameters(name: str) -> set[str]:
    ctx = EditingContext(project_id="demo", base_url="http://reader.test/")
    spec = next(s for s in build_editing_tools(ctx) if s.name == name)
    return set(spec.json_schema["properties"])


def test_every_chat_tool_over_a_draft_takes_the_draft_id() -> None:
    for name in _EDITS_A_DRAFT:
        assert "draft_id" in _chat_tool_parameters(name), name


def test_start_editing_hands_back_the_id_and_nothing_else() -> None:
    assert typing.get_type_hints(mcp_server.start_editing)["return"] is str
