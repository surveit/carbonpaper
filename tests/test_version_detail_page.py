"""Route tests for the read-only version-detail page and run-this-version."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services import versioning
from app.services import project as project_service
from app.models.review_guide import ReviewGuideStep
from app.services.versioning import ReviewGuide
from app.services import workspace

client = TestClient(app)


# The columns of the CSV the `project` fixture writes; every non-publish stage
# must declare its output_schema (app/models/stage.py: Stage._schemas_declared).
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
    compiled = pdir / "compiled"
    compiled.mkdir(parents=True)
    data = pdir / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    (compiled / "01_load.json").write_text(json.dumps(_stage(data)), encoding="utf-8")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return pdir


def test_version_detail_renders_frozen_graph_and_publish(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project, message="v1", reviewer="local")
    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")
    assert page.status_code == 200
    assert meta.version_id in page.text
    assert "mermaid" in page.text          # the graph rendered
    assert "/publish" in page.text          # unpublished → Publish control present
    assert "Run this version" in page.text  # ...and runnable all the same
    # The way back is the Versions rung of the header trail, not a button of this
    # page's own — no page carries a back link of its own any more.
    assert 'href="/project/demo/workflow/versions" class="crumb-link"' in page.text


def test_version_detail_does_not_offer_to_generate_a_guide(project: Path) -> None:
    """A guide is read beside a run's measured stages, so a run is where it is asked for."""
    meta = project_service.save_working_copy_as_version(project, message="v1", reviewer="local")

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert 'data-role="generate-guide"' not in page.text


def test_version_detail_labels_the_authored_description(project: Path) -> None:
    """The author's sentence sits under its own header, not inside the app's lede."""
    meta = project_service.save_working_copy_as_version(
        project, message="Nine flat categories, no severity.", reviewer="local"
    )

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert "vd-desc-kicker" in page.text
    assert "Nine flat categories, no severity." in page.text
    # The boilerplate no longer runs into the authored half.
    assert "A read-only snapshot of the workflow. Nine flat" not in page.text


def test_version_detail_says_when_no_description_was_written(project: Path) -> None:
    """An absent description is a fact about the version, so it is stated, not filled in."""
    meta = project_service.save_working_copy_as_version(project, message="", reviewer="local")

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert "this version was cut without one" in page.text


def _save_covering_guide(project_dir: Path, version_id: str) -> None:
    """A guide narrating every stage of the version in one step."""
    stages = versioning.load_version(project_dir, version_id).stages
    versioning.save_version_guide(
        project_dir,
        version_id,
        ReviewGuide(project=project_dir.name, version_id=version_id, steps=[ReviewGuideStep(
            title="How this workflow works",
            prose="Every stage, narrated together.",
            stage_ids=[stage.id for stage in stages],
            data_description="Every row the workflow ends with.",
        )]),
    )


def test_version_detail_drops_the_offer_once_a_guide_exists(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project, message="v1", reviewer="local")
    _save_covering_guide(project, meta.version_id)

    page = client.get(f"/project/demo/workflow/version/{meta.version_id}")

    assert 'data-role="generate-guide"' not in page.text


def test_version_detail_404_for_unknown_version(project: Path) -> None:
    assert client.get("/project/demo/workflow/version/20990101T000000").status_code == 404


def test_run_this_version_404_for_nonexistent_version(project: Path) -> None:
    resp = client.post(
        "/project/demo/workflow/version/20990101T000000/run", follow_redirects=False
    )
    assert resp.status_code == 404


def test_run_this_version_runs_whether_or_not_it_is_published(project: Path) -> None:
    """The same version runs before and after a human publishes it."""
    meta = project_service.save_working_copy_as_version(project, message="v1", reviewer="local")
    vid = meta.version_id

    unpub = client.post(f"/project/demo/workflow/version/{vid}/run", follow_redirects=False)
    assert unpub.status_code == 303
    assert "/runs/" in unpub.headers["location"]

    versioning.publish_version(project, vid, reviewer="local")
    pub = client.post(f"/project/demo/workflow/version/{vid}/run", follow_redirects=False)
    assert pub.status_code == 303
    assert "/runs/" in pub.headers["location"]


def test_run_this_version_400s_not_500s_on_unbound_input(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version whose input stage authors no path (the workflow
    leaves it for a run binding, per Connector's own docstring) is not
    run-ready — prepare_run raises MissingInputBindingError. The route must
    report this as a 400, the same way trigger_run does, not let it fall
    through to an unhandled 500."""
    unbound_stage = {
        "id": "load", "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
    }
    meta = versioning.create_version_from_stages(
        project, [unbound_stage], message="v-unbound", reviewer="local")
    vid = meta.version_id

    resp = client.post(f"/project/demo/workflow/version/{vid}/run", follow_redirects=False)
    assert resp.status_code == 400
    assert "no file bound" in resp.json()["detail"]
