"""Naming a run: where the name is written, and what a rename leaves alone."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services import workspace
from app.services.project import save_working_copy_as_version
from app.services.methodology import write_methodology
from app.services.run_manifest_metadata import read_run_metadata, read_run_name
from app.web.run_index import build_run_index_rows
from run_seed import read_manifest, store_manifest
from stage_seed import add_stage

client = TestClient(app)
GOLDENS = Path(__file__).parent / "goldens"

RUN_ID = "20260101T000000"
# A name really typed on this project, read off its stored run records.
A_REAL_NAME = "SMOKE: consolidated 4-file input, relevance capped at 25"


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    pdir = tmp_path / "demo"
    (pdir / "runs").mkdir(parents=True)
    write_methodology(pdir.name, "methodology")
    payload = json.loads((GOLDENS / "ok_run.json").read_text(encoding="utf-8"))
    payload["run_id"] = RUN_ID
    payload["project"] = pdir.name
    store_manifest(pdir, RUN_ID, payload)
    return pdir


def name_it(project: str, name: str, run_id: str = RUN_ID):
    return client.post(f"/project/{project}/runs/{run_id}/name",
                       data={"name": name}, follow_redirects=False)


def test_a_named_run_leads_its_page_with_the_name(project_dir: Path) -> None:
    assert name_it(project_dir.name, A_REAL_NAME).status_code == 303
    page = client.get(f"/project/{project_dir.name}/runs/{RUN_ID}").text
    assert A_REAL_NAME in page
    assert read_run_name(project_dir.name, RUN_ID) == A_REAL_NAME


def test_the_name_is_written_beside_the_manifest_not_into_it(project_dir: Path) -> None:
    """The executor rewrites the manifest after every stage; a name in there is racing it."""
    name_it(project_dir.name, A_REAL_NAME)
    assert "name" not in read_manifest(project_dir, RUN_ID)


def test_saving_it_blank_clears_the_name(project_dir: Path) -> None:
    name_it(project_dir.name, A_REAL_NAME)
    name_it(project_dir.name, "  ")
    assert read_run_name(project_dir.name, RUN_ID) == ""


def test_renaming_does_not_unarchive_and_archiving_does_not_unname(project_dir: Path) -> None:
    """Both write the one RunManifestMetadata record, so each must edit it in place."""
    name_it(project_dir.name, A_REAL_NAME)
    client.post(f"/project/{project_dir.name}/runs/{RUN_ID}/archive", follow_redirects=False)
    record, = read_run_metadata(project_dir.name)
    assert (record.name, record.archived) == (A_REAL_NAME, True)

    name_it(project_dir.name, "renamed while archived")
    record, = read_run_metadata(project_dir.name)
    assert (record.name, record.archived) == ("renamed while archived", True)


def test_naming_an_unrecorded_run_is_a_404(project_dir: Path) -> None:
    assert name_it(project_dir.name, A_REAL_NAME, run_id="20990101T000000").status_code == 404


def test_the_runs_list_reads_the_name_back(project_dir: Path) -> None:
    name_it(project_dir.name, A_REAL_NAME)
    row, = build_run_index_rows(project_dir.name)
    assert row.name == A_REAL_NAME


_ROWS_SCHEMA = [{"name": "name", "type": "str", "nullable": False},
                {"name": "val", "type": "int", "nullable": False}]


@pytest.fixture
def runnable_project(tmp_path: Path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    add_stage(proj, {"id": "load", "description": "Load", "type": "input_data",
                     "signature": {"form": "replaces", "produces": _ROWS_SCHEMA},
                     "connector": {"kind": "file",
                                   "params": {"path": str(data), "format": "csv"}}})
    save_working_copy_as_version(proj.name, message="v1")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def test_the_launch_form_offers_a_name_field(runnable_project: Path) -> None:
    page = client.get(f"/project/{runnable_project.name}/runs/new").text
    assert 'name="name"' in page, "the form can name the run it is about to start"


def test_a_name_given_at_launch_is_on_the_run_it_started(runnable_project: Path) -> None:
    client.post(f"/project/{runnable_project.name}/run",
                data={"name": A_REAL_NAME}, follow_redirects=False)
    row, = build_run_index_rows(runnable_project.name)
    assert row.name == A_REAL_NAME
