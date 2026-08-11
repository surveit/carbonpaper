"""The tutorial agent: its tool surface, its seeding tool, and the limits passthrough.

Offline throughout: an engine is built but never streamed, and no model is called.
"""
from __future__ import annotations

import asyncio
import json
import shutil
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
from app.agents.tutorial.config import make_tutorial_tools
from app.tools.tutorial import TutorialContext

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


def test_create_tutorial_project_binds_an_absolute_csv_to_every_input_stage(
    projects_root: Path,
) -> None:
    seeded = _seed_a_tour()

    assert seeded["name"] in project_service.list_projects()
    assert [stage["id"] for stage in seeded["stages"]] == [
        "raw_filings", "public_commitments", "matched_commitments",
        "judge_alignment", "flag_contradiction", "publish_report",
    ]

    bound = {entry["stage_id"]: Path(entry["csv_path"]) for entry in seeded["bound_inputs"]}
    assert set(bound) == {"raw_filings", "public_commitments"}
    assert all(path.is_absolute() and path.is_file() for path in bound.values())

    stages = load_workflow(projects_root / seeded["name"])
    sources = [s for s in stages if isinstance(s, InputDataStage)]
    assert {s.id: Path(s.connector.params["path"]) for s in sources} == bound


def test_a_second_tour_reuses_the_project_the_first_one_seeded(projects_root: Path) -> None:
    """Accept what is already there rather than filling the workspace with copies."""
    first = _seed_a_tour()
    (projects_root / first["name"] / "MINE.txt").write_text("kept", encoding="utf-8")

    second = _seed_a_tour()

    assert second["name"] == first["name"]
    assert project_service.list_projects() == [first["name"]]
    # Reused, not re-imported: whatever the reader did to it is still there.
    assert (projects_root / first["name"] / "MINE.txt").is_file()


def test_a_tour_after_the_project_was_deleted_still_seeds(projects_root: Path) -> None:
    """The path that broke it live: delete the tutorial project, then tour again."""
    first = _seed_a_tour()
    # What delete_project does: rmtree the directory, KEEP the store record. And
    # create_project refuses any name whose record exists — so the name is left held by
    # a tombstone: reusing it finds no workflow, importing over it is refused.
    shutil.rmtree(projects_root / first["name"])
    assert project_service.Project.exists(first["name"]), "the record should outlive it"

    second = _seed_a_tour()

    # Whatever it is called, it must be a project that actually loads and can run.
    assert (projects_root / second["name"] / "document.md").is_file()
    assert load_workflow(projects_root / second["name"])
    assert run_service.resolve_version(second["name"], None) == second["version_id"]


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

    tool = next(t for t in _tools() if t.name == "run_workflow")
    out = _call(tool, {"project_id": seeded["name"], "limits": {"raw_filings": 6}})
    started = json.loads(out["content"][0]["text"])

    assert seen["project"] == seeded["name"]
    assert seen["limits"] == {"raw_filings": 6}
    assert seen["version_id"] is None
    # The same {run_id, status} the MCP surface returns — no tour-shaped extra field.
    assert started == {"run_id": "20260810T101112", "status": "ok"}


def test_the_run_link_is_the_seeding_tools_prefix_plus_the_returned_run_id() -> None:
    """run_workflow returns a bare id, so the tour joins two tool-returned halves."""
    seeded = _seed_a_tour()

    assert seeded["runs_url_prefix"] == f"{_BASE_URL}project/{seeded['name']}/runs/"
    assert seeded["runs_url_prefix"] + "20260810T101112" == (
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
    # The join drops no filing, so the cap is what every later stage sees.
    assert by_stage["matched_commitments"]["output_row_count"] == 6
    # No model is available offline, so the LLM stage is where this run stops.
    assert by_stage["judge_alignment"]["status"] == "error"


def test_the_tour_seeds_a_review_guide_the_reader_can_open(projects_root: Path) -> None:
    seeded = _seed_a_tour()

    guide = project_service.read_review_guide(seeded["name"], seeded["version_id"])
    assert guide is not None
    narrated = [sid for step in guide.steps for sid in step.stage_ids]
    assert narrated == [stage["id"] for stage in seeded["stages"]]
    assert guide.unnarrated == []
    assert seeded["guide_url"] == (
        f"{_BASE_URL}project/{seeded['name']}/workflow/version/{seeded['version_id']}"
    )
    assert seeded["workflow_url"] == f"{_BASE_URL}project/{seeded['name']}/workflow"


def test_get_run_status_reads_the_manifest_back_without_waiting(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tour polls: a `running` status is an answer, not an error to recover from."""
    monkeypatch.setattr(run_service, "read_run_status", lambda p, r: {
        "run_id": r, "status": "running",
        "stage_records": [
            {"stage_id": "judge_alignment", "status": "running", "output_row_count": 0}
        ],
    })
    seeded = _seed_a_tour()
    tool = next(t for t in _tools() if t.name == "get_run_status")

    out = _call(tool, {"project_id": seeded["name"], "run_id": "r"})
    status = json.loads(out["content"][0]["text"])

    assert out.get("is_error") is not True
    assert status["status"] == "running"
    assert status["stage_records"][0]["stage_id"] == "judge_alignment"
