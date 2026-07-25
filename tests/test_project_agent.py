"""Tests for the editing agent's tool factory.

Asserts the agent's tools are built and correctly named by checking the tool
factory's output (stable, our own names). The engine wiring is covered by
tests/test_project_chat_sdk.py."""
from __future__ import annotations

from app.agents.compiler.tools import EditingContext, make_editing_tools

_EXPECTED_TOOL_NAMES = {
    "list_projects",
    "get_current_project",
    "describe_workflow",
    "read_stage",
    "edit_stage",
    "add_stage",
    "remove_stage",
    "compile_workflow",
    "create_draft",
    "read_draft",
    "set_draft_stage",
    "remove_draft_stage",
    "save_version",
}


def test_editing_tools_factory_yields_expected_tool_names() -> None:
    tools = make_editing_tools(EditingContext(project_id="alpha"))
    assert {tool.__name__ for tool in tools} == _EXPECTED_TOOL_NAMES
