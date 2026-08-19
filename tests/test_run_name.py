from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.core.run_status import RunStatus, StageStatus
from app.models.run_manifest import StageRecord
from app.models.run_parameters import RunParameters
from app.runtime.manifest import PRODUCTION_RUNS, RunManifest, read_run_manifest, write_manifest
from app.services import workspace
from app.web.config import templates
from app.web.run_header import build_run_header

client = TestClient(app)


def _manifest(project: str, run_id: str, *, name: str = "") -> RunManifest:
    return RunManifest(
        id=RunManifest.compose_id(project, run_id, PRODUCTION_RUNS),
        run_id=run_id,
        started_at="2026-08-19T09:56:35",
        project=project,
        workflow_version="v1",
        parameters=RunParameters(),
        input_bindings={},
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.ERRORS,
        stage_records=[
            StageRecord(
                stage_id="load",
                type="input_data",
                status=StageStatus.OK,
                input_validation_report=[],
                output_validation_report=None,
                output_row_count=1,
            ),
        ],
        name=name,
    )


def _render_header(project: str, run_id: str, tmp_path: Path, *, name: str = "") -> str:
    workspace.set_projects_dir(tmp_path)
    run_dir = tmp_path / project / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(project, run_id, name=name).to_dict()
    header = build_run_header(project, run_id, run_dir, manifest)
    return templates.env.get_template("_run_header.html").render(
        project=project, run_id=run_id, header=header, name_max_length=80
    )


def test_a_named_run_renders_its_name_with_the_pencil_beside_it(tmp_path: Path) -> None:
    html = _render_header("demo", "20260819T095635", tmp_path, name="Funsies!")

    assert 'class="run-name">Funsies!</h1>' in html
    assert 'aria-label="Edit run name"' in html
    assert html.index('class="run-name">Funsies!</h1>') < html.index('aria-label="Edit run name"')
    assert 'name="name"' in html
    assert 'value="Funsies!"' in html
    assert html.index('name="name"') < html.index(">Save<")
    assert "Clearing the name removes it." in html


def test_posting_a_run_name_updates_the_manifest_and_redirects(tmp_path: Path) -> None:
    workspace.set_projects_dir(tmp_path)
    project = "demo"
    run_id = "20260819T095635"
    write_manifest(_manifest(project, run_id))

    response = client.post(
        f"/project/{project}/runs/{run_id}/name",
        data={"name": "Funsies!"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/project/{project}/runs/{run_id}"
    assert read_run_manifest(project, run_id).name == "Funsies!"


def test_clearing_a_run_name_removes_it(tmp_path: Path) -> None:
    workspace.set_projects_dir(tmp_path)
    project = "demo"
    run_id = "20260819T095635"
    write_manifest(_manifest(project, run_id, name="Funsies!"))

    response = client.post(
        f"/project/{project}/runs/{run_id}/name",
        data={"name": "  "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert read_run_manifest(project, run_id).name == ""
