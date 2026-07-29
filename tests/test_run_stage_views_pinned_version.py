"""A run's stage views must describe the version the run PINNED, never the live
`compiled/` working copy: the stage panel's source and schemas, the lineage
panel's transform, and the scratch re-run's handler. When the pinned version
cannot be resolved the panels say so and the scratch re-run refuses to execute.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.config as web_config
import app.services.workspace as workspace
import app.web.loading as loading
import app.web.routers.project as project_router
import app.web.routers.runs as runs_router
from app.main import app
from app.runtime.runner import execute_run
from app.services import versioning

client = TestClient(app)

PROJECT = "stage_view_pinning"
LOAD_ID = "load"
CLASSIFY_ID = "classify"
PINNED_MARKER = "PINNED_LABEL"
DRIFTED_MARKER = "DRIFTED_LABEL"


def _load_stage(data_path: Path) -> dict:
    return {
        "id": LOAD_ID, "name": "Load rows", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(data_path), "format": "csv"}},
        "output_schema": {"columns": [{"name": "name", "type": "str"},
                                      {"name": "val", "type": "int"}]},
    }


def _classify_stage(marker: str) -> dict:
    """A python_row_function whose only observable difference between the pinned
    version and the drifted working copy is the label it writes."""
    return {
        "id": CLASSIFY_ID, "name": f"Classify ({marker})",
        "type": "python_row_function",
        "inputs": [{"id": LOAD_ID,
                    "schema": {"columns": [{"name": "name", "type": "str"},
                                           {"name": "val", "type": "int"}]}}],
        "function": {"kind": "inline",
                     "code": f'def transform(row):\n    return {{**row, "label": "{marker}"}}\n'},
        "output_schema": {"columns": [{"name": "name", "type": "str"},
                                      {"name": "val", "type": "int"},
                                      {"name": "label", "type": "str"}]},
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / PROJECT
    (pdir / "compiled").mkdir(parents=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(data, index=False)
    (pdir / "compiled" / "01_load.json").write_text(
        json.dumps(_load_stage(data)), encoding="utf-8")
    (pdir / "compiled" / "02_classify.json").write_text(
        json.dumps(_classify_stage(PINNED_MARKER)), encoding="utf-8")
    for mod in (web_config, workspace, loading, project_router, runs_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)
    return pdir


def _run_once(project_dir: Path) -> str:
    """Snapshot + publish the working copy, run it, and return the run id."""
    version_id = versioning.create_version_from_disk(
        project_dir, message="v1", reviewer="test").version_id
    versioning.publish_version(project_dir, version_id, reviewer="test")
    return str(execute_run(project_dir, repo_root=project_dir)["run_id"])


def _drift_the_working_copy(project_dir: Path) -> None:
    """Edit the stage in place, as an author would after the run. Nothing
    re-versions it, so the run's pinned version and the working copy disagree
    on what `classify` does."""
    (project_dir / "compiled" / "02_classify.json").write_text(
        json.dumps(_classify_stage(DRIFTED_MARKER)), encoding="utf-8")


def _unpin_the_run(project_dir: Path, run_id: str) -> None:
    """Drop the manifest's workflow_version, the shape a pre-versioning run has:
    what it executed is unknowable."""
    path = project_dir / "runs" / run_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["workflow_version"] = None
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _stage_panel(run_id: str):
    return client.get(f"/project/{PROJECT}/runs/{run_id}/stage/{CLASSIFY_ID}/partial")


def _lineage_panel(run_id: str):
    return client.get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{CLASSIFY_ID}/lineage_panel?row=0")


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
    """The scratch re-run reads THIS run's rows and is labelled as this run's
    transform, so it must execute the pinned handler — otherwise the panel
    reports output that this run's workflow could never produce."""
    run_id = _run_once(project)
    _drift_the_working_copy(project)

    body = _scratch_preview(run_id).json()
    assert body["ok"] is True
    assert [row["label"] for row in body["preview"]] == [PINNED_MARKER]


# ─── Transform reads the same under either tier ─────────────────────────────

def test_transform_pane_serves_both_the_schema_and_current_run_tiers(
    project: Path,
) -> None:
    """The definition the run pinned IS what the current run transformed with,
    so one pane answers both tiers. Two panes would leave "Current run ›
    Transform" — the tier the panel opens on — showing less than Schema does."""
    run_id = _run_once(project)

    html = _stage_panel(run_id).text
    assert 'data-pane="schema-transform run-transform"' in html
    assert 'data-pane="schema-transform"' not in html
    assert 'data-pane="run-transform"' not in html
    # The handle and the scratch re-run result both live in that one pane.
    pane = html.split('data-pane="schema-transform run-transform"', 1)[1]
    pane = pane.split("data-pane=", 1)[0]
    assert PINNED_MARKER in pane
    assert 'class="scratch-result"' in pane


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
    # The row's own output data is still true, so it still renders.
    assert "Output · row 0" in panel.text


def test_scratch_preview_refuses_to_execute_an_unresolvable_version(
    project: Path,
) -> None:
    """Executing the working copy here would run code this run never ran and
    label the result as this run's. Refuse instead."""
    run_id = _run_once(project)
    _drift_the_working_copy(project)
    _unpin_the_run(project, run_id)

    response = _scratch_preview(run_id)
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert "records no workflow version" in body["error"]
    assert "preview" not in body


def test_stage_panel_hides_the_scratch_button_when_the_definition_is_unavailable(
    project: Path,
) -> None:
    """The panel must not offer a re-run it would refuse."""
    run_id = _run_once(project)
    _unpin_the_run(project, run_id)

    panel = _stage_panel(run_id)
    assert "Run transform on selected" not in panel.text
