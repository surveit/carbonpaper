"""The tour's door: the home zero state's only CTA, and the draft/materialize routes
behind it.

Offline throughout — visiting the draft page runs no agent turn and creates nothing;
materializing writes a session but still runs no turn.
"""
from __future__ import annotations

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
_DRAFT_URL = "/chat/agent/tutorial/new"


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_project(root: Path, name: str = "already-here") -> None:
    proj = root / name
    proj.mkdir()
    write_methodology((proj).name, "methodology prose")


def _materialize_the_tour() -> str:
    """What the draft page's first reply does — see ensureSession() in chat.html."""
    client.get(_DRAFT_URL)  # visiting first, as a reader would
    r = client.post(
        "/chat/agent/tutorial/sessions",
        json={"context": {"base_url": "http://testserver/"}, "title": "Guided tour"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"], data
    return data["sid"]


def test_the_tour_block_offers_only_the_tour() -> None:
    """The tour markup is its own block: no second door inside it."""
    page = client.get("/")
    assert page.status_code == 200
    assert "New here? Take the guided tour" in page.text
    assert _CTA in page.text
    assert f'href="{_DRAFT_URL}"' in page.text
    assert 'class="btn primary" id="tour-cta"' in page.text

    tour_state = page.text.split('id="tour-state"')[1].split("</div>")[0]
    assert "New project" not in tour_state, "the tour block grew a second door"


def test_new_project_stays_reachable_from_the_header() -> None:
    """The path to a blank project lives in the header regardless of tour visibility."""
    page = client.get("/")

    header = page.text.split('class="dash-header"')[1].split('id="tour-state"')[0]
    assert 'href="/project/new"' in header
    assert "＋ New project" in header


def test_the_tour_visibility_is_decided_client_side_by_this_browser() -> None:
    """No server-side tour state exists: the page ships both states and lets JS choose."""
    page = client.get("/")

    assert 'id="tour-state"' in page.text
    assert 'id="projects-state"' in page.text
    assert 'localStorage.setItem(KEY, "1")' in page.text
    assert '"carbonpaper.tour.started"' in page.text
    assert 'document.documentElement.classList.add("tour-unstarted")' in page.text
    for claim in ("you have taken", "you've taken", "tour complete", "already toured"):
        assert claim not in page.text.lower(), claim


def test_the_project_list_ships_inside_the_block_the_tour_replaces(
    examples_root: Path,
) -> None:
    """Both list shapes sit inside #projects-state, which is what .tour-unstarted hides."""
    _make_project(examples_root)
    with_projects = client.get("/").text
    assert "already-here" in with_projects.split('id="projects-state"')[1]

    workspace.set_projects_dir(examples_root / "empty")
    (examples_root / "empty").mkdir()
    assert "No projects yet" in client.get("/").text.split('id="projects-state"')[1]


def test_a_home_page_with_projects_still_ships_the_tour_markup(
    examples_root: Path,
) -> None:
    """Tour visibility no longer depends on project count — the browser decides."""
    _make_project(examples_root)
    page = client.get("/")
    assert page.status_code == 200
    assert "already-here" in page.text
    assert "No projects yet" not in page.text
    assert _CTA in page.text
    assert 'id="tour-state"' in page.text


def test_visiting_the_draft_page_creates_nothing() -> None:
    before = len(_store.list_sessions())

    assert client.get(_DRAFT_URL).status_code == 200
    assert client.get(_DRAFT_URL).status_code == 200  # a reload, same result

    assert len(_store.list_sessions()) == before


def test_the_draft_page_renders_the_greeting_with_no_session_behind_it() -> None:
    """The reader lands on a greeting, not on an empty box waiting to be typed in."""
    page = client.get(_DRAFT_URL)

    assert "Welcome to Carbon Paper" in page.text
    assert page.text.count("Welcome to Carbon Paper") == 1


def test_materializing_opens_a_session_bound_to_the_tutorial_agent() -> None:
    sid = _materialize_the_tour()
    assert _store.load(sid)["agent_id"] == "tutorial"


def test_the_materialized_session_carries_a_base_url_the_tutorial_context_accepts() -> None:
    sid = _materialize_the_tour()
    context = _store.load(sid)["context"]

    assert context["base_url"].endswith("/")
    assert TutorialContext.model_validate(context).base_url == context["base_url"]


def test_two_visitors_get_their_own_sessions() -> None:
    assert _materialize_the_tour() != _materialize_the_tour()


def test_the_materialized_session_already_carries_the_greeting_with_no_model_call() -> None:
    """Fixed text written at materialization, not a live turn: no turn_id exists yet."""
    from app.agents.tutorial.prompt import (
        TUTORIAL_OPENING_MESSAGE,
        TUTORIAL_OPENING_OFFERS,
    )

    sid = _materialize_the_tour()
    data = _store.load(sid)

    assert data["active_turn"] is None
    assert data["messages"] == [
        {"role": "assistant", "parts": [
            {"type": "text", "text": TUTORIAL_OPENING_MESSAGE},
            {"type": "offer", "options": TUTORIAL_OPENING_OFFERS},
        ]}
    ]


def test_the_open_route_no_longer_exists() -> None:
    """The greeting used to be a live turn started by this route; it is gone with it."""
    sid = _materialize_the_tour()

    assert client.post(f"/chat/{sid}/open").status_code == 404
