"""The home card's four-state headline and its run counts (app.web.project_cards)."""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from app.core.run_status import RunStatus
from app.main import app
from app.services import workspace
from project_seed import seed_project
from app.web.loading import list_projects
from app.web.project_cards import ProjectStatus, read_run_summary
from app.services.methodology import write_methodology
from run_seed import store_manifest

client = TestClient(app)


@pytest.fixture(autouse=True)
def examples_root(tmp_path):
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _write_run(runs_dir, run_id, *, status=None, is_test=False):
    run = runs_dir / run_id
    run.mkdir(parents=True)
    manifest = {}
    if status is not None:
        manifest["status"] = str(status)
    if is_test:
        manifest["parameters"] = {"is_test_run": True}
    store_manifest(run.parent.parent, run.name, manifest)
    return run


def _make_project(root, name, runs=()):
    proj = seed_project(name)
    write_methodology((proj).name, "methodology prose")
    for run_id, kwargs in runs:
        _write_run(proj / "runs", run_id, **kwargs)
    return proj


# ── the mapping ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("run_status", "expected"),
    [
        (RunStatus.RUNNING, ProjectStatus.IN_PROGRESS),
        (RunStatus.CANCELLED, ProjectStatus.IN_PROGRESS),
        (RunStatus.AWAITING_REVIEW, ProjectStatus.AWAITING_REVIEW),
        (RunStatus.OK, ProjectStatus.COMPLETED),
        (RunStatus.WARNINGS, ProjectStatus.COMPLETED),
        (RunStatus.ERRORS, ProjectStatus.ERRORED),
    ],
)
def test_every_run_status_has_a_headline(examples_root, run_status, expected):
    proj = _make_project(examples_root, "p", [("20260101T000000", {"status": run_status})])
    assert read_run_summary(proj.name).headline is expected


def test_the_mapping_covers_every_run_status():
    """A new RunStatus would otherwise land silently on the fallback."""
    from app.web.project_cards import _RUN_STATUS_HEADLINES

    assert set(_RUN_STATUS_HEADLINES) == set(RunStatus)


# ── which run the headline comes from ────────────────────────────────────────

def test_the_headline_is_the_newest_run(examples_root):
    proj = _make_project(examples_root, "p", [
        ("20260101T000000", {"status": RunStatus.ERRORS}),
        ("20260102T000000", {"status": RunStatus.OK}),
    ])
    assert read_run_summary(proj.name).headline is ProjectStatus.COMPLETED


def test_a_test_run_never_sets_the_headline(examples_root):
    proj = _make_project(examples_root, "p", [
        ("20260101T000000", {"status": RunStatus.ERRORS}),
        ("20260102T000000", {"status": RunStatus.OK, "is_test": True}),
    ])
    summary = read_run_summary(proj.name)
    assert summary.headline is ProjectStatus.ERRORED
    assert (summary.real, summary.tests) == (1, 1)


def test_a_project_with_no_runs_is_in_progress(examples_root):
    proj = _make_project(examples_root, "p")
    summary = read_run_summary(proj.name)
    assert summary.headline is ProjectStatus.IN_PROGRESS
    assert (summary.real, summary.tests) == (0, 0)


def test_a_status_this_app_does_not_define_falls_through(examples_root):
    proj = _make_project(examples_root, "p", [
        ("20260101T000000", {"status": RunStatus.ERRORS}),
        ("20260102T000000", {"status": "teleported"}),
    ])
    summary = read_run_summary(proj.name)
    assert summary.headline is ProjectStatus.ERRORED
    assert summary.real == 2


# ── what the page renders ────────────────────────────────────────────────────

def test_a_project_exercised_only_by_tests_does_not_claim_to_be_untouched(examples_root):
    _make_project(examples_root, "probed", [
        ("20260101T000000", {"status": RunStatus.OK, "is_test": True}),
        ("20260102T000000", {"status": RunStatus.OK, "is_test": True}),
    ])
    [card] = list_projects()
    assert (card.n_runs, card.n_test_runs) == (0, 2)
    body = client.get("/").text
    assert "No runs yet" in body
    assert "2 tests" in body


def test_the_headline_word_reaches_the_page(examples_root):
    _make_project(examples_root, "halted",
                  [("20260101T000000", {"status": RunStatus.AWAITING_REVIEW})])
    [card] = list_projects()
    assert card.status_label == "Awaiting review"
    body = client.get("/").text
    assert "Awaiting review" in body
    assert "1 run" in body
