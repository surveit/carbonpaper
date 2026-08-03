"""Tests for the editing agent's tool factory.

Asserts the agent's tools are built and correctly named by checking the tool
factory's output (stable, our own names). The engine wiring is covered by
tests/test_project_chat_sdk.py."""
from __future__ import annotations

from app.tools.editing import EditingContext, make_editing_tools

_EXPECTED_TOOL_NAMES = {
    "list_projects",
    "get_current_project",
    "describe_workflow",
    "read_stage",
    "edit_stage",
    "add_stage",
    "remove_stage",
    "list_distinct_values",
    "create_draft",
    "read_draft",
    "set_draft_stage",
    "remove_draft_stage",
    "save_version",
    "read_review_guide",
    "write_review_guide",
}


def test_editing_tools_factory_yields_expected_tool_names() -> None:
    tools = make_editing_tools(EditingContext(project_id="alpha"))
    assert {spec.name for spec in tools} == _EXPECTED_TOOL_NAMES
