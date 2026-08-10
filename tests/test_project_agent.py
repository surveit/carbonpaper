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
    "create_draft",
    "read_draft",
    "set_draft_stage",
    "remove_draft_stage",
    "save_version",
    "read_review_guide",
    "write_review_guide",
    "run_workflow",
    "wait_for_run",
}


def test_editing_tools_factory_yields_expected_tool_names() -> None:
    tools = make_editing_tools(EditingContext(project_id="alpha"))
    assert {spec.name for spec in tools} == _EXPECTED_TOOL_NAMES


def test_the_editing_agent_can_run_a_workflow_and_wait_for_it() -> None:
    tools = {spec.name: spec for spec in make_editing_tools(EditingContext(project_id="a"))}
    # #475: it could author a workflow and not execute one. Each needs a schema and a
    # label, or the SDK cannot bind it.

    for name in ("run_workflow", "wait_for_run"):
        assert set(tools[name].input_schema) >= {"project_id"}
        assert tools[name].label
    assert set(tools["run_workflow"].input_schema) == {"project_id", "version_id", "limits"}
    assert set(tools["wait_for_run"].input_schema) == {
        "project_id", "run_id", "timeout_seconds",
    }
