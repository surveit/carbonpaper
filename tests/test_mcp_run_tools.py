"""The MCP run surface: the run_workflow / get_run_status / test_run tools.

Thin wrappers over app.services.run / app.services.test_run. Tests call the tool
functions directly against a tmp workspace (the EXAMPLES_DIR idiom from
tests/test_mcp_server.py) and assert the delegate contract plus the loud-error
translation to {ok: False, error}. The run happy path reuses the file-connector
project idiom from tests/test_run_service.py; the test-run delegate reuses the demo
fixtures from tests/test_test_run_service.py."""
from __future__ import annotations

import json

import pandas as pd

import app.services.run as run_service
from app.models import Stage
from app.services import versioning
from app.services.versioning import WorkflowVersion, create_version_from_disk


def _make_run_project(root):
    """A tiny file-connector project (from tests/test_run_service.py), published."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"),
                                 "format": "csv"}},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    vid = create_version_from_disk(root, message="seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")
    return vid


_LOAD_SCHEMA = {"columns": [{"name": "doc_id", "type": "str"},
                            {"name": "score", "type": "int"}]}
_LOAD_STAGE_TMPL = {
    "id": "load", "type": "input_data", "name": "Load rows",
    "output_schema": _LOAD_SCHEMA,
}
_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "name": "Label by sign",
    "inputs": [{"id": "load", "schema": _LOAD_SCHEMA}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "output_schema": {"columns": [{"name": "doc_id", "type": "str"},
                                  {"name": "score", "type": "int"},
                                  {"name": "label", "type": "str"}]},
}


def _make_test_run_project(root):
    """A `demo` project with a bound 4-row source and one deterministic stage,
    seeded as an unpublished version (test runs work on unpublished candidates)."""
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3]}).to_csv(
        root / "data" / "rows.csv", index=False)
    load = dict(_LOAD_STAGE_TMPL,
                connector={"kind": "file",
                           "params": {"path": str(root / "data" / "rows.csv"),
                                      "format": "csv"}})
    WorkflowVersion(
        id=f"{root.name}/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test", published=False,
        stages=[Stage.model_validate(s) for s in (load, _CLASSIFY)],
    ).save()


def _sync_background(monkeypatch):
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))


def test_run_workflow_starts_a_real_run_pollable_by_get_run_status(tmp_path, monkeypatch):
    """run_workflow mints a real run id; get_run_status reads back its manifest."""
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    _sync_background(monkeypatch)
    _make_run_project(tmp_path / "money_trail")

    started = server.run_workflow(project_id="money_trail")
    assert "run_id" in started
    status = server.get_run_status(project_id="money_trail", run_id=started["run_id"])
    assert status["run_id"] == started["run_id"]
    assert status["status"] == "ok"


def test_run_workflow_translates_no_version_to_error(tmp_path, monkeypatch):
    """A project with no published version fails loudly as {ok: False, error},
    never a traceback or a fabricated run id."""
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    _sync_background(monkeypatch)
    (tmp_path / "unready").mkdir()

    result = server.run_workflow(project_id="unready")
    assert result["ok"] is False
    assert result["error"]
    assert "run_id" not in result


def test_get_run_status_missing_run_translates_to_error(tmp_path, monkeypatch):
    """An unknown run id becomes {ok: False, error}, not a RunNotFoundError trace."""
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    _make_run_project(tmp_path / "money_trail")

    result = server.get_run_status(project_id="money_trail", run_id="20990101T000000")
    assert result["ok"] is False
    assert result["error"]


def test_test_run_delegates_and_reports_verdict(tmp_path, monkeypatch):
    """test_run runs the frontier over the sample and returns the test-run verdict
    (ok True, the executed stage, its row count) — never a production run."""
    from app.mcp import server
    from app.services import workspace

    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    _make_test_run_project(tmp_path / "demo")

    result = server.test_run(project_id="demo", limit=2, offset=1)
    assert result["ok"] is True
    assert result["stages_run"] == ["classify"]
    assert result["rows_out"] == 2
    assert "test_run_id" in result
    assert not (tmp_path / "demo" / "runs").exists()


def test_run_tools_are_registered(tmp_path, monkeypatch):
    """The three run tools are on the MCP tool registry."""
    from app.mcp import server

    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert {"run_workflow", "get_run_status", "test_run"} <= names
