"""Where the reader is, reaching the agent beside them."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.compiler.config import CONFIG as EDITING_CONFIG
from app.core.agent.registry import render_system_prompt
from app.main import app
from app.services import project as project_service
from app.services import workspace
from app.tools.editing import EditingContext, build_editing_tools
from app.web.chat_router import read_chat_context

_STATIC = Path(__file__).resolve().parents[1] / "app/static"

client = TestClient(app)

_BASE_URL = "https://carbon.example/"


def _make_project(tmp_path) -> str:
    workspace.set_projects_dir(tmp_path)
    return project_service.create_project("Lobbying spend", "doc text", source="test").id


def _draft_panel(query: str) -> str:
    response = client.get(f"/chat/agent/editing/new/panel?{query}")
    assert response.status_code == 200, response.text
    return response.text


# ── what the page binds ──────────────────────────────────────────────────────


def test_a_chat_opened_on_a_project_page_opens_inside_that_project(tmp_path) -> None:
    project_id = _make_project(tmp_path)

    panel = _draft_panel(f"opened_on=/project/{project_id}/runs/r1/lineage")

    # The project-bound opening, not the three-ways-in a chat that knows nothing gets.
    assert "lobbying_spend" in panel
    assert "Three ways to start" not in panel


@pytest.mark.parametrize("opened_on", [
    "/project/no_such_project/workflow",
    "/files",
    "/",
    "",
])
def test_a_page_naming_no_project_binds_none(opened_on: str) -> None:
    context = read_chat_context(_request(f"opened_on={opened_on}"))

    # Never a guess: an unbound chat asks, and asking beats editing the wrong project.
    assert "project_id" not in context


def test_a_link_that_names_the_project_outranks_the_page_it_was_clicked_on(
    tmp_path,
) -> None:
    named = _make_project(tmp_path)
    other = project_service.create_project("Other", "doc", source="test").id

    context = read_chat_context(
        _request(f"project_id={named}&opened_on=/project/{other}/workflow"))

    assert context["project_id"] == named


# ── what each turn carries ───────────────────────────────────────────────────


def test_the_tool_answers_with_the_page_the_reader_is_on_now() -> None:
    assert _ask_current_url(page="/project/p1/runs/r1/lineage") == (
        "https://carbon.example/project/p1/runs/r1/lineage")


def test_a_turn_reporting_no_page_answers_nothing_rather_than_a_guess() -> None:
    assert _ask_current_url(page=None) is None


def test_nothing_the_reader_did_not_type_reaches_the_conversation(tmp_path) -> None:
    """The page arrives as a tool result, so no turn holds words the reader never wrote."""
    project_id = _make_project(tmp_path)
    context = EditingContext(
        project_id=project_id, base_url=_BASE_URL, page="/project/p1/runs/r1/lineage")

    prompt = render_system_prompt(EDITING_CONFIG, context)

    # Not in the system prompt either: it is the cached prefix, and the page moves.
    assert "/runs/r1/lineage" not in prompt
    assert prompt == render_system_prompt(
        EDITING_CONFIG, context.model_copy(update={"page": "/files"}))


def test_the_prompt_names_the_tool_that_reads_it(tmp_path) -> None:
    # A tool the model is never told to reach for is one it answers "this" without.
    context = EditingContext(base_url=_BASE_URL)

    assert "get_current_url" in render_system_prompt(EDITING_CONFIG, context)


def test_the_binding_is_prompt_prose_because_it_never_moves(tmp_path) -> None:
    """A session's project is settled when it opens, so a tool would re-answer one fact."""
    project_id = _make_project(tmp_path)

    prompt = render_system_prompt(
        EDITING_CONFIG, EditingContext(project_id=project_id, base_url=_BASE_URL))

    assert project_id in prompt
    assert "get_current_project" not in prompt


def test_a_chat_bound_to_nothing_is_told_the_two_ways_on(tmp_path) -> None:
    prompt = render_system_prompt(EDITING_CONFIG, EditingContext(base_url=_BASE_URL))

    # Never a guess: the reader picks an existing project, or names a new one.
    assert "opened in no project" in prompt
    assert "list_projects" in prompt and "create_project" in prompt


# ── what the client sends ────────────────────────────────────────────────────


def test_the_reader_is_never_told_they_are_looking_at_the_conversation() -> None:
    # The regex deciding this lives in <head>; reading its mark keeps this from copying it.
    source = (_STATIC / "chat-panel.js").read_text(encoding="utf-8")

    assert "chat-is-the-page" in source


def test_both_hosts_report_the_page_through_the_one_reader() -> None:
    panel = (_STATIC / "chat-panel.js").read_text(encoding="utf-8")
    rail = (_STATIC / "chat-rail.js").read_text(encoding="utf-8")

    assert "window.ChatPanel.here = function" in panel
    assert "page: window.ChatPanel.here()" in panel
    assert "window.ChatPanel.here()" in rail


def _ask_current_url(page: str | None) -> str | None:
    specs = build_editing_tools(EditingContext(base_url=_BASE_URL, page=page))
    spec = next(s for s in specs if s.name == "get_current_url")
    return spec.fn()


def _request(query: str):
    """A Request is what the route hands the reader; building one keeps the test on it."""
    from starlette.requests import Request

    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": "/chat/agent/editing/new/panel", "raw_path": b"",
        "query_string": query.encode(), "root_path": "",
        "headers": [(b"host", b"carbon.example")], "server": ("carbon.example", 443),
        "client": ("test", 1),
    })
