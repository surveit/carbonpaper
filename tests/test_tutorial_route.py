"""The tour's door: the home zero state's secondary CTA and the route behind it.

Offline throughout — the route opens a session and redirects; no agent turn runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.authoring_lifecycle_note import (
    AUTHORING_LIFECYCLE_GUIDANCE,
    LIFECYCLE_ONE_LINE,
)
from app.services import workspace
from app.tools.tutorial import TutorialContext
from app.web.chat_router import _store

client = TestClient(app)

_CTA = "Take a guided tour on sample data"


@pytest.fixture(autouse=True)
def examples_root(tmp_path: Path) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def _make_project(root: Path, name: str = "already-here") -> None:
    proj = root / name
    proj.mkdir()
    (proj / "document.md").write_text("methodology prose", encoding="utf-8")
    (proj / "project.json").write_text(
        json.dumps({"name": name, "model": "sonnet"}), encoding="utf-8"
    )


def _start_the_tour() -> str:
    r = client.post("/tutorial", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/chat/")
    return location.rsplit("/", 1)[-1]


def test_the_zero_state_offers_the_tour_as_a_primary_action() -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "No projects yet" in page.text
    assert _CTA in page.text
    assert 'action="/tutorial"' in page.text
    # Both doors are primary (#476), and the tour's is not styled as a lesser one.
    assert 'class="btn primary new-methodology-btn"' in page.text
    assert 'class="btn primary"' in page.text
    assert "btn secondary" not in page.text


def test_the_zero_state_sketches_the_lifecycle_in_the_authoring_prompts_words() -> None:
    page = client.get("/")
    # One string, so what the reader is promised is what the prompts enforce.
    assert LIFECYCLE_ONE_LINE in page.text
    assert LIFECYCLE_ONE_LINE in AUTHORING_LIFECYCLE_GUIDANCE


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


def test_the_tour_page_opens_the_conversation_itself() -> None:
    """The reader lands on a greeting, not on an empty box waiting to be typed in."""
    page = client.get(f"/chat/{_start_the_tour()}")

    assert "const OPENS_ITSELF = true" in page.text


def test_a_session_that_has_already_opened_will_not_greet_twice() -> None:
    """A reload must replay the transcript, not start a second opening turn."""
    sid = _start_the_tour()
    _store.save_messages(sid, [{"role": "assistant", "parts": [{"type": "text",
                                                                "text": "Hello."}]}])

    assert "const OPENS_ITSELF = false" in client.get(f"/chat/{sid}").text
    assert client.post(f"/chat/{sid}/open").status_code == 409


def test_the_opening_turn_is_not_attributed_to_the_reader() -> None:
    """The prompt that makes the agent speak is the app's, so it is stored as nobody's."""
    from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT
    from app.core.agent.turns import _drop_user_messages

    engine_transcript = [
        {"role": "user", "parts": [{"type": "text", "text": TUTORIAL_OPENING_PROMPT}]},
        {"role": "assistant", "parts": [{"type": "text", "text": "Hello."}]},
    ]

    assert _drop_user_messages(engine_transcript) == engine_transcript[1:]
