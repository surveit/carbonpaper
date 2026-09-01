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
from fastapi.testclient import TestClient

import app.agents.tutorial.config  # noqa: F401 — registers the "tutorial" agent
import app.services.run as run_service
from app.models.records.project import Project
from app.core.agent.registry import build_engine, build_mcp_server
from app.core.agent.store import open_session_store
from app.main import app as fastapi_app
from app.models.stages.input_data import InputDataStage
from app.services import project as project_service
from app.services.uploads import resolve_files_binding
from app.services.loader import load_workflow
from app.runtime.trace import trace_row, trace_to_dict
from app.tools.editing import EditingContext, build_editing_tools
from app.agents.tutorial.config import build_tutorial_tools
from app.tools.tutorial import TutorialAgentReference, TutorialContext
from app.services.methodology import exists as methodology_exists

_BASE_URL = "http://127.0.0.1:8788/"
_EXPECTED_TOOLS = {
    "create_tutorial_project",
    "offer_next_steps",
    "read_stage_output_rows",
    "run_eval",
    "run_workflow",
    "get_run_status",
    "sleep",
    "read_workflow_summary",
}


def _tools() -> list[SdkMcpTool[Any]]:
    _server, _allowed, wrapped = build_mcp_server(
        build_tutorial_tools(TutorialContext(base_url=_BASE_URL))
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


def test_build_engine_resolves_tutorial_with_only_the_tour_tools() -> None:
    engine = build_engine("tutorial", {"base_url": _BASE_URL})
    bare = {name.rsplit("__", 1)[-1] for name in engine._allowed_tools}
    assert bare == _EXPECTED_TOOLS


def test_the_tutorial_agent_gets_none_of_the_editing_tools() -> None:
    editing = {
        spec.name for spec in build_editing_tools(EditingContext(project_id="anything", base_url="http://reader.test/"))
    }
    engine = build_engine("tutorial", {"base_url": _BASE_URL})
    bare = {name.rsplit("__", 1)[-1] for name in engine._allowed_tools}

    assert "add_stage" in editing and "save_version" in editing  # the list is real
    # The overlap is the shared read-and-run tools; nothing that edits a workflow.
    assert bare & editing == {"read_workflow_summary", "read_stage_output_rows",
                              "run_workflow", "get_run_status", "sleep"}
    for editing_only in ("add_stage", "edit_stages", "delete_stage", "save_version",
                         "create_draft", "set_draft_stage", "write_review_guide"):
        assert editing_only not in bare


def test_the_seeded_project_keeps_no_path_of_its_own(projects_root: Path) -> None:
    """The stored workflow stays portable: the files are named per run, not baked in."""
    seeded = _seed_a_tour()

    assert seeded["project"]["id"] in project_service.list_projects()
    files = seeded["input_files"]
    assert set(files) == {"lobbying_filings", "public_commitments"}
    # A file the project holds, named by its record id, not a path baked into the workflow.
    assert all(resolve_files_binding(seeded["project"]["id"], [file_id])["paths"]
               for file_id in files.values())

    sources = [s for s in load_workflow(seeded["project"]["id"])
               if isinstance(s, InputDataStage)]
    assert [s.connector.params.paths for s in sources] == [[], []]


def test_a_second_tour_reuses_the_project_the_first_one_seeded(projects_root: Path) -> None:
    """Accept what is already there rather than filling the workspace with copies."""
    first = _seed_a_tour()
    (projects_root / first["project"]["id"] / "MINE.txt").write_text("kept", encoding="utf-8")

    second = _seed_a_tour()

    assert second["project"]["id"] == first["project"]["id"]
    assert project_service.list_projects() == [first["project"]["id"]]
    # Reused, not re-imported: whatever the reader did to it is still there.
    assert (projects_root / first["project"]["id"] / "MINE.txt").is_file()


def test_a_tour_after_the_project_was_deleted_still_seeds(projects_root: Path) -> None:
    """The path that broke it live: delete the tutorial project, then tour again."""
    first = _seed_a_tour()
    # The store record outlives the rmtree, so the tour must not reuse a project it
    # can no longer load — it seeds a fresh one instead of resolving the dead record.
    shutil.rmtree(projects_root / first["project"]["id"])
    assert Project.load_or_none(first["project"]["id"]) is not None, "the record should outlive it"

    second = _seed_a_tour()

    # Whatever it is called, it must be a project that actually loads and can run.
    assert second["project"]["id"] != first["project"]["id"]
    assert methodology_exists(second["project"]["id"])
    assert load_workflow(second["project"]["id"])
    assert run_service.resolve_version(second["project"]["id"], None) == second["version_id"]


def test_the_handoff_is_built_from_this_workspaces_base_url() -> None:
    """Both forms name the endpoint this workspace actually serves MCP on."""
    seeded = _seed_a_tour()

    assert seeded["mcp_url"] == f"{_BASE_URL}mcp"
    assert seeded["mcp_command"] == (
        f"claude mcp add --transport http carbonpaper {_BASE_URL}mcp"
    )


def test_the_reference_carries_no_paste_into_your_assistant_message() -> None:
    """It promised a connect-and-author an open session cannot do; the CLI line replaced it."""
    assert "mcp_ask_your_assistant" not in TutorialAgentReference.model_fields


def test_the_seeded_payload_carries_a_chat_link_that_creates_nothing_until_clicked() -> None:
    """A draft: seeding writes no session for either handoff, only a link to one."""
    store = open_session_store()
    before = {s["session_id"] for s in store.list_sessions()}

    seeded = _seed_a_tour()

    assert {s["session_id"] for s in store.list_sessions()} == before, (
        "seeding a tour must not materialize either handoff chat")
    for url in (seeded["edit_chat_url"], seeded["new_project_chat_url"]):
        assert url.startswith(f"{_BASE_URL}chat/agent/editing/new")
        # The claim the tour makes when it hands the URL over: it opens, and opening
        # it still creates nothing (see test_tutorial_route.py).
        assert TestClient(fastapi_app).get(
            url.removeprefix(_BASE_URL.rstrip("/"))).status_code == 200


def test_the_new_project_chat_names_no_project_so_the_agent_makes_one() -> None:
    """A reader starting their own must not land in the tour's project."""
    seeded = _seed_a_tour()

    assert "project_id" not in seeded["new_project_chat_url"]


def test_the_eval_url_addresses_the_eval_the_tour_just_seeded() -> None:
    seeded = _seed_a_tour()

    path = f"/project/{seeded['project']['id']}/evals/{seeded['eval_id']}"

    assert seeded["eval_url"] == _BASE_URL.rstrip("/") + path
    # The claim the tour makes when it hands the URL over: it opens.
    assert TestClient(fastapi_app).get(path).status_code == 200


def test_the_link_and_the_button_are_the_same_door() -> None:
    """Two doors, one room: the tour's link is not a second, different offer."""
    from urllib.parse import urlencode

    seeded = _seed_a_tour()
    project = seeded["project"]["id"]

    assert seeded["edit_chat_url"] == (
        f"{_BASE_URL.rstrip('/')}/chat/agent/editing/new?"
        f"{urlencode({'project_id': project})}"
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
    out = _call(tool, {"project_id": seeded["project"]["id"], "limits": {"lobbying_filings": 6},
                       "files": seeded["input_files"]})
    started = json.loads(out["content"][0]["text"])

    assert seen["project"] == seeded["project"]["id"]
    assert seen["limits"] == {"lobbying_filings": 6}
    assert seen["version_id"] is None
    # The same {run_id, status} the MCP surface returns — no tour-shaped extra field.
    assert started == {"run_id": "20260810T101112", "status": "ok"}


def test_the_run_link_is_the_seeding_tools_prefix_plus_the_returned_run_id() -> None:
    """run_workflow returns a bare id, so the tour joins two tool-returned halves."""
    seeded = _seed_a_tour()

    assert seeded["runs_url_prefix"] == f"{_BASE_URL}project/{seeded['project']['id']}/runs/"
    assert seeded["runs_url_prefix"] + "20260810T101112" == (
        f"{_BASE_URL}project/{seeded['project']['id']}/runs/20260810T101112"
    )


def test_a_real_run_resolves_the_bound_csv_and_honours_the_row_cap(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beat 3 for real: the run's bindings resolve, and the cap reaches the source."""
    monkeypatch.setattr(
        run_service, "_run_in_background", lambda target, *args: target(*args)
    )
    seeded = _seed_a_tour()

    tool = next(t for t in _tools() if t.name == "run_workflow")
    out = _call(tool, {"project_id": seeded["project"]["id"], "limits": {"lobbying_filings": 6},
                       "files": seeded["input_files"]})
    started = json.loads(out["content"][0]["text"])

    status = run_service.read_run_status(seeded["project"]["id"], started["run_id"])
    by_stage = {r["stage_id"]: r for r in status["stage_records"]}
    assert by_stage["lobbying_filings"]["output_row_count"] == 6
    # The join drops no filing, so the cap is what every later stage sees.
    assert by_stage["filings_with_commitments"]["output_row_count"] == 6
    # No model is available offline, so getting through here is the seeded cache.
    assert by_stage["judge_alignment"]["status"] == "ok"
    assert by_stage["judge_alignment"]["cached_rows"] == 6


def test_the_tour_seeds_a_review_guide_the_reader_can_open(projects_root: Path) -> None:
    seeded = _seed_a_tour()

    guide = project_service.read_review_guide(seeded["project"]["id"], seeded["version_id"])
    assert guide is not None
    narrated = [sid for step in guide.steps for sid in step.stage_ids]
    assert narrated == [s.id for s in load_workflow(seeded["project"]["id"])]
    assert guide.unnarrated == []
    assert seeded["guide_url"] == (
        f"{_BASE_URL}project/{seeded['project']['id']}/workflow/version/{seeded['version_id']}"
    )
    assert seeded["workflow_url"] == f"{_BASE_URL}project/{seeded['project']['id']}/workflow"


def test_get_run_status_reads_the_manifest_back_without_waiting(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tour polls: a `running` status is an answer, not an error to recover from."""
    monkeypatch.setattr(run_service, "read_run_status_without_tracebacks", lambda p, r: {
        "run_id": r, "status": "running",
        "stage_records": [
            {"stage_id": "judge_alignment", "status": "running", "output_row_count": 0}
        ],
    })
    seeded = _seed_a_tour()
    tool = next(t for t in _tools() if t.name == "get_run_status")

    out = _call(tool, {"project_id": seeded["project"]["id"], "run_id": "r"})
    status = json.loads(out["content"][0]["text"])

    assert out.get("is_error") is not True
    assert status["status"] == "running"
    assert status["stage_records"][0]["stage_id"] == "judge_alignment"


def _run_the_tour_capped(
    monkeypatch: pytest.MonkeyPatch, bust_cache: bool = False
) -> tuple[dict[str, Any], str]:
    monkeypatch.setattr(
        run_service, "_run_in_background", lambda target, *args: target(*args)
    )
    seeded = _seed_a_tour()
    out = _call(
        next(t for t in _tools() if t.name == "run_workflow"),
        {"project_id": seeded["project"]["id"], "limits": {"lobbying_filings": 6},
         "files": seeded["input_files"], "bust_cache": bust_cache},
    )
    return seeded, json.loads(out["content"][0]["text"])["run_id"]


def _read_lineage_links(
    project: str, run_id: str, stage_id: str, **window: int
) -> dict[str, Any]:
    out = _call(
        next(t for t in _tools() if t.name == "read_stage_output_rows"),
        {"project_id": project, "run_id": run_id, "stage_id": stage_id, **window},
    )
    assert out.get("is_error") is not True, out["content"][0]["text"]
    links: dict[str, Any] = json.loads(out["content"][0]["text"])
    return links


def test_each_row_comes_back_with_the_whole_link_to_its_own_lineage(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is left for the tour to join: an ordinal it guessed would link a wrong row."""
    seeded, run_id = _run_the_tour_capped(monkeypatch)

    links = _read_lineage_links(seeded["project"]["id"], run_id, "filings_with_commitments")

    assert links["row_count"] == 6
    assert [row["ordinal"] for row in links["rows"]] == list(range(6))
    for row in links["rows"]:
        assert row["lineage_url"] == (
            f"{seeded['runs_url_prefix']}{run_id}"
            f"/stage/filings_with_commitments/row/{row['ordinal']}/trace/view"
        )


def test_a_blank_cell_reaches_the_tour_blank(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tour picks its absence row off these values — "None" as text would read as one."""
    seeded, run_id = _run_the_tour_capped(monkeypatch)

    rows = _read_lineage_links(seeded["project"]["id"], run_id, "filings_with_commitments")["rows"]

    blank = [row for row in rows if row["values"]["public_commitment"] is None]
    filled = [row for row in rows if row["values"]["public_commitment"] is not None]
    assert blank and filled
    assert all(row["values"]["client"] for row in rows)


def test_the_row_the_tour_calls_an_absence_is_the_one_with_no_second_parent(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beat 4's claim, checked against the trace the link opens."""
    seeded, run_id = _run_the_tour_capped(monkeypatch)
    run_dir = projects_root / seeded["project"]["id"] / "runs" / run_id

    rows = _read_lineage_links(seeded["project"]["id"], run_id, "filings_with_commitments")["rows"]

    for row in rows:
        trace = trace_to_dict(trace_row(run_dir, "filings_with_commitments", row["ordinal"]))
        joined = next(s for s in trace["steps"] if s["stage_id"] == "filings_with_commitments")
        matched = row["values"]["public_commitment"] is not None
        assert bool(joined["branches"]) is matched, row["values"]["client"]
        # The chain the reader walks: back through the check to the filing as filed.
        assert [step["stage_id"] for step in trace["steps"]] == [
            "filings_with_commitments", "clean_filings", "lobbying_filings"
        ]


def test_a_stage_that_did_not_finish_is_refused_rather_than_read(
    projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An errored stage still wrote a frame — of nulls it never filled in."""
    seeded, run_id = _run_the_tour_capped(monkeypatch, bust_cache=True)

    out = _call(
        next(t for t in _tools() if t.name == "read_stage_output_rows"),
        {"project_id": seeded["project"]["id"], "run_id": run_id, "stage_id": "judge_alignment"},
    )

    assert out["is_error"] is True
    assert "is 'error'" in out["content"][0]["text"]
    assert run_service.read_stage_output(seeded["project"]["id"], run_id, "judge_alignment")[
        "ai_judgment"
    ].isna().all(), "the frame the refusal is protecting the tour from"


def test_the_seeding_tool_hands_back_the_stages_it_seeded(projects_root: Path) -> None:
    """Beats 3 and 4 pick their stages by TYPE off this, so the script names none itself."""
    workflow = _seed_a_tour()["workflow"]

    by_type = {stage["type"]: stage["id"] for stage in workflow["stages"]}
    assert workflow["issues"] == []
    assert [stage["id"] for stage in workflow["stages"]] == [
        s.id for s in load_workflow(workflow["name"])
    ]
    # The script's three rules: the stage before the report, the coded one, beat 3's queue.
    assert by_type["report"] == "publish_report"
    assert by_type["python_row_function"] == "clean_filings"
    assert by_type["human_review_queue"] == "review_contradictions"
    feeds_report = next(s for s in workflow["stages"] if s["id"] == by_type["report"])
    assert feeds_report["inputs"] == ["review_contradictions"]
