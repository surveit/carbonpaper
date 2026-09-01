"""A run's stage views must describe the version the run PINNED, never the live
`compiled/` working copy: the stage panel's source and schemas, the lineage
panel's transform, and the scratch re-run's handler. When the pinned version
cannot be resolved the panels say so and the scratch re-run refuses to execute.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
from app.main import app
from app.runtime.runner import execute_run
from app.services import project as project_service
from conftest import pinned_stages
from stage_seed import add_stage
from run_seed import read_manifest, store_manifest

client = TestClient(app)

PROJECT = "stage_view_pinning"
LOAD_ID = "load"
CLASSIFY_ID = "classify"
PINNED_MARKER = "PINNED_LABEL"
DRIFTED_MARKER = "DRIFTED_LABEL"


def _load_stage(data_path: Path) -> dict:
    return {
        "id": LOAD_ID, "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(data_path), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "name", "type": "str", "nullable": True},
                {"name": "val", "type": "int", "nullable": True},
            ],
        },
    }


def _classify_stage(marker: str) -> dict:
    return {
        "id": CLASSIFY_ID, "description": f"Classify ({marker})",
        "type": "python_row_function",
        "inputs": [{"id": LOAD_ID}],
        "function": {"kind": "inline",
                     "code": f'def transform(row):\n    return {{**row, "label": "{marker}"}}\n'},
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [
                        {"name": "name", "type": "str", "nullable": True},
                        {"name": "val", "type": "int", "nullable": True},
                    ],
                },
            ],
            "adds": [{"name": "label", "type": "str", "nullable": True}],
        },
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / PROJECT
    pdir.mkdir(parents=True, exist_ok=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(data, index=False)
    add_stage(pdir, _load_stage(data))
    add_stage(pdir, _classify_stage(PINNED_MARKER))
    workspace.set_projects_dir(tmp_path)
    return pdir


def _run_once(project_dir: Path) -> str:
    project_service.save_working_copy_as_version(project_dir.name, message="v1")
    return str(execute_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir))["run_id"])


def _drift_the_working_copy(project_dir: Path) -> None:
    add_stage(project_dir, _classify_stage(DRIFTED_MARKER))


def _unpin_the_run(project_dir: Path, run_id: str) -> None:
    """Drops workflow_version — the shape a pre-versioning run really has on disk."""
    manifest = read_manifest(project_dir, run_id)
    manifest["workflow_version"] = None
    store_manifest(project_dir, run_id, manifest)


def _stage_panel(run_id: str):
    return client.get(f"/project/{PROJECT}/runs/{run_id}/stage/{CLASSIFY_ID}/partial")


def _lineage_panel(run_id: str):
    return client.get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{CLASSIFY_ID}/lineage_panel?row=0")


def _simulate_page(run_id: str):
    return client.get(f"/project/{PROJECT}/runs/{run_id}/stage/{CLASSIFY_ID}/simulate")


def _scratch_preview(run_id: str):
    return client.post(
        f"/project/{PROJECT}/runs/{run_id}/stage/{CLASSIFY_ID}/preview",
        json={"indices": [0]})


# ─── The regression: stage views track the pinned version, not compiled/ ─────

def test_stage_panel_shows_the_source_that_ran_after_the_working_copy_drifts(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)

    panel = _stage_panel(run_id)
    assert panel.status_code == 200
    assert PINNED_MARKER in panel.text
    assert DRIFTED_MARKER not in panel.text


def test_lineage_panel_shows_the_transform_that_ran_after_drift(project: Path) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)

    panel = _lineage_panel(run_id)
    assert panel.status_code == 200
    assert PINNED_MARKER in panel.text
    assert DRIFTED_MARKER not in panel.text


def test_scratch_preview_executes_the_stage_that_ran_not_the_working_copy(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)

    body = _scratch_preview(run_id).json()
    assert body["ok"] is True
    assert [row["label"] for row in body["preview"]] == [PINNED_MARKER]


# ─── One Transform pane, holding the pinned definition ──────────────────────

def test_the_transform_pane_holds_the_pinned_definition_and_links_the_simulator(
    project: Path,
) -> None:
    run_id = _run_once(project)

    html = _stage_panel(run_id).text
    assert 'data-pane="transform"' in html
    pane = html.split('data-pane="transform"', 1)[1]
    pane = pane.split("data-pane=", 1)[0]
    assert PINNED_MARKER in pane
    assert f"/stage/{CLASSIFY_ID}/simulate" in pane


# ─── Unresolvable pinned version: loud and visible, never a substitute ───────

def test_stage_panel_says_the_definition_is_unavailable_when_the_run_is_unpinned(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    _unpin_the_run(project, run_id)

    panel = _stage_panel(run_id)
    assert panel.status_code == 200
    assert "Stage definition unavailable" in panel.text
    assert "records no workflow version" in panel.text
    assert DRIFTED_MARKER not in panel.text


def test_lineage_panel_says_the_definition_is_unavailable_when_the_run_is_unpinned(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    _unpin_the_run(project, run_id)

    panel = _lineage_panel(run_id)
    assert panel.status_code == 200
    assert "Stage definition unavailable" in panel.text
    assert DRIFTED_MARKER not in panel.text


def test_scratch_preview_refuses_to_execute_an_unresolvable_version(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    _unpin_the_run(project, run_id)

    response = _scratch_preview(run_id)
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert "records no workflow version" in body["error"]
    assert "preview" not in body


def test_stage_panel_hides_the_simulate_link_when_the_definition_is_unavailable(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _unpin_the_run(project, run_id)

    panel = _stage_panel(run_id)
    assert "/simulate" not in panel.text


def test_stage_panel_leaves_the_validation_lines_to_the_run_page_index(
    project: Path,
) -> None:
    run_id = _run_once(project)

    panel = _stage_panel(run_id)
    assert 'class="validation-block"' not in panel.text


def test_the_simulate_page_refuses_a_run_whose_version_cannot_be_read(
    project: Path,
) -> None:
    run_id = _run_once(project)
    _unpin_the_run(project, run_id)

    assert _simulate_page(run_id).status_code == 404


def test_the_simulate_page_offers_the_rows_and_says_nothing_is_saved(
    project: Path,
) -> None:
    run_id = _run_once(project)

    page = _simulate_page(run_id)
    assert page.status_code == 200
    assert 'class="row-pick"' in page.text
    assert "Run transform on selected" in page.text
    assert "Nothing here is saved" in page.text
