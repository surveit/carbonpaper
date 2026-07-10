"""Tests for the per-project editing agent builder.

Asserts the agent's tools are built and correctly bound by checking the tool
factory's output (stable, our own names). The SDK engine builder is covered by
tests/test_project_chat_sdk.py."""
from __future__ import annotations

from pathlib import Path

from app.compiler.agent.tools import make_project_tools

_EXPECTED_TOOL_NAMES = {
    "list_projects",
    "get_current_project",
    "describe_workflow",
    "read_stage",
    "edit_stage",
    "add_stage",
    "compile_workflow",
}


def test_project_tools_factory_yields_expected_tool_names(tmp_path: Path) -> None:
    tools = make_project_tools("alpha", examples_dir=tmp_path)
    assert {tool.__name__ for tool in tools} == _EXPECTED_TOOL_NAMES
