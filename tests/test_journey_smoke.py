"""End-to-end smoke gate: the offline journey from an authored working copy to a
published artifact, driven through the real web endpoints.

The journey under test — the product's core promise, minus the LLM steps
(conftest forces the LLM offline; LLM-stage coverage needs a real key and runs
on demand, not in CI):

    create project -> author a python-only workflow (compiled/*.json, the app's
    own storage convention) -> POST /version -> run form offers a binding field
    -> POST /run binding a run-time input file -> every stage reaches `ok` ->
    the publish stage's artifact exists on disk, is served by the artifact
    route, and carries numbers computed from the bound file.

Any break on this path is a product outage even when every unit test is green,
so this test fails at the first seam that regresses.
"""
from __future__ import annotations

import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.runs as runs_router
from app.main import app
from app.services.project import create_project

client = TestClient(app)

PROJECT = "smoke_journey"


def test_offline_journey_reaches_a_published_artifact(journey_project, tmp_path):
    # Version the working copy through the web endpoint.
    resp = client.post(f"/project/{PROJECT}/version", data={"message": "first version"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text

    # The run form offers a binding field for the file input stage.
    resp = client.get(f"/project/{PROJECT}/runs")
    assert resp.status_code == 200
    assert 'name="binding__load"' in resp.text

    # Trigger a run bound to a run-time input file (not the authored one).
    bound = tmp_path / "this_week.csv"
    pd.DataFrame({"name": ["a", "b", "c"], "val": [1, 2, 5]}).to_csv(bound, index=False)
    resp = client.post(
        f"/project/{PROJECT}/run",
        data={"binding__load": str(bound)},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    run_id = resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]

    # The run completed: every stage ok.
    status = client.get(f"/project/{PROJECT}/runs/{run_id}/status").json()
    assert status["status"] == "ok", status
    assert status["terminal"] is True

    # The manifest records the binding's provenance: a run-supplied path, hashed.
    manifest = json.loads(
        (journey_project / "runs" / run_id / "manifest.json").read_text(encoding="utf-8")
    )
    binding = manifest["input_bindings"]["load"]
    assert binding["source"] == "run"
    assert binding["sha256"]

    # The published artifact exists on disk and the artifact route serves it.
    artifact = journey_project / "runs" / run_id / "artifacts" / "report" / "totals.csv"
    assert artifact.is_file(), f"publish stage wrote no artifact at {artifact}"
    served = client.get(f"/project/{PROJECT}/runs/{run_id}/artifact/report/totals.csv")
    assert served.status_code == 200

    # The served numbers come from the BOUND file: flagged = val > 1,
    # so totals are 1 (not flagged: a) and 7 (flagged: b + c).
    totals = pd.read_csv(io.StringIO(served.text)).set_index("flagged")["val"]
    assert totals[False] == 1
    assert totals[True] == 7


# ── Fixture: a real project directory with a python-only workflow ────────────

@pytest.fixture
def journey_project(tmp_path, monkeypatch):
    _point_examples_dir_at(monkeypatch, tmp_path)
    # Run synchronously: the background thread is not the seam under test, and
    # the poll loop it exists for would only slow this test down.
    monkeypatch.setattr(runs_router, "run_in_background", lambda target, *args: target(*args))

    authored = tmp_path / "authored.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(authored, index=False)

    create_project(PROJECT, "Flag rows over the threshold and publish totals.",
                   source="smoke test")
    project_dir = tmp_path / PROJECT
    (project_dir / "compiled").mkdir()
    for position, stage in enumerate(_workflow_stages(str(authored)), start=1):
        path = project_dir / "compiled" / f"{position:02d}_{stage['id']}.json"
        path.write_text(json.dumps(stage), encoding="utf-8")
    return project_dir


def _point_examples_dir_at(monkeypatch, root) -> None:
    """Point the projects storage root at `root`. EXAMPLES_DIR is imported
    by value into each consuming module, so every participant on the journey
    needs its own copy patched."""
    for module in (
        "app.services.workspace",
        "app.web.config",
        "app.web.loading",
        "app.web.routers.runs",
        "app.web.routers.node_review",
        "app.web.routers.project",
    ):
        monkeypatch.setattr(f"{module}.EXAMPLES_DIR", root)


def _workflow_stages(authored_path: str) -> list[dict]:
    """A minimal workflow using one stage of each non-LLM executable family:
    file input -> per-row transform -> frame reshape -> publish."""
    load_schema = {
        "columns": [{"name": "name", "type": "str"}, {"name": "val", "type": "int"}],
        "primary_key": ["name"],
    }
    flag_schema = {
        "columns": [{"name": "name", "type": "str"}, {"name": "val", "type": "int"},
                    {"name": "flagged", "type": "bool"}],
        "primary_key": ["name"],
    }
    return [
        {
            "id": "load", "name": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": authored_path, "format": "csv"}},
            "output_schema": load_schema,
        },
        {
            "id": "flag", "name": "Flag rows over threshold", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": load_schema}],
            "function": {"kind": "inline", "code": (
                "def transform(row):\n"
                "    row[\"flagged\"] = row[\"val\"] > 1\n"
                "    return row\n"
            )},
            "output_schema": flag_schema,
        },
        {
            "id": "totals", "name": "Total per flag", "type": "python_frame_function",
            "inputs": [{"id": "flag", "schema": flag_schema}],
            "function": {"kind": "inline", "code": (
                "def transform(df):\n"
                "    return df.groupby(\"flagged\", as_index=False)[\"val\"].sum()\n"
            )},
        },
        {
            "id": "report", "name": "Publish totals", "type": "publish",
            "inputs": [{"id": "totals"}],
            "publish": {"format": "csv", "destination": "report/"},
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
