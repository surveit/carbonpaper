"""Offline end-to-end gate through the real web endpoints; conftest forces the LLM
offline, so the journey covered here excludes the LLM stages.
"""
from __future__ import annotations

import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import create_project
from app.services import workspace
from stage_seed import add_stage
from run_seed import manifest_exists, read_manifest

client = TestClient(app)

PROJECT = "smoke_journey"


def assert_run_ok(status: dict, project_dir, run_id: str) -> None:
    """pytest truncates a bare `assert ..., status`, so which stage failed never reaches the log."""
    if status.get("status") == "ok":
        return
    detail = "no manifest stored"
    manifest_project = project_dir

    manifest_run = run_id
    if manifest_exists(manifest_project, manifest_run):
        manifest = read_manifest(manifest_project, manifest_run)
        problems = [
            record for record in manifest.get("stage_records", [])
            if record and record.get("status") not in ("ok", "pending")
        ]
        detail = json.dumps(problems, indent=2, default=str)
    pytest.fail(
        f"run {run_id} finished {status.get('status')!r}, not 'ok'\n"
        f"status: {json.dumps(status, indent=2, default=str)}\n"
        f"non-ok stage records:\n{detail}"
    )


def test_offline_journey_reaches_its_artifact(journey_project, tmp_path):
    # Version the working copy through the web endpoint.
    resp = client.post(f"/project/{journey_project.name}/version", data={"message": "first version"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text

    # author -> version -> run -> artifact. A version is cut, never approved.
    # The run form offers a binding field for the file input stage.
    resp = client.get(f"/project/{journey_project.name}/runs/new")
    assert resp.status_code == 200
    assert 'name="binding__load"' in resp.text

    # Upload this week's file the way the form's Upload… does, then run bound to it
    # rather than to the path the workflow authored.
    bound = tmp_path / "this_week.csv"
    pd.DataFrame({"name": ["a", "b", "c"], "val": [1, 2, 5]}).to_csv(bound, index=False)
    with bound.open("rb") as handle:
        uploaded = client.post(f"/project/{journey_project.name}/files",
                               files={"file": ("this_week.csv", handle.read(), "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    resp = client.post(
        f"/project/{journey_project.name}/run",
        data={"binding__load": uploaded.json()["file_id"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    run_id = resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]

    # The run completed: every stage ok.
    status = client.get(f"/project/{journey_project.name}/runs/{run_id}/status").json()
    assert_run_ok(status, journey_project, run_id)
    assert status["terminal"] is True

    # The manifest records the binding's provenance: a run-supplied path, hashed.
    manifest = read_manifest(journey_project, run_id)
    binding = manifest["input_bindings"]["load"]
    assert binding["source"] == "run"
    assert binding["files"][0]["sha256"]

    # The published artifact exists on disk and the artifact route serves it.
    artifact = journey_project / "runs" / run_id / "artifacts" / "report" / "totals.csv"
    assert artifact.is_file(), f"report stage wrote no artifact at {artifact}"
    served = client.get(f"/project/{journey_project.name}/runs/{run_id}/artifact/report/totals.csv")
    assert served.status_code == 200

    # The served numbers come from the BOUND file: flagged = val > 1,
    # so totals are 1 (not flagged: a) and 7 (flagged: b + c).
    totals = pd.read_csv(io.StringIO(served.text)).set_index("flagged")["val"]
    assert totals[False] == 1
    assert totals[True] == 7


def test_report_stage_records_no_output_validation_issue(journey_project):
    client.post(f"/project/{journey_project.name}/version", data={"message": "first version"})
    resp = client.post(f"/project/{journey_project.name}/run", follow_redirects=False)
    assert resp.status_code == 303, resp.text
    run_id = resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]
    status = client.get(f"/project/{journey_project.name}/runs/{run_id}/status").json()
    assert_run_ok(status, journey_project, run_id)

    manifest = read_manifest(journey_project, run_id)
    record = {r["stage_id"]: r for r in manifest["stage_records"]}["report"]
    assert record["status"] == "ok"
    assert record["output_validation_report"]["issues"] == []


# ── Fixture: a real project directory with a python-only workflow ────────────

@pytest.fixture
def journey_project(tmp_path, monkeypatch):
    _point_examples_dir_at(tmp_path)
    # Run synchronously: the background thread is not the seam under test, and
    # the poll loop it exists for would only slow this test down.
    monkeypatch.setattr(run_service, "_run_in_background", lambda target, *args: target(*args))

    authored = tmp_path / "authored.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(authored, index=False)

    project_id = create_project(PROJECT, "Flag rows over the threshold and publish totals.",
                                source="smoke test").id
    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    for stage in _workflow_stages(str(authored)):
        add_stage(project_dir, stage)
    return project_dir


def _point_examples_dir_at(root) -> None:
    workspace.set_projects_dir(root)


def _workflow_stages(authored_path: str) -> list[dict]:
    load_schema = {
        "columns": [{"name": "name", "type": "str", "nullable": True}, {"name": "val", "type": "int", "nullable": True}],
    }
    # groupby("flagged", as_index=False)["val"].sum() — one row per flag value,
    # in that column order.
    totals_schema = {
        "columns": [{"name": "flagged", "type": "bool", "nullable": True}, {"name": "val", "type": "int", "nullable": True}],
    }
    return [
        {
            "id": "load", "description": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": authored_path, "format": "csv"}},
            "signature": {"form": "replaces", "produces": load_schema["columns"]},
        },
        {
            "id": "flag", "description": "Flag rows over threshold", "type": "python_row_function",
            "inputs": [{"id": "load"}],
            "function": {"kind": "inline", "code": (
                "def transform(row):\n"
                "    row[\"flagged\"] = row[\"val\"] > 1\n"
                "    return row\n"
            )},
            "signature": {"form": "extends",
                          "reads": [{"input": "load", "columns": load_schema["columns"]}],
                          "adds": [{"name": "flagged", "type": "bool", "nullable": True}]},
        },
        {
            "id": "totals", "description": "Total per flag", "type": "python_frame_function",
            "inputs": [{"id": "flag"}],
            "function": {"kind": "inline", "code": (
                "def transform(df):\n"
                "    return df.groupby(\"flagged\", as_index=False)[\"val\"].sum()\n"
            )},
            "signature": {"form": "replaces", "produces": totals_schema["columns"]},
        },
        {
            "id": "report", "description": "Publish totals", "type": "report",
            "inputs": [{"id": "totals"}],
            "report": {"format": "csv", "destination": "report/"},
            "signature": {"form": "replaces"},
            "function": {"kind": "inline", "code": (
                "import pandas as pd\n"
                "from pathlib import Path\n"
                "\n"
                "def transform(df, output_dir):\n"
                "    out = Path(output_dir) / \"totals.csv\"\n"
                "    df.to_csv(out, index=False)\n"
                "    return pd.DataFrame({\"artifact\": [str(out)]})\n"
            )},
        },
    ]
