"""Route tests for the read-only version-detail page and run-this-version."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services import versioning
from app.services import project as project_service
from app.models.review_guide import ReviewGuideStep
from app.models.records.review_guide import ReviewGuide
from app.services import workspace
from stage_seed import add_stage

client = TestClient(app)


# The columns of the CSV the `project` fixture writes; Stage._schemas_declared wants them.
_ROWS_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": False},
                            {"name": "val", "type": "int", "nullable": False}]}


def _stage(data_path: Path) -> dict:
    return {
        "id": "load", "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(data_path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / "demo"
    compiled = pdir
    compiled.mkdir(parents=True, exist_ok=True)
    data = pdir / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    add_stage(compiled, _stage(data))
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return pdir


def test_version_detail_renders_the_frozen_graph(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")
    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")
    assert page.status_code == 200
    assert meta.version_id in page.text
    assert "mermaid" in page.text          # the graph rendered
    assert "Run this version" in page.text  # ...and runnable all the same
    # The way back is the Versions rung of the header trail, not a button of this
    # page's own — no page carries a back link of its own any more.
    assert 'href="/project/demo/workflow/versions" class="crumb-link"' in page.text


def test_version_detail_does_not_offer_to_generate_a_guide(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert 'data-role="generate-guide"' not in page.text


def test_the_description_is_the_page_heading(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="Nine flat categories, no severity."
    )

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert 'class="vd-name"' in page.text
    assert "Nine flat categories, no severity." in page.text


def test_version_detail_says_when_no_description_was_written(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="")

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert "Saved without a description" in page.text


def _save_covering_guide(project_dir: Path, version_id: str) -> None:
    stages = versioning.load_version(project_dir.name, version_id).stages
    versioning.save_version_guide(project_dir.name,
        version_id,
        ReviewGuide(project=project_dir.name, version_id=version_id,
                    steps=[ReviewGuideStep(
            title="How this workflow works",
            prose="Every stage, narrated together.",
            stage_ids=[stage.id for stage in stages],
            data_description="Every row the workflow ends with.",
        )]),
    )


def test_version_detail_drops_the_offer_once_a_guide_exists(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")
    _save_covering_guide(project, meta.version_id)

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert 'data-role="generate-guide"' not in page.text


def test_version_detail_404_for_unknown_version(project: Path) -> None:
    assert client.get("/project/demo/workflow/version/20990101T000000").status_code == 404


def test_run_this_version_opens_the_run_form_on_this_version(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")
    vid = meta.version_id

    page = client.get(f"/project/demo/workflow/version/{vid}")

    # A link, not a form: the run's file bindings and row caps are picked on the
    # run form, and this page cannot pick them.
    assert f'href="/project/demo/runs/new?version_id={vid}"' in page.text
    assert f'action="/project/demo/workflow/version/{vid}/run"' not in page.text
    form = client.get(f"/project/demo/runs/new?version_id={vid}")
    assert form.status_code == 200
    assert f'value="{vid}" selected' in form.text


def test_run_this_version_is_offered(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")
    vid = meta.version_id
    link = f'href="/project/demo/runs/new?version_id={vid}"'

    assert link in client.get(f"/project/demo/workflow/version/{vid}").text
