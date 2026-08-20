"""Archiving a run: which of the two lists it appears on, and what archiving leaves
alone. The claim the feature rests on is that a run's own record does not move."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.services.methodology import write_methodology
from app.services.run_manifest_metadata import read_run_metadata
from app.web.run_index import RUN_VIEW_ARCHIVED, build_run_index_rows, count_archived_runs
from run_seed import read_manifest, store_manifest, store_manifest_text

client = TestClient(app)
GOLDENS = Path(__file__).parent / "goldens"

RUN_ID = "20260101T000000"
OTHER_RUN_ID = "20260102T000000"


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "demo"
    (pdir / "runs").mkdir(parents=True)
    write_methodology(pdir.name, "methodology")
    return pdir


def seed_run(project_dir: Path, run_id: str = RUN_ID) -> None:
    payload = json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))
    payload["run_id"] = run_id
    payload["project"] = project_dir.name
    store_manifest(project_dir, run_id, payload)


def archive(project_dir: Path, run_id: str = RUN_ID):
    return client.post(f"/project/{project_dir.name}/runs/{run_id}/archive",
                       follow_redirects=False)


def restore(project_dir: Path, run_id: str = RUN_ID):
    return client.post(f"/project/{project_dir.name}/runs/{run_id}/unarchive",
                       follow_redirects=False)


def run_ids(project_dir: Path, *, view: str | None = None) -> list[str]:
    return [row.run_id for row in build_run_index_rows(project_dir.name, view=view)]


def test_an_archived_run_leaves_the_runs_list(project_dir: Path):
    seed_run(project_dir)
    seed_run(project_dir, OTHER_RUN_ID)

    archive(project_dir)

    assert run_ids(project_dir) == [OTHER_RUN_ID]


def test_an_archived_run_is_on_the_archived_list(project_dir: Path):
    seed_run(project_dir)

    archive(project_dir)

    assert run_ids(project_dir, view=RUN_VIEW_ARCHIVED) == [RUN_ID]


def test_restoring_puts_the_run_back_on_the_runs_list(project_dir: Path):
    seed_run(project_dir)
    archive(project_dir)

    restore(project_dir)

    assert (run_ids(project_dir), run_ids(project_dir, view=RUN_VIEW_ARCHIVED)) == ([RUN_ID], [])


def test_archiving_a_run_twice_archives_it_once(project_dir: Path):
    seed_run(project_dir)

    archive(project_dir)
    archive(project_dir)

    assert count_archived_runs(project_dir.name) == 1
    assert run_ids(project_dir, view=RUN_VIEW_ARCHIVED) == [RUN_ID]


def test_a_restored_run_keeps_the_one_record_that_archived_it(project_dir: Path):
    # Restoring edits the record; it must not delete what else it holds.
    seed_run(project_dir)

    archive(project_dir)
    restore(project_dir)

    record, = read_run_metadata(project_dir.name)
    assert (record.run_id, record.archived) == (RUN_ID, False)


def test_archiving_leaves_the_run_record_untouched(project_dir: Path):
    seed_run(project_dir)
    before = read_manifest(project_dir, RUN_ID)

    archive(project_dir)

    assert read_manifest(project_dir, RUN_ID) == before


def test_a_run_whose_record_will_not_parse_can_still_be_archived(project_dir: Path):
    # The row an archiver most wants gone, so it must not go through the parser.
    store_manifest_text(project_dir, RUN_ID, "{ not json")

    assert archive(project_dir).status_code == 303
    assert run_ids(project_dir) == []
    assert run_ids(project_dir, view=RUN_VIEW_ARCHIVED) == [RUN_ID]


def test_archiving_a_run_the_project_never_recorded_is_a_404(project_dir: Path):
    assert archive(project_dir, "20261231T235959").status_code == 404


def test_one_project_archiving_a_run_does_not_hide_anothers(project_dir: Path, tmp_path: Path):
    seed_run(project_dir)
    other = tmp_path / "second"
    (other / "runs").mkdir(parents=True)
    write_methodology(other.name, "methodology")
    seed_run(other)

    archive(project_dir)

    assert run_ids(other) == [RUN_ID]


def test_the_view_picker_counts_the_archived_run(project_dir: Path):
    seed_run(project_dir)
    assert "Archived (0)" in client.get(f"/project/{project_dir.name}/runs").text

    archive(project_dir)

    assert "Archived (1)" in client.get(f"/project/{project_dir.name}/runs").text


def test_the_archived_view_offers_restore_in_place_of_archive(project_dir: Path):
    seed_run(project_dir)
    archive(project_dir)

    page = client.get(f"/project/{project_dir.name}/runs?view=archived").text

    assert f"/runs/{RUN_ID}/unarchive" in page
    assert f"/runs/{RUN_ID}/archive" not in page


def test_the_runs_picker_omits_an_archived_run(project_dir: Path):
    seed_run(project_dir)
    seed_run(project_dir, OTHER_RUN_ID)

    archive(project_dir)

    picker = client.get(f"/pickers/project/{project_dir.name}/runs").text
    assert RUN_ID not in picker
    assert OTHER_RUN_ID in picker
