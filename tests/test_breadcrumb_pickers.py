"""Every picker href a Crumb carries must resolve to a route that answers.

`/project/x/runs/picker` was shadowed by `/project/{project}/runs/{run_id}`, which
matched it as a run named "picker" — the rung was dead and only a popover said so.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.web import breadcrumbs
from app.services import project as project_service
from app.services import workspace

client = TestClient(app)

_ROWS_SCHEMA = [{"name": "name", "type": "str", "nullable": False}]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    pdir = tmp_path / "demo"
    compiled = pdir / "compiled"
    compiled.mkdir(parents=True)
    data = pdir / "a.csv"
    pd.DataFrame({"name": ["x"]}).to_csv(data, index=False)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(data), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _ROWS_SCHEMA},
    }), encoding="utf-8")
    workspace.set_projects_dir(tmp_path)
    return pdir


def _picker_hrefs(crumbs: list[breadcrumbs.Crumb]) -> list[str]:
    return [crumb.picker for crumb in crumbs if crumb.picker]


def test_every_switcher_rung_points_at_a_route_that_answers(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project, message="v1", reviewer="local")
    trails = [
        breadcrumbs.build_section_crumbs("demo", label="Runs"),
        breadcrumbs.build_version_crumbs("demo", meta.version_id),
        breadcrumbs.build_run_crumbs("demo", "20260805T144252"),
        breadcrumbs.build_run_child_crumbs("demo", "20260805T144252", label="Review queue"),
    ]
    hrefs = sorted({href for trail in trails for href in _picker_hrefs(trail)})
    assert hrefs, "no switcher rungs at all — this test would pass vacuously"
    for href in hrefs:
        assert client.get(href).status_code == 200, f"{href} does not resolve"


def test_the_version_rung_lists_versions_with_their_publish_state(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(
        project, message="Nine flat categories.", reviewer="local"
    )
    trail = breadcrumbs.build_version_crumbs("demo", meta.version_id)

    body = client.get(_picker_hrefs(trail)[-1], params={"current": meta.version_id}).text

    assert meta.version_id in body
    assert "Nine flat categories." in body
    assert "unpublished" in body


def test_a_version_with_no_message_is_said_to_have_none(project: Path) -> None:
    """The picker never writes a description the stored version does not carry."""
    meta = project_service.save_working_copy_as_version(project, message="", reviewer="local")
    trail = breadcrumbs.build_version_crumbs("demo", meta.version_id)

    body = client.get(_picker_hrefs(trail)[-1]).text

    assert "No description" in body


def test_only_the_project_and_the_leaf_rung_switch(project: Path) -> None:
    """A section rung names one place, so there is nothing to switch between."""
    trail = breadcrumbs.build_run_crumbs("demo", "20260805T144252")

    switchers = [crumb.label for crumb in trail if crumb.picker]

    assert switchers == ["demo", "20260805T144252"]


def test_a_run_child_page_does_not_switch_the_run_it_hangs_off(project: Path) -> None:
    """On a queue or rows page the run is context, not the thing being read."""
    trail = breadcrumbs.build_run_child_crumbs("demo", "20260805T144252", label="Review queue")

    assert [crumb.label for crumb in trail if crumb.picker] == ["demo"]
    assert trail[-1].label == "Review queue" and trail[-1].href is None
