"""Every picker href a Crumb carries must resolve to a route that answers.

`/project/x/runs/picker` was shadowed by `/project/{project}/runs/{run_id}`, which
matched it as a run named "picker" — the rung was dead and only a popover said so.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.web import breadcrumbs
from app.services import project as project_service
from app.services import workspace
from stage_seed import add_stage

client = TestClient(app)

_ROWS_SCHEMA = [{"name": "name", "type": "str", "nullable": False}]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    pdir = tmp_path / "demo"
    compiled = pdir
    compiled.mkdir(parents=True, exist_ok=True)
    data = pdir / "a.csv"
    pd.DataFrame({"name": ["x"]}).to_csv(data, index=False)
    add_stage(compiled, {
        "id": "load", "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(data), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _ROWS_SCHEMA},
    })
    workspace.set_projects_dir(tmp_path)
    return pdir


def _picker_hrefs(crumbs: list[breadcrumbs.Crumb]) -> list[str]:
    return [crumb.picker for crumb in crumbs if crumb.picker]


def test_every_switcher_rung_points_at_a_route_that_answers(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")
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


def test_the_version_rung_lists_versions_with_their_message(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="Nine flat categories."
    )
    trail = breadcrumbs.build_version_crumbs("demo", meta.version_id)

    body = client.get(_picker_hrefs(trail)[-1], params={"current": meta.version_id}).text

    assert meta.version_id in body
    assert "Nine flat categories." in body


def test_a_version_with_no_message_is_said_to_have_none(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="")
    trail = breadcrumbs.build_version_crumbs("demo", meta.version_id)

    body = client.get(_picker_hrefs(trail)[-1]).text

    assert "No description" in body


def test_only_the_project_and_the_leaf_rung_switch(project: Path) -> None:
    trail = breadcrumbs.build_run_crumbs("demo", "20260805T144252")

    switchers = [crumb.label for crumb in trail if crumb.picker]

    assert switchers == ["demo", "20260805T144252"]


def test_a_run_child_page_does_not_switch_the_run_it_hangs_off(project: Path) -> None:
    trail = breadcrumbs.build_run_child_crumbs("demo", "20260805T144252", label="Review queue")

    assert [crumb.label for crumb in trail if crumb.picker] == ["demo"]
    assert trail[-1].label == "Review queue" and trail[-1].href is None


def test_the_project_rung_reads_as_the_name_and_switches_on_the_id(project: Path) -> None:
    """Two projects may share a name, so the popover marks the current one by id."""
    project_service.Project(id="demo", name="demo", title="Congress roster").save()
    trail = breadcrumbs.build_section_crumbs("demo", label="Runs")
    rung = trail[1]

    assert rung.label == "Congress roster"
    assert rung.picker_current == "demo"

    body = client.get(rung.picker, params={"current": rung.picker_current}).text

    assert "Congress roster" in body
    assert 'aria-current="true"' in body


def test_the_project_picker_omits_a_deleted_project(project: Path) -> None:
    """A record can outlive its workspace dir (delete_project() keeps the store row)."""
    project_service.Project(id="demo", name="demo").save()
    project_service.Project(id="gone", name="gone", title="Deleted project").save()
    trail = breadcrumbs.build_section_crumbs("demo", label="Runs")
    rung = trail[1]

    body = client.get(rung.picker, params={"current": rung.picker_current}).text

    assert "demo" in body
    assert "Deleted project" not in body
