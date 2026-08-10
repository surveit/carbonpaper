"""The tutorial agent: its tool surface, its seeding tool, and the limits passthrough.

Offline throughout: an engine is built but never streamed, and no model is called.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

import app.agents.tutorial.config  # noqa: F401 — registers the "tutorial" agent
import app.services.run as run_service
from app.core.agent.registry import build_engine, build_mcp_server
from app.models.stages.input_data import InputDataStage
from app.services import project as project_service
from app.services.loader import load_workflow
from app.tools.editing import EditingContext, make_editing_tools
from app.tools.tutorial import TutorialContext, make_tutorial_tools

_BASE_URL = "http://127.0.0.1:8788/"
_EXPECTED_TOOLS = {
    "create_tutorial_project",
    "run_workflow",
    "get_run_status",
    "describe_workflow",
}


def _tools() -> list[SdkMcpTool[Any]]:
    _server, _allowed, wrapped = build_mcp_server(
        make_tutorial_tools(TutorialContext(base_url=_BASE_URL))
    )
    return wrapped


def _call(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        return await tool.handler(args)

    return asyncio.run(_run())


def _seed_a_tour() -> dict[str, Any]:
    tool = next(t for t in _tools() if t.name == "create_tutorial_project")
    out = _call(tool, {})
    assert out.get("is_error") is not True, out["content"][0]["text"]
    parsed: dict[str, Any] = json.loads(out["content"][0]["text"])
    return parsed


def test_build_engine_resolves_tutorial_with_only_the_four_tour_tools() -> None:
    engine = build_engine("tutorial", {"base_url": _BASE_URL})
    bare = {name.rsplit("__", 1)[-1] for name in engine._allowed_tools}
    assert bare == _EXPECTED_TOOLS


def test_the_tutorial_agent_gets_none_of_the_editing_tools() -> None:
    editing = {
        spec.name for spec in make_editing_tools(EditingContext(project_id="anything"))
    }
    engine = build_engine("tutorial", {"base_url": _BASE_URL})
    bare = {name.rsplit("__", 1)[-1] for name in engine._allowed_tools}

    assert "add_stage" in editing and "save_version" in editing  # the list is real
    assert bare & editing == {"describe_workflow"}
    for editing_only in ("add_stage", "edit_stage", "remove_stage", "save_version",
                         "create_draft", "set_draft_stage", "write_review_guide"):
        assert editing_only not in bare


def test_create_tutorial_project_imports_the_fixture_with_an_absolute_csv_bound(
    projects_root: Path,
) -> None:
    seeded = _seed_a_tour()

    assert seeded["name"] in project_service.list_projects()
    assert [stage["id"] for stage in seeded["stages"]] == [
        "raw_filings", "significant_filings", "classify_issues",
        "flag_followup", "publish_report",
    ]

    bound = Path(seeded["csv_path"])
    assert bound.is_absolute() and bound.is_file()

    stages = load_workflow(projects_root / seeded["name"])
    source = next(s for s in stages if isinstance(s, InputDataStage))
    stored = source.connector.params["path"]
    assert Path(stored).is_absolute()
    assert Path(stored) == bound


def test_two_tours_seed_two_distinct_projects(projects_root: Path) -> None:
    first = _seed_a_tour()
    second = _seed_a_tour()

    assert first["name"] != second["name"]
    assert {first["name"], second["name"]} <= set(project_service.list_projects())


def test_the_handoff_command_is_built_from_this_workspaces_base_url() -> None:
    seeded = _seed_a_tour()
    assert seeded["mcp_command"] == (
        f"claude mcp add --transport http carbonpaper {_BASE_URL}mcp"
    )


def test_run_workflow_passes_limits_through_to_the_run_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_a_tour()
    seen: dict[str, Any] = {}

    def _capture(project: str, **kwargs: Any) -> str:
        seen["project"] = project
        seen.update(kwargs)
        return "20260810T101112"

    monkeypatch.setattr(run_service, "start_run", _capture)
    monkeypatch.setattr(run_service, "read_run_status", lambda p, r: {"status": "ok"})
    monkeypatch.setattr(run_service, "read_pinned_version", lambda p, r: "v1")

    tool = next(t for t in _tools() if t.name == "run_workflow")
    out = _call(tool, {"project_id": seeded["name"], "limits": {"raw_filings": 6}})
    started = json.loads(out["content"][0]["text"])

    assert seen["project"] == seeded["name"]
    assert seen["limits"] == {"raw_filings": 6}
    assert seen["version_id"] is None
    assert started["run_id"] == "20260810T101112"
    assert started["run_url"] == (
        f"{_BASE_URL}project/{seeded['name']}/runs/20260810T101112"
    )


def test_a_real_run_resolves_the_bound_csv_and_honours_the_row_cap(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beat 3 for real: the bound CSV preflights, and the cap reaches the source."""
    monkeypatch.setattr(
        run_service, "_run_in_background", lambda target, *args: target(*args)
    )
    seeded = _seed_a_tour()

    tool = next(t for t in _tools() if t.name == "run_workflow")
    out = _call(tool, {"project_id": seeded["name"], "limits": {"raw_filings": 6}})
    started = json.loads(out["content"][0]["text"])

    status = run_service.read_run_status(seeded["name"], started["run_id"])
    by_stage = {r["stage_id"]: r for r in status["stage_records"]}
    assert by_stage["raw_filings"]["output_row_count"] == 6
    # The filter runs before the model stage, so fewer rows would have reached it.
    assert by_stage["significant_filings"]["output_row_count"] < 6
    # No model is available offline, so the LLM stage is where this run stops.
    assert by_stage["classify_issues"]["status"] == "error"


def test_the_mcp_run_workflow_tool_forwards_limits_too(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.mcp import server

    seeded = _seed_a_tour()
    seen: dict[str, Any] = {}

    def _capture(project: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "20260810T101112"

    monkeypatch.setattr(run_service, "start_run", _capture)
    monkeypatch.setattr(run_service, "read_run_status", lambda p, r: {"status": "ok"})

    started = server.run_workflow(
        project_id=seeded["name"], limits={"raw_filings": 6}
    )
    assert started["run_id"] == "20260810T101112"
    assert seen["limits"] == {"raw_filings": 6}
