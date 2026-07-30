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
from app.services.versioning import list_versions
from conftest import save_covering_guide

client = TestClient(app)

PROJECT = "smoke_journey"


def assert_run_ok(status: dict, project_dir, run_id: str) -> None:
    """Assert the run finished `ok`; on anything else, fail with the
    manifest's per-stage problem records spelled out in full. The bare
    `assert ..., status` form is useless in CI: pytest truncates the status
    dict's repr, so the one line that says WHICH stage failed and WHY never
    reaches the log."""
    if status.get("status") == "ok":
        return
    detail = "manifest.json not found"
    manifest_path = project_dir / "runs" / run_id / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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


def test_offline_journey_reaches_a_published_artifact(journey_project, tmp_path):
    # Version the working copy through the web endpoint.
    resp = client.post(f"/project/{PROJECT}/version", data={"message": "first version"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text

    # Publish it — a run pins a PUBLISHED version, so the human-approval step is
    # part of the journey: author -> version -> guide -> publish -> run -> artifact.
    # Publishing is gated on a review guide, so one is written before the POST.
    version_id = list_versions(journey_project)[0].version_id
    save_covering_guide(journey_project, version_id)
    resp = client.post(f"/project/{PROJECT}/versions/{version_id}/publish",
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text

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
    assert_run_ok(status, journey_project, run_id)
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
    monkeypatch.setattr(run_service, "_run_in_background", lambda target, *args: target(*args))

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
    # groupby("flagged", as_index=False)["val"].sum() — one row per flag value,
    # in that column order.
    totals_schema = {
        "columns": [{"name": "flagged", "type": "bool"}, {"name": "val", "type": "int"}],
        "primary_key": ["flagged"],
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
            "output_schema": totals_schema,
        },
        {
            "id": "report", "name": "Publish totals", "type": "publish",
            "inputs": [{"id": "totals", "schema": totals_schema}],
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
