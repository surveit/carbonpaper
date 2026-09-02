"""What the editing agent opens with: written words picked by the page it opened on."""
from __future__ import annotations

import pytest

from app.agents.compiler.config import CONFIG as EDITING_CONFIG
from app.agents.compiler.opening import PAGE_OPENINGS, choose_opening_turn
from app.core.agent.registry import render_system_prompt
from app.core.agent.session import create_agent_session
from app.core.agent.store import open_session_store
from app.services import project as project_service
from app.services import workspace
from app.tools.editing import EditingContext, build_editing_tools
from app.tools.prompt_fragments import ROW_LINEAGE_PAGE_NOTE

_store = open_session_store()

_BASE_URL = "https://carbon.example/"

_WAYS_IN = [
    "Start from data I have",
    "Start from a methodology document",
    "Change a project that exists",
]

# What each offer rests on. An empty tuple means the prompt answers it, not a tool.
_OFFERS_REST_ON = {
    "Explain how this value was built": ("read_stage_output_rows", "read_stage"),
    "Explain how to use this page": (),
    "Explain what changed in this version": ("read_workflow_summary", "read_stage"),
    "Run this version as a test": ("run_workflow_test",),
    "Compare the saved versions": ("read_workflow_summary",),
    "Which version did the last run use?": ("list_runs", "get_run_status"),
    "Explain what happened": ("get_run_status",),
    "Review the run for potential errors": ("get_run_status", "report_compiler_warnings"),
    "Rerun with different inputs": ("run_workflow",),
    "Compare the recent runs": ("list_runs", "get_run_status"),
    "Start a new run": ("run_workflow",),
    "Explain what this workflow does": ("read_workflow_summary",),
    "Edit the workflow": ("edit_stages", "add_stage"),
    "Run this workflow": ("run_workflow",),
    "What do these terms control?": ("read_terms",),
    "Add a term": ("write_terms",),
    "What's in this data?": ("profile_file", "list_files"),
    "Use one of these as an input": ("list_files", "add_stage"),
    "Explain what this project does": ("read_workflow_summary", "get_project_status"),
    "Where does this project stand?": ("get_project_status",),
    "Start from data I have": ("profile_file", "create_project"),
    "Start from a methodology document": ("create_project",),
    "Change a project that exists": ("list_projects",),
}


def read_every_offer() -> set[str]:
    """Both halves: the per-page tables, and the two fallbacks a page miss lands on."""
    written = {offer for page in PAGE_OPENINGS for offer in page.offers}
    for in_project in (False, True):
        written |= set(choose_opening_turn("/nowhere", in_project).offers)
    return written


# ── the offers are the message ───────────────────────────────────────────────


@pytest.mark.parametrize("offer", sorted(read_every_offer()))
def test_every_offer_rests_on_tools_this_agent_binds(offer: str) -> None:
    assert offer in _OFFERS_REST_ON, f"{offer!r} is offered, but nothing says what keeps it"

    assert set(_OFFERS_REST_ON[offer]) <= _bound_tool_names()


def test_the_page_the_lineage_offer_asks_about_is_described_in_the_prompt() -> None:
    # The one offer resting on no tool. Without the note it has nothing to answer from.
    prompt = render_system_prompt(EDITING_CONFIG, EditingContext(base_url=_BASE_URL))

    assert not _OFFERS_REST_ON["Explain how to use this page"]
    assert ROW_LINEAGE_PAGE_NOTE in prompt


def test_this_agent_binds_nothing_that_publishes() -> None:
    # Publishing is the human's approval; every offer is checked against this set above.
    assert not [name for name in _bound_tool_names() if "publish" in name]


# ── which words a page gets ──────────────────────────────────────────────────


def test_a_chat_opened_on_no_page_at_all_gets_the_ways_in() -> None:
    turn = choose_opening_turn(None, in_project=False)

    assert turn.offers == _WAYS_IN


def test_a_chat_opened_on_a_run_says_so_and_names_the_project(tmp_path) -> None:
    project_id = _make_project(tmp_path)

    message = _opening_message(
        {"project_id": project_id, "opened_on": f"/project/{project_id}/runs/r1"})

    # The rail's own header carries the project's name, so the words need not repeat it.
    assert "this run" in message
    assert project_id not in message


def test_the_lineage_page_gets_words_no_other_page_gets(tmp_path) -> None:
    project_id = _make_project(tmp_path)
    on_run = {"project_id": project_id, "opened_on": f"/project/{project_id}/runs/r1"}

    message = _opening_message(
        on_run | {"opened_on": f"{on_run['opened_on']}/stage/parse/row/0/trace/view"})

    assert "row lineage" in message
    assert message != _opening_message(on_run)


def test_the_address_a_reader_is_actually_on_carries_a_query(tmp_path) -> None:
    """`ChatPanel.here()` sends pathname + search; ?column= is on every lineage link."""
    project_id = _make_project(tmp_path)
    trace = f"/project/{project_id}/runs/r1/stage/paid_totals/row/0/trace/view"

    on_column = _opening_message(
        {"project_id": project_id, "opened_on": f"{trace}?column=total_income_usd"})

    assert on_column == _opening_message(
        {"project_id": project_id, "opened_on": trace})
    assert "row lineage" in on_column


def test_a_page_outside_any_project_gets_the_ways_in_not_a_page_opening() -> None:
    turn = choose_opening_turn("/project/p1/runs/r1", in_project=False)

    assert turn.offers == _WAYS_IN


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
