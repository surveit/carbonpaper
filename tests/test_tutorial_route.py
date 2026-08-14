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


def test_the_tour_block_offers_only_the_tour() -> None:
    """The tour markup is its own block: no second door inside it."""
    page = client.get("/")
    assert page.status_code == 200
    assert "New here? Take the guided tour" in page.text
    assert _CTA in page.text
    assert 'action="/tutorial"' in page.text
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

    assert 'id="tour-state" style="display:none;"' in page.text
    assert 'localStorage.setItem(KEY, "1")' in page.text
    assert '"carbonpaper.tour.started"' in page.text
    assert 'document.getElementById("tour-state").style.display = ""' in page.text
    for claim in ("you have taken", "you've taken", "tour complete", "already toured"):
        assert claim not in page.text.lower(), claim


def test_a_home_page_with_projects_still_ships_the_tour_markup_hidden(
    examples_root: Path,
) -> None:
    """Tour visibility no longer depends on project count — the browser decides."""
    _make_project(examples_root)
    page = client.get("/")
    assert page.status_code == 200
    assert "already-here" in page.text
    assert "No projects yet" not in page.text
    assert _CTA in page.text
    assert 'id="tour-state" style="display:none;"' in page.text


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


def test_the_tour_page_opens_the_conversation_itself() -> None:
    """The reader lands on a greeting, not on an empty box waiting to be typed in."""
    page = client.get(f"/chat/{_start_the_tour()}")

    assert "const OPENS_ITSELF = true" in page.text


def test_a_session_that_has_already_opened_will_not_greet_twice() -> None:
    """A reload must replay the transcript, not start a second opening turn."""
    sid = _start_the_tour()
    _store.append_messages(sid, [{"role": "assistant", "parts": [{"type": "text",
                                                                "text": "Hello."}]}])

    assert "const OPENS_ITSELF = false" in client.get(f"/chat/{sid}").text
    assert client.post(f"/chat/{sid}/open").status_code == 409


def test_the_opening_turn_is_not_attributed_to_the_reader() -> None:
    """The prompt that makes the agent speak is the app's, so it is stored as nobody's."""
    from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT
    from app.core.agent.store import TranscriptMessage
    from app.core.agent.turns import _drop_user_messages

    engine_transcript: list[TranscriptMessage] = [
        {"role": "user", "parts": [{"type": "text", "text": TUTORIAL_OPENING_PROMPT}]},
        {"role": "assistant", "parts": [{"type": "text", "text": "Hello."}]},
    ]

    assert _drop_user_messages(engine_transcript) == engine_transcript[1:]
