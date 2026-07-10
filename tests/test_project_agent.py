"""Tests for the per-project editing agent builder.

Asserts the agent is built and correctly bound by checking the tool factory's
output (stable, our own names) rather than PydanticAI's private tool registry
(unstable, internal). Uses the scripted dev model so this runs offline."""
from __future__ import annotations

from pathlib import Path

from app.chat import project_agent
from app.chat.dev_model import make_dev_model
from app.chat.engine import ChatEngine
from app.chat.project_tools import make_project_tools

_EXPECTED_TOOL_NAMES = {
    "list_projects",
    "describe_workflow",
    "describe_stage_types",
    "read_stage",
    "edit_stage",
    "add_stage",
    "create_version",
    "compile_workflow",
}


def test_project_tools_factory_yields_expected_tool_names(tmp_path: Path) -> None:
    tools = make_project_tools("alpha", examples_dir=tmp_path)
    assert {tool.__name__ for tool in tools} == _EXPECTED_TOOL_NAMES


def test_build_project_agent_returns_a_chat_engine(tmp_path: Path) -> None:
    engine = project_agent.build_project_agent(
        "alpha", examples_dir=tmp_path, model=make_dev_model()
    )
    assert isinstance(engine, ChatEngine)


def test_get_project_agent_caches_per_name(tmp_path: Path, monkeypatch) -> None:
    # CW_CHAT_BACKEND=dev keeps this deterministic and offline: get_project_agent
    # builds with no model override, so without this it would fall through to
    # backend auto-selection (CLI availability / API key), which varies by machine.
    monkeypatch.setenv("CW_CHAT_BACKEND", "dev")
    monkeypatch.setattr(project_agent, "_agents", {})
    first = project_agent.get_project_agent("alpha")
    second = project_agent.get_project_agent("alpha")
    assert first is second
