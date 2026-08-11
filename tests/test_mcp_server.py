"""The app lifespan starts a fresh MCP session manager per entry, so entering it here
is safe alongside other lifespan-running tests.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models import parse_stage, StageDraft
from app.services import workspace

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    # FastMCP auto-enables DNS-rebinding protection for its default host
    # (127.0.0.1), which only allow-lists Host headers of the form
    # "127.0.0.1:<port>" / "localhost:<port>" — TestClient's default
    # "testserver" Host fails that check with 421. Point the client at an
    # allowed host instead of loosening the server's security settings.
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


def test_mcp_endpoint_initializes(client):
    resp = client.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "carbonpaper"
    assert resp.history == []  # exact-path match — a 307 redirect would break non-following MCP clients


def test_mcp_lists_the_authoring_tools(client):
    client.post("/mcp", json=INITIALIZED, headers=HEADERS)
    resp = client.post("/mcp", json=LIST_TOOLS, headers=HEADERS)
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert {
        "list_projects",
        "create_project",
        "get_project_status",
        "generate_data_model",
        "read_data_model",
        "describe_workflow",
        "read_stage",
        "edit_stage",
        "add_stage",
        "remove_stage",
        "generate_stage_tests",
        "run_stage_tests",
        "save_version",
        "read_review_guide",
        "write_review_guide",
    } <= names


def test_create_project_tool_and_status(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    created = server.create_project(name="Money Trail", document="Follow the filings.")
    assert created["project_id"] == "money_trail"
    status = server.get_project_status(project_id="money_trail")
    assert status["has_document"] is True


def test_generate_data_model_kicks_the_live_turn(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    server.create_project(name="probe", document="doc text")

    seen: dict[str, object] = {}

    def fake_start(pdir: Path, *, document: str, model: str) -> str:
        seen["pdir"] = pdir
        seen["document"] = document
        return "sess123"

    monkeypatch.setattr(server.generation, "start_generation", fake_start)
    out = asyncio.run(server.generate_data_model(project_id="probe"))
    assert out["watch"] == "/chat/sess123"
    assert seen["document"] == "doc text"


def test_generate_data_model_without_document_fails_loudly(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    (tmp_path / "empty_proj").mkdir()
    with pytest.raises(ValueError):
        asyncio.run(server.generate_data_model(project_id="empty_proj"))


_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": True},
]}
_DOUBLE = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"
_LOAD_STAGE = StageDraft.model_validate({
    "id": "load", "description": "Load", "type": "input_data", "connector": {"kind": "file"},
    "signature": {
        "form": "replaces",
        "produces": [{"name": "doc_id", "type": "str", "nullable": False}],
    },
})


def _write_compiled_workflow(pdir: Path) -> None:
    from app.services.loader import write_stage

    compiled = pdir / "compiled"
    compiled.mkdir(parents=True)
    stages: list[dict[str, object]] = [
        {"id": "load", "description": "Load", "type": "input_data", "connector": {"kind": "file"},
         "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]}},
        {"id": "double", "description": "Double", "type": "python_row_function",
         "inputs": [{"id": "load", "schema": _IN_SCHEMA}], "signature": {
             "form": "extends",
             "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
             "adds": [{"name": "doubled", "type": "float", "nullable": True}],
         },
         "function": {"kind": "inline", "code": _DOUBLE},
         "tests": [
             {"name": "doubles", "inputs": {"load": [{"amount": 2.0}]},
              "expected": [{"amount": 2.0, "doubled": 4.0}]},
             {"name": "wrong", "inputs": {"load": [{"amount": 2.0}]},
              "expected": [{"amount": 2.0, "doubled": 5.0}]},
         ]},
        {"id": "untested", "description": "Untested", "type": "python_row_function",
         "inputs": [{"id": "load", "schema": _IN_SCHEMA}], "signature": {
             "form": "extends",
             "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
             "adds": [{"name": "doubled", "type": "float", "nullable": True}],
         },
         "function": {"kind": "inline", "code": _DOUBLE}},
    ]
    for spec in stages:
        write_stage(compiled / f"{spec['id']}.json", parse_stage(spec))


def test_run_stage_tests_reports_summary_diffs_and_coverage(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    _write_compiled_workflow(pdir)

    report = server.run_stage_tests(project_id="trail")
    assert set(report) == {"summary", "stages", "untested_stages"}
    assert report["untested_stages"] == ["untested"]
    assert report["summary"]["failed"] == 1
    [run] = report["stages"]
    failing = next(o for o in run["results"] if o["name"] == "wrong")
    assert failing["status"] != "passed"
    assert failing["diffs"][0]["column"] == "doubled"


def test_run_stage_tests_scopes_to_one_stage(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    _write_compiled_workflow(pdir)

    report = server.run_stage_tests(project_id="trail", stage_id="double")
    assert report["summary"]["tests_total"] == 2


def test_generate_stage_tests_kicks_the_generation_turn(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    server.create_project(name="probe", document="doc text")

    seen: dict[str, object] = {}

    def fake_start(pdir: Path, *, stage_id: str, model: str) -> str:
        seen["stage_id"] = stage_id
        return "sess-tests"

    monkeypatch.setattr(server.generation, "start_stage_test_generation", fake_start)
    out = asyncio.run(server.generate_stage_tests(project_id="probe", stage_id="double"))
    assert out["status"] == "started"
    assert out["watch"] == "/chat/sess-tests"
    assert seen["stage_id"] == "double"


def test_mcp_remove_stage_returns_ok_and_issues(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    _write_compiled_workflow(pdir)

    removed = server.remove_stage(project_id="trail", stage_id="untested")
    assert removed == {"ok": True, "issues": []}
    assert not (pdir / "compiled" / "untested.json").exists()

    # `double` still inputs from `load`, so removing `load` is refused with issues.
    refused = server.remove_stage(project_id="trail", stage_id="load")
    assert refused["ok"] is False and refused["issues"]
    assert (pdir / "compiled" / "load.json").exists()


def test_mcp_stage_tools_report_an_unknown_stage_id_as_issues(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    _write_compiled_workflow(tmp_path / "trail")

    removed = server.remove_stage(project_id="trail", stage_id="ghost")
    assert removed["ok"] is False and any("ghost" in i for i in removed["issues"])

    edited = server.edit_stage(project_id="trail", stage_id="ghost", changes_json='{"limit": 1}')
    assert edited["ok"] is False and any("ghost" in i for i in edited["issues"])


def test_mcp_add_stage_reports_an_unloadable_workflow_as_issues(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    compiled = tmp_path / "trail" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "broken.json").write_text('{"id": "broken", "type": "not_a_real_type"}', encoding="utf-8")

    added = server.add_stage(
        project_id="trail",
        stages=[_LOAD_STAGE],
    )
    assert added["ok"] is False and added["issues"]
    assert not (compiled / "load.json").exists()


def test_mcp_add_stage_refuses_to_invent_a_project(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    with pytest.raises(ValueError):
        server.add_stage(
            project_id="no_such_project",
            stages=[_LOAD_STAGE],
        )
    assert list(tmp_path.iterdir()) == []


def test_mcp_add_stage_creates_the_first_stage_of_a_new_project(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    server.create_project(name="trail", document="Follow the filings.")

    added = server.add_stage(
        project_id="trail",
        stages=[_LOAD_STAGE],
    )
    assert added == {
        "ok": True, "issues": [], "added": ["load"], "failed": [], "skipped": [],
    }, "a clean draft warns about nothing"
    assert server.describe_workflow(project_id="trail")["stages"][0]["id"] == "load"


def test_mcp_add_stage_drops_server_owned_fields_and_names_them(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    server.create_project(name="trail", document="Follow the filings.")
    echoed = {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "doc_id", "type": "str", "nullable": True}],
        },
        "tests": [], "source": {"section": "para 3"},
    }

    _content, added = asyncio.run(
        server.mcp.call_tool("add_stage", {"project_id": "trail", "stages": [echoed]})
    )

    assert added["ok"] is True and added["added"] == ["load"]
    named, explanation = added["warnings"]
    assert named.startswith("`load`:"), "a batch must not lose which stage carried them"
    assert "tests" in named and "source" in named
    assert "eval" not in named and "review" not in named, "names only what was sent"
    assert "generate_stage_tests" in explanation
    stored = json.loads(server.read_stage(project_id="trail", stage_id="load"))
    assert not {"tests", "source"} & set(stored)


def test_mcp_add_stage_still_refuses_an_unknown_field(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    server.create_project(name="trail", document="Follow the filings.")
    typo = {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "nonsense": 1,
    }

    with pytest.raises(Exception, match="nonsense"):
        asyncio.run(server.mcp.call_tool("add_stage", {"project_id": "trail", "stages": [typo]}))


_UNADDITIVE_LLM_STAGE = {
    "id": "score", "description": "Score", "type": "llm_transform",
    "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
    # The signature must read exactly what the template injects; reading `id`
    # instead of `amount` breaks that, and `Stage` is where that rule lives.
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": [{"name": "id", "type": "str", "nullable": True}]}],
        "adds": [{"name": "verdict", "type": "str", "nullable": True}],
    },
    "llm": {"prompt_data_template": "judge {amount}"},
}


def test_mcp_add_stage_refuses_an_invalid_stage_on_the_issues_channel(tmp_path, monkeypatch):
    """Through the real boundary: a rule firing during binding gives isError, not issues."""
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    _write_compiled_workflow(tmp_path / "trail")

    _content, refused = asyncio.run(
        server.mcp.call_tool("add_stage", {"project_id": "trail", "stages": [_UNADDITIVE_LLM_STAGE]})
    )

    assert refused["ok"] is False
    assert any("prompt template" in issue for issue in refused["issues"])
    assert not (tmp_path / "trail" / "compiled" / "score.json").exists()


def test_add_stage_input_schema_omits_the_server_owned_fields(tmp_path, monkeypatch):
    from app.mcp import server

    [tool] = [t for t in asyncio.run(server.mcp.list_tools()) if t.name == "add_stage"]
    defs = tool.inputSchema["$defs"]

    assert not {"tests", "eval", "review", "source"} & set(defs["StageDraft"]["properties"])


def test_mcp_save_version_snapshots_the_working_copy_unpublished(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import versioning

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    _write_compiled_workflow(pdir)

    saved = server.save_version(project_id="trail", message="first cut")
    assert saved["ok"] is True and saved["issues"] == []

    [version] = versioning.list_versions(pdir)
    assert saved["version_id"] == version.version_id
    assert version.message == "first cut"
    assert version.reviewer == "agent"
    assert version.published is False
    assert {s.id for s in version.stages} == {"load", "double", "untested"}


def test_mcp_save_version_omitting_the_parent_records_none(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import versioning

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    _write_compiled_workflow(pdir)

    server.save_version(project_id="trail", message="first cut")
    second = server.save_version(project_id="trail", message="second cut")

    assert second["ok"] is True
    assert versioning.load_version(pdir, second["version_id"]).parent_version is None


def test_mcp_save_version_records_the_caller_supplied_parent(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import versioning

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    _write_compiled_workflow(pdir)

    first = server.save_version(project_id="trail", message="first cut")
    second = server.save_version(
        project_id="trail", message="second cut", parent_version=first["version_id"])

    assert second["version_id"] != first["version_id"]
    saved = versioning.load_version(pdir, second["version_id"])
    assert saved.parent_version == first["version_id"]


def test_mcp_save_version_refuses_a_parent_that_does_not_exist(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import versioning

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    _write_compiled_workflow(pdir)

    server.save_version(project_id="trail", message="first cut")
    before = [v.version_id for v in versioning.list_versions(pdir)]

    refused = server.save_version(
        project_id="trail", message="second cut", parent_version="20200101T000000")

    assert refused["ok"] is False
    assert "20200101T000000" in " ".join(refused["issues"])
    assert "version_id" not in refused
    assert [v.version_id for v in versioning.list_versions(pdir)] == before


def test_mcp_save_version_refuses_an_unloadable_working_copy(tmp_path, monkeypatch):
    from app.mcp import server
    from app.services import versioning

    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "trail"
    (pdir / "compiled").mkdir(parents=True)
    (pdir / "compiled" / "broken.json").write_text(
        '{"id": "broken", "type": "not_a_real_type"}', encoding="utf-8")

    refused = server.save_version(project_id="trail", message="doomed")
    assert refused["ok"] is False and refused["issues"]
    assert "version_id" not in refused
    assert versioning.list_versions(pdir) == []


def test_mcp_save_version_refuses_to_invent_a_project(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    with pytest.raises(ValueError):
        server.save_version(project_id="no_such_project", message="nope")
    assert list(tmp_path.iterdir()) == []


def _saved_version(tmp_path, monkeypatch) -> str:
    from app.mcp import server
    from app.services import workspace

    workspace.set_projects_dir(tmp_path)
    _write_compiled_workflow(tmp_path / "trail")
    return server.save_version(project_id="trail", message="first cut")["version_id"]


_GUIDE = {
    "steps": [
        {"title": "Double each amount", "prose": "Every `amount` is doubled as filed.",
         "stage_ids": ["double"],
         "data_description": "Every filed row, its `amount` doubled."},
    ],
    "unnarrated": ["load", "untested"],
}


def test_mcp_review_guide_round_trips_through_the_tool_boundary(tmp_path, monkeypatch):
    from app.mcp import server

    version_id = _saved_version(tmp_path, monkeypatch)
    args = {"project_id": "trail", "version_id": version_id}
    _content, before = asyncio.run(server.mcp.call_tool("read_review_guide", args))
    assert before["result"] is None

    asyncio.run(server.mcp.call_tool("write_review_guide", {**args, "guide": _GUIDE}))

    _content, stored = asyncio.run(server.mcp.call_tool("read_review_guide", args))
    assert stored["result"]["steps"] == _GUIDE["steps"]
    assert stored["result"]["unnarrated"] == _GUIDE["unnarrated"]


def test_mcp_write_review_guide_refuses_a_mismatch_naming_the_stage(tmp_path, monkeypatch):
    from app.mcp import server

    version_id = _saved_version(tmp_path, monkeypatch)
    args = {"project_id": "trail", "version_id": version_id}
    partial = {"steps": _GUIDE["steps"], "unnarrated": ["load"]}  # 'untested' accounted for nowhere

    with pytest.raises(Exception, match="untested"):
        asyncio.run(server.mcp.call_tool("write_review_guide", {**args, "guide": partial}))

    _content, stored = asyncio.run(server.mcp.call_tool("read_review_guide", args))
    assert stored["result"] is None


def test_read_tools_reject_unknown_project(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    with pytest.raises(ValueError):
        server.read_data_model(project_id="no_such_project")
    with pytest.raises(ValueError):
        server.describe_workflow(project_id="no_such_project")
