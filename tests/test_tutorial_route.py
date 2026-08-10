"""The tour's door: the home zero state's secondary CTA and the route behind it.

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


def test_the_zero_state_offers_the_tour_as_the_secondary_action() -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "No projects yet" in page.text
    assert _CTA in page.text
    assert 'action="/tutorial"' in page.text
    assert 'class="btn secondary"' in page.text
    # Second door, not the first: the primary is still New project.
    assert 'class="btn primary new-methodology-btn"' in page.text


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
