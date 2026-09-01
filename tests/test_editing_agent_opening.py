"""What the editing agent opens with: written words picked by the page it opened on."""
from __future__ import annotations

import pytest

from app.agents.compiler.config import CONFIG as EDITING_CONFIG
from app.agents.compiler.opening import PAGE_OPENINGS, choose_opening_turn
from app.core.agent.session import create_agent_session
from app.core.agent.store import open_session_store
from app.services import project as project_service
from app.services import workspace
from app.tools.editing import EditingContext, build_editing_tools

_store = open_session_store()

_BASE_URL = "https://carbon.example/"

_WAYS_IN = [
    "Start from data I have",
    "Start from a methodology document",
    "Change a project that exists",
]

# What each offer rests on: clicking one sends its words, so the tools must exist.
_OFFERS_REST_ON = {
    "Explain how this value was built": ("read_stage_output_rows", "read_stage"),
    "Did anything upstream error?": ("get_run_status",),
    "Show me the rows around it": ("read_stage_output_rows",),
    "What changed in this version?": ("read_workflow_summary", "read_stage"),
    "What would this establish if I ran it?": ("read_review_guide",),
    "Run it as a test": ("run_workflow_test",),
    "Which version did the last run use?": ("list_runs", "get_run_status"),
    "What changed between them?": ("read_workflow_summary",),
    "How did this run go?": ("get_run_status",),
    "Show me what it published": ("read_stage_output_rows",),
    "Run it again on more rows": ("run_workflow",),
    "Which run should I trust?": ("list_runs", "get_run_status"),
    "Start a new run": ("run_workflow",),
    "What does this workflow establish?": ("read_workflow_summary",),
    "Add or change a stage": ("add_stage", "edit_stages"),
    "Does the workflow match this document?": ("read_workflow_summary",),
    "Change the document": ("read_workflow_summary",),
    "What do these terms control?": ("read_terms",),
    "Add a term": ("write_terms",),
    "What's in this data?": ("profile_file", "list_files"),
    "Use one of these as an input": ("list_files", "add_stage"),
    "Where does this project stand?": ("get_project_status",),
    "What should I do next?": ("get_project_status", "report_compiler_warnings"),
    "Change the workflow": ("edit_stages",),
    "What is this page telling me?": ("get_current_url",),
    "What would you change here?": ("read_workflow_summary",),
    "Run it and show me the rows": ("run_workflow", "read_stage_output_rows"),
    "Start from data I have": ("profile_file", "create_project"),
    "Start from a methodology document": ("create_project",),
    "Change a project that exists": ("list_projects",),
}


def read_every_offer() -> set[str]:
    """Both halves: the per-page tables, and the two fallbacks a page miss lands on."""
    written = {offer for page in PAGE_OPENINGS for offer in page.offers}
    for name in (None, "a_project"):
        written |= set(choose_opening_turn("/nowhere", name).offers)
    return written


# ── the offers are the message ───────────────────────────────────────────────


@pytest.mark.parametrize("offer", sorted(read_every_offer()))
def test_every_offer_rests_on_tools_this_agent_binds(offer: str) -> None:
    assert offer in _OFFERS_REST_ON, f"{offer!r} is offered, but nothing says what keeps it"

    assert set(_OFFERS_REST_ON[offer]) <= _bound_tool_names()


def test_this_agent_binds_nothing_that_publishes() -> None:
    # Publishing is the human's approval; every offer is checked against this set above.
    assert not [name for name in _bound_tool_names() if "publish" in name]


# ── which words a page gets ──────────────────────────────────────────────────


def test_a_chat_opened_on_no_page_at_all_gets_the_ways_in() -> None:
    turn = choose_opening_turn(None, None)

    assert turn.offers == _WAYS_IN


def test_a_chat_opened_on_a_run_says_so_and_names_the_project(tmp_path) -> None:
    project_id = _make_project(tmp_path)

    message = _opening_message(
        {"project_id": project_id, "opened_on": f"/project/{project_id}/runs/r1"})

    # The name every other surface shows, not the opaque id the URL carries.
    assert "lobbying_spend" in message
    assert "on one run" in message
    assert project_id not in message


def test_the_lineage_page_gets_words_no_other_page_gets(tmp_path) -> None:
    project_id = _make_project(tmp_path)
    on_run = {"project_id": project_id, "opened_on": f"/project/{project_id}/runs/r1"}

    message = _opening_message(
        on_run | {"opened_on": f"{on_run['opened_on']}/stage/parse/row/0/trace/view"})

    assert "lineage" in message
    assert message != _opening_message(on_run)


def test_a_page_in_no_project_never_leaves_the_name_unfilled() -> None:
    turn = choose_opening_turn("/project/p1/runs/r1", None)

    assert "{name}" not in turn.text


def test_a_task_link_stays_silent_so_it_does_not_talk_over_the_task(tmp_path) -> None:
    project_id = _make_project(tmp_path)

    sid = create_agent_session(
        "editing",
        {"project_id": project_id, "task": "Write the document",
         "opened_on": f"/project/{project_id}"},
        base_url=_BASE_URL, title="t")

    assert _store.load(sid)["messages"] == []


def test_the_hook_the_editing_agent_registered_is_the_one_that_runs() -> None:
    # Without this, every test above would pass on an agent carrying no hook.
    assert EDITING_CONFIG.render_opening_turn is not None


def _opening_message(context: dict) -> str:
    sid = create_agent_session("editing", context, base_url=_BASE_URL, title="t")
    messages = _store.load(sid)["messages"]
    assert len(messages) == 1, messages
    return messages[0]["parts"][0]["text"]


def _make_project(tmp_path) -> str:
    workspace.set_projects_dir(tmp_path)
    return project_service.create_project("Lobbying spend", "doc text", source="test").id


def _bound_tool_names() -> set[str]:
    return {spec.name for spec in build_editing_tools(
        EditingContext(project_id="anything", base_url=_BASE_URL))}
