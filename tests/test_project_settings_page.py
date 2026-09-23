"""The settings page: the only place code execution is turned on, and by a person."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import code_approval, workspace

PROJECT = "settings-demo"


@pytest.fixture(autouse=True)
def project(tmp_path) -> Any:
    (tmp_path / PROJECT).mkdir(parents=True, exist_ok=True)
    workspace.set_projects_dir(tmp_path)
    return tmp_path


@pytest.fixture
def client() -> Any:
    with TestClient(app, follow_redirects=False) as running:
        yield running


def read_the_page(client: TestClient) -> Any:
    return client.get(f"/project/{PROJECT}/settings")


# ── what the page says in each state ──────────────────────────────────────────
def test_the_page_says_code_execution_is_off(client):
    page = read_the_page(client)

    assert page.status_code == 200
    assert '<span class="badge pending">off</span>' in page.text
    assert "Turn code execution on" in page.text


def test_the_page_names_what_turning_it_on_unlocks(client):
    """A reader deciding needs the labels, not three slugs they have no word for."""
    assert "dangerously run code on the table" in read_the_page(client).text


def test_the_page_carries_the_warning_the_owner_answers(client):
    assert "not built for arbitrary code execution" in read_the_page(client).text


def test_the_page_shows_when_it_was_turned_on_and_why(client):
    code_approval.approve_code_execution(PROJECT, "diffing two roster snapshots")

    page = read_the_page(client)

    assert '<span class="badge warn">on</span>' in page.text
    assert "diffing two roster snapshots" in page.text
    assert "Turn code execution off" in page.text


def test_the_nav_leads_to_the_page(client):
    assert f'href="/project/{PROJECT}/settings"' in read_the_page(client).text


# ── turning it on and off ─────────────────────────────────────────────────────
def test_posting_a_reason_turns_it_on_and_returns_to_the_page(client):
    posted = client.post(
        f"/project/{PROJECT}/code-execution/approve", data={"reason": "a stated reason"})

    assert posted.status_code == 303
    assert posted.headers["location"] == f"/project/{PROJECT}/settings"
    standing = code_approval.read_code_execution_approval(PROJECT)
    assert standing is not None and standing.reason == "a stated reason"


def test_approving_without_a_reason_is_refused(client):
    """The record is what whoever revokes later reads, so a blank one is no record."""
    posted = client.post(f"/project/{PROJECT}/code-execution/approve", data={"reason": "  "})

    assert posted.status_code == 400
    assert code_approval.has_code_execution_approval(PROJECT) is False


def test_withdrawing_turns_it_off_and_returns_to_the_page(client):
    code_approval.approve_code_execution(PROJECT, "a stated reason")

    posted = client.post(f"/project/{PROJECT}/code-execution/withdraw")

    assert posted.status_code == 303
    assert posted.headers["location"] == f"/project/{PROJECT}/settings"
    assert code_approval.has_code_execution_approval(PROJECT) is False


def test_a_project_that_does_not_exist_is_a_404(client):
    assert client.get("/project/no-such-project/settings").status_code == 404
    assert client.post(
        "/project/no-such-project/code-execution/approve", data={"reason": "x"}
    ).status_code == 404
