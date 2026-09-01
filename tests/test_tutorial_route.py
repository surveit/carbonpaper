"""The tour's door: the home page's redirect into it, and the routes behind it."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from project_seed import seed_project
from app.tools.tutorial import TutorialContext
from app.web.chat_router import _store
from app.services.methodology import write_methodology

client = TestClient(app)

_DRAFT_URL = "/chat/agent/tutorial/new"


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_project(root: Path, name: str = "already-here") -> None:
    seed_project(name)
    write_methodology(name, "methodology prose")


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


def test_the_home_page_offers_no_tour_button() -> None:
    """The button a browser that has not toured would have had to click is gone."""
    page = client.get("/")
    assert page.status_code == 200
    assert "guided tour" not in page.text.lower()
    assert "tour-cta" not in page.text


def test_new_project_opens_the_editing_agent_from_the_header() -> None:
    """A project starts in the chat that can make one, not in a form."""
    page = client.get("/")

    header = page.text.split('class="dash-header"')[1].split("<script>")[0]
    assert 'href="/chat/agent/editing/new"' in header
    assert "＋ New project" in header


def _tour_script(page: str) -> str:
    """The home page's own script, not the shell's — several ship above it."""
    return page.split('const KEY = "carbonpaper.tour.started"')[1].split("</script>")[0]


def test_an_untoured_browser_is_sent_to_the_tour_and_the_visit_is_recorded() -> None:
    """The write comes first: without it the next visit redirects again, forever."""
    script = _tour_script(client.get("/").text)

    assert script.index('localStorage.setItem(KEY, "1")') < script.index(
        f'location.replace("{_DRAFT_URL}")'
    )


def test_a_browser_that_cannot_record_the_visit_stays_on_the_page() -> None:
    """Redirecting a browser whose write failed would strand it: every visit, no way back."""
    script = _tour_script(client.get("/").text)

    assert "catch (e) { sent = true; }" in script
    assert 'if (localStorage.getItem(KEY) !== "1") sent = true;' in script


def test_the_page_claims_nothing_about_what_this_reader_has_done() -> None:
    for claim in ("you have taken", "you've taken", "tour complete", "already toured"):
        assert claim not in client.get("/").text.lower(), claim


def test_the_project_list_is_what_the_page_itself_renders(
    examples_root: Path,
) -> None:
    """Both list shapes render server-side; the redirect is what takes a reader off them."""
    _make_project(examples_root)
    assert "already-here" in client.get("/").text

    workspace.set_projects_dir(examples_root / "empty")
    (examples_root / "empty").mkdir()
    assert "No projects yet" in client.get("/").text


def test_the_redirect_ships_whatever_the_project_list_holds(
    examples_root: Path,
) -> None:
    """Being sent to the tour does not depend on project count — the browser decides."""
    _make_project(examples_root)
    page = client.get("/")
    assert page.status_code == 200
    assert "already-here" in page.text
    assert "No projects yet" not in page.text
    assert f'location.replace("{_DRAFT_URL}")' in page.text


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
