"""A run page's workflow graph must come from the version the run PINNED, never
from the live `compiled/` working copy, which drifts as it is edited. When the
pinned version cannot be resolved the page says so and draws NO graph.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
from app.core.errors import RunVersionUnresolvableError
from app.main import app
from app.runtime.runner import execute_run
from app.services import project as project_service
from app.services.run import load_run_workflow
from conftest import pinned_stages
from stage_seed import add_stage
from run_seed import read_manifest, store_manifest

client = TestClient(app)

PROJECT = "graph_pinning"
PINNED_ID = "pinned_stage"
DRIFTED_ID = "drifted_stage"


def _input_stage(stage_id: str, name: str, data_path: Path) -> dict:
    return {
        "id": stage_id, "description": name, "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(data_path), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "name", "type": "str", "nullable": False},
                {"name": "val", "type": "int", "nullable": False},
            ],
        },
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / PROJECT
    pdir.mkdir(parents=True, exist_ok=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(data, index=False)
    add_stage(pdir, _input_stage(PINNED_ID, "Pinned stage", data))
    workspace.set_projects_dir(tmp_path)
    return pdir


def _run_once(project_dir: Path) -> str:
    project_service.save_working_copy_as_version(project_dir.name, message="v1")
    return str(execute_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir))["run_id"])


def _drift_the_working_copy(project_dir: Path) -> None:
    add_stage(project_dir, _input_stage(DRIFTED_ID, "Drifted stage",
                                project_dir / "rows.csv"))


def _rewrite_manifest(project_dir: Path, run_id: str, **changes: object) -> None:
    manifest = read_manifest(project_dir, run_id)
    manifest.update(changes)
    store_manifest(project_dir, run_id, manifest)


# ─── The regression: the graph tracks the pinned version, not compiled/ ──────

def test_run_page_graph_stays_on_the_pinned_version_after_the_working_copy_drifts(
    project: Path,
) -> None:
    run_id = _run_once(project)
    before = client.get(f"/project/{PROJECT}/runs/{run_id}")
    assert before.status_code == 200
    assert PINNED_ID in before.text

    _drift_the_working_copy(project)

    after = client.get(f"/project/{PROJECT}/runs/{run_id}")
    assert after.status_code == 200
    # The graph is unchanged by the edit: still the stage that ran, and no trace
    # of the stage the working copy now holds.
    assert PINNED_ID in after.text
    assert DRIFTED_ID not in after.text


def test_status_poller_graph_stays_on_the_pinned_version(project: Path) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)

    payload = client.get(f"/project/{PROJECT}/runs/{run_id}/status").json()
    assert payload["graph_error"] is None
    assert PINNED_ID in payload["mermaid"]
    assert DRIFTED_ID not in payload["mermaid"]


def test_row_trace_view_graph_stays_on_the_pinned_version(project: Path) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)

    page = client.get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{PINNED_ID}/row/0/trace/view")
    assert page.status_code == 200
    assert DRIFTED_ID not in page.text
    # The trace's node detail resolves against the pinned stages, so the graph
    # covers every traced node and renders. Read off the working copy the traced
    # stage id would be unknown and the page would fall back to no graph at all.
    assert "flowchart LR" in page.text
    assert f'click {PINNED_ID} call dvNode' in page.text


# ─── Unresolvable pinned version: loud and visible, never a substitute ───────

def test_run_page_says_the_graph_is_unavailable_when_the_version_is_missing(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    _rewrite_manifest(project, run_id, workflow_version="20990101T000000")

    page = client.get(f"/project/{PROJECT}/runs/{run_id}")
    assert page.status_code == 200
    assert "Workflow graph unavailable" in page.text
    assert "20990101T000000" in page.text
    # The point of the whole fix: the working copy is NOT drawn in its place.
    assert DRIFTED_ID not in page.text
    assert "flowchart LR" not in page.text


def test_run_with_a_null_pinned_version_shows_no_graph(project: Path) -> None:
    """A subset run really does record a null version — a real state, not a corrupt manifest."""
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    _rewrite_manifest(project, run_id, workflow_version=None)

    page = client.get(f"/project/{PROJECT}/runs/{run_id}")
    assert page.status_code == 200
    assert "Workflow graph unavailable" in page.text
    assert "records no workflow version" in page.text
    assert DRIFTED_ID not in page.text
    assert "flowchart LR" not in page.text


def test_status_poller_reports_the_unresolvable_version_instead_of_a_graph(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    _rewrite_manifest(project, run_id, workflow_version=None)

    payload = client.get(f"/project/{PROJECT}/runs/{run_id}/status").json()
    assert payload["mermaid"] == ""
    assert "records no workflow version" in payload["graph_error"]


# ─── The loader seam itself ─────────────────────────────────────────────────

def test_load_run_workflow_reads_the_pinned_version(project: Path) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    manifest = read_manifest(project, run_id)

    workflow = load_run_workflow(PROJECT, manifest)
    assert [s.id for s in workflow.stages] == [PINNED_ID]


def test_load_run_workflow_raises_rather_than_falling_back(project: Path) -> None:
    with pytest.raises(RunVersionUnresolvableError, match="records no workflow version"):
        load_run_workflow(PROJECT, {"workflow_version": None})
    with pytest.raises(RunVersionUnresolvableError, match="could not be read"):
        load_run_workflow(PROJECT, {"workflow_version": "20990101T000000"})
