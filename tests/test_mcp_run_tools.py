from __future__ import annotations


import pandas as pd

import app.services.run as run_service
from app.models import parse_stage
from app.services.project import save_working_copy_as_version
from app.models.records.workflow_version import WorkflowVersion
from app.services import workspace
from stage_seed import add_stage
from run_seed import manifest_exists


def _make_run_project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"),
                                 "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "name", "type": "str", "nullable": True},
                {"name": "val", "type": "int", "nullable": True},
            ],
        },
    }
    add_stage(root, stage)
    return save_working_copy_as_version(root.name, message="seed").version_id


_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str", "nullable": True},
                            {"name": "score", "type": "int", "nullable": True}]}
_LOAD_STAGE_TMPL = {
    "id": "load", "type": "input_data", "description": "Load rows",
    "signature": {"form": "replaces", "produces": _LOAD_SCHEMA["columns"]},
}
_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "description": "Label by sign",
    "inputs": [{"id": "load"}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": _LOAD_SCHEMA["columns"]}],
        "adds": [{"name": "label", "type": "str", "nullable": True}],
    },
}


def _make_workflow_test_project(root):
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3]}).to_csv(
        root / "data" / "rows.csv", index=False)
    load = dict(_LOAD_STAGE_TMPL,
                connector={"kind": "file",
                           "params": {"path": str(root / "data" / "rows.csv"),
                                      "format": "csv"}})
    WorkflowVersion(
        id=f"{root.name}/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed",
        stages=[parse_stage(s) for s in (load, _CLASSIFY)],
    ).save()


def _sync_background(monkeypatch):
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))


def test_run_workflow_starts_a_real_run_pollable_by_get_run_status(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    _sync_background(monkeypatch)
    _make_run_project(tmp_path / "money_trail")

    started = server.run_workflow(project_id="money_trail")
    assert "run_id" in started
    status = server.get_run_status(project_id="money_trail", run_id=started["run_id"])
    assert status["run_id"] == started["run_id"]
    assert status["status"] == "ok"


def test_run_workflow_translates_no_version_to_error(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    _sync_background(monkeypatch)
    (tmp_path / "unready").mkdir(parents=True, exist_ok=True)

    result = server.run_workflow(project_id="unready")
    assert result["ok"] is False
    assert result["error"]
    assert "run_id" not in result


def test_get_run_status_missing_run_translates_to_error(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    _make_run_project(tmp_path / "money_trail")

    result = server.get_run_status(project_id="money_trail", run_id="20990101T000000")
    assert result["ok"] is False
    assert result["error"]


def test_run_workflow_test_delegates_and_reports_verdict(tmp_path, monkeypatch):
    from app.mcp import server

    workspace.set_projects_dir(tmp_path)
    _make_workflow_test_project(tmp_path / "demo")

    result = server.run_workflow_test(project_id="demo", limit=2, offset=1)
    assert result["ok"] is True
    assert result["stages_run"] == ["classify"]
    assert "rows_out" not in result
    assert "run_id" in result
    # A real run under the project's runs/ dir — reachable through the same
    # get_run_status a production run uses — but marked a test run.
    manifest_project = tmp_path / "demo"

    manifest_run = result["run_id"]
    assert manifest_exists(manifest_project, manifest_run)
    status = server.get_run_status(project_id="demo", run_id=result["run_id"])
    assert status["parameters"]["is_test_run"] is True


def test_run_tools_are_registered(tmp_path, monkeypatch):
    from app.mcp import server

    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert {"run_workflow", "get_run_status", "run_workflow_test"} <= names
