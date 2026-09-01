"""The home page's order: most recently touched first (app.web.loading.list_projects)."""
from __future__ import annotations

import pytest

from app.core.persistence import get_store
from app.core.run_status import RunStatus
from app.models.records.project import Project
from app.services import workspace
from app.services.loader import save_stages
from app.services.methodology import write_methodology
from app.web.loading import list_projects
from project_seed import seed_project
from run_seed import store_manifest

_LONG_AGO = "2020-01-01T00:00:00.000000"


@pytest.fixture(autouse=True)
def examples_root(tmp_path):
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _seed_project(project_id, runs=(), *, edited_at=_LONG_AGO):
    seed_project(project_id)
    write_methodology(project_id, "methodology prose")
    # `seed_project` stamps the record now, which would date every project together.
    _restamp_record(project_id, edited_at)
    for run_id, started_at in runs:
        store_manifest(project_id, run_id,
                       {"status": str(RunStatus.OK), "started_at": started_at})


def _restamp_record(project_id, updated_at):
    raw = {**get_store().read(Project.collection, project_id), "updated_at": updated_at}
    get_store().write(Project.collection, project_id, raw)


def _order():
    return [card.id for card in list_projects()]


def test_the_newest_run_leads(examples_root):
    _seed_project("middle", [("20260102T000000", "2026-01-02T09:00:00")])
    _seed_project("newest", [("20260103T000000", "2026-01-03T09:00:00")])
    _seed_project("oldest", [("20260101T000000", "2026-01-01T09:00:00")])
    assert _order() == ["newest", "middle", "oldest"]


def test_a_run_id_that_is_not_a_stamp_is_dated_by_its_manifest(examples_root):
    """A run id sorting last is not a run that happened last."""
    _seed_project("aborted_simulation",
                  [("SIMULATION_429_20260101T000000", "2026-01-01T09:00:00")])
    _seed_project("worked_recently", [("20260301T000000", "2026-03-01T09:00:00")])
    assert _order() == ["worked_recently", "aborted_simulation"]


def test_an_edit_dates_a_project_that_has_never_run(examples_root):
    _seed_project("old_run", [("20260101T000000", "2026-01-01T09:00:00")])
    _seed_project("recently_edited")
    save_stages("recently_edited", [])
    assert _order() == ["recently_edited", "old_run"]


def test_a_rename_dates_a_project(examples_root):
    _seed_project("renamed", edited_at="2026-05-05T09:00:00.000000")
    _seed_project("ran", [("20260101T000000", "2026-01-01T09:00:00")])
    assert _order() == ["renamed", "ran"]


def test_a_project_no_record_dates_sorts_last(examples_root):
    _seed_project("broken_stamp")
    _restamp_record("broken_stamp", "not a timestamp")
    _seed_project("wrote_a_run", [("20260101T000000", "2026-01-01T09:00:00")])
    assert _order() == ["wrote_a_run", "broken_stamp"]
