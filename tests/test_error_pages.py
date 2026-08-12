"""A dead link is read by a person, and the app's own fetch() reads `detail`.

The two are told apart by an explicit `text/html` in Accept: a browser navigation sends
it, and nothing else in this app does — the fetch() calls send `*/*` and the MCP client
sends `application/json, text/event-stream`.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import app
from app.services import workspace
from app.web.errors import NoSuchProject, render_error

client = TestClient(app)

_BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
# What tests/test_mcp_server.py sends, and what the fetch() calls send by default.
_MCP = {"accept": "application/json, text/event-stream"}
_FETCH = {"accept": "*/*"}

_GONE = "20260812T121101.427270"


@pytest.fixture(autouse=True)
def empty_workspace(tmp_path):
    workspace.set_projects_dir(tmp_path)
    return tmp_path


def test_a_browser_gets_a_page_naming_what_was_not_found() -> None:
    r = client.get(f"/project/{_GONE}", headers=_BROWSER)

    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    # Jinja escapes the quotes around the id; the words and the id are what matter.
    assert "No project" in r.text and _GONE in r.text
    assert "404 — Not Found" in r.text
    # The trail's home rung is the way back to the project index.
    assert 'href="/"' in r.text


def test_the_page_says_a_deleted_project_is_gone_rather_than_the_link_wrong() -> None:
    r = client.get(f"/project/{_GONE}", headers=_BROWSER)

    assert "This address is intact; the project it names is gone" in r.text


def test_a_404_for_no_route_at_all_is_a_page_too() -> None:
    """The dead link in a deck is usually a path no router claims, not a missing project."""
    r = client.get("/no/such/place", headers=_BROWSER)

    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "There is no page at this address." in r.text
    assert "project it names is gone" not in r.text


def test_the_apps_own_fetch_still_reads_detail() -> None:
    r = client.get(f"/project/{_GONE}", headers=_FETCH)

    assert r.status_code == 404
    assert r.json()["detail"] == f"No project '{_GONE}'"


def test_a_fragment_route_answers_a_fetch_in_json_not_in_a_page() -> None:
    """section_workflow.html reads r.status off this one; a page body would be dropped."""
    r = client.get(f"/project/{_GONE}/node/whatever/panel", headers=_FETCH)

    assert r.status_code == 404
    assert _GONE in r.json()["detail"]


def test_an_mcp_client_is_never_handed_a_page() -> None:
    r = client.get("/no/such/place", headers=_MCP)

    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_a_client_that_states_no_preference_gets_json() -> None:
    r = client.get(f"/project/{_GONE}")

    assert r.json()["detail"] == f"No project '{_GONE}'"


@pytest.mark.anyio
async def test_a_500_raised_as_an_http_exception_renders_the_same_page() -> None:
    response = await render_error(
        _browser_request("/project/x/runs/y"),
        HTTPException(status_code=500, detail="Could not read output file"),
    )

    assert response.status_code == 500
    assert "500 — Internal Server Error" in bytes(response.body).decode()


@pytest.mark.anyio
async def test_an_exception_that_is_not_an_http_one_is_re_raised_untouched() -> None:
    """Swallowing it here would turn a traceback into a tidy page and lose the fault."""
    with pytest.raises(RuntimeError, match="the actual fault"):
        await render_error(_browser_request("/"), RuntimeError("the actual fault"))


def test_the_project_404_carries_its_own_type_everywhere_it_is_raised() -> None:
    """The page's 'it is gone' paragraph is keyed off the type, not off the detail text."""
    assert isinstance(NoSuchProject("x"), HTTPException)
    assert NoSuchProject("x").status_code == 404
    assert NoSuchProject("x").detail == "No project 'x'"


def _browser_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"accept", _BROWSER["accept"].encode())],
            "query_string": b"",
            "app": app,
        }
    )
