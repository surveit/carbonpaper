"""The tour's door: the home zero state's only CTA and the route behind it.

Offline throughout — the route opens a session and redirects; no agent turn runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.tools.tutorial import TutorialContext
from app.web.chat_router import _store
from app.services.methodology import write_methodology

client = TestClient(app)

_CTA = "Take a guided tour on sample data"


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_project(root: Path, name: str = "already-here") -> None:
    proj = root / name
    proj.mkdir()
    write_methodology((proj).name, "methodology prose")
    (proj / "project.json").write_text(
        json.dumps({"name": name, "model": "sonnet"}), encoding="utf-8"
    )


def _start_the_tour() -> str:
    r = client.post("/tutorial", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/chat/")
    return location.rsplit("/", 1)[-1]


def test_the_zero_state_offers_the_tour_and_nothing_else() -> None:
    """One door. A reader with no projects has nothing to judge a blank form against."""
    page = client.get("/")
    assert page.status_code == 200
    assert "New here? Take the guided tour" in page.text
    assert _CTA in page.text
    assert 'action="/tutorial"' in page.text
    assert 'class="btn primary" id="tour-cta"' in page.text

    zero_state = page.text.split('id="zero-state"')[1].split("</div>")[0]
    assert "New project" not in zero_state, "the zero state grew a second door again"


def test_new_project_stays_reachable_from_the_header() -> None:
    """Removing it from the zero state must not strand the path to a blank project."""
    page = client.get("/")

    header = page.text.split('class="dash-header"')[1].split('id="zero-state"')[0]
    assert 'href="/project/new"' in header
    assert "＋ New project" in header


def test_the_zero_state_records_only_that_this_browser_started_the_tour() -> None:
    """No server-side tour state exists, so the page must not imply the person toured."""
    page = client.get("/")

    assert 'localStorage.setItem(KEY, "1")' in page.text
    assert '"carbonpaper.tour.started"' in page.text
    # A returning browser gets a quieter button — never a claim about what was finished.
    assert "Take the guided tour again" in page.text
    for claim in ("you have taken", "you've taken", "tour complete", "already toured"):
        assert claim not in page.text.lower(), claim


def test_a_home_page_with_projects_does_not_offer_the_tour(examples_root: Path) -> None:
    _make_project(examples_root)
    page = client.get("/")
    assert page.status_code == 200
    assert "already-here" in page.text
    assert "No projects yet" not in page.text
    assert _CTA not in page.text
    assert "/tutorial" not in page.text


def test_the_route_opens_a_session_bound_to_the_tutorial_agent() -> None:
    sid = _start_the_tour()
    assert _store.load(sid)["agent_id"] == "tutorial"


def test_the_session_carries_a_base_url_the_tutorial_context_accepts() -> None:
    sid = _start_the_tour()
    context = _store.load(sid)["context"]

    assert context["base_url"].endswith("/")
    assert TutorialContext.model_validate(context).base_url == context["base_url"]


def test_two_visitors_get_their_own_sessions() -> None:
    assert _start_the_tour() != _start_the_tour()
