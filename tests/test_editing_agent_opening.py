"""What the editing agent opens with. Two messages, keyed off the session's project:
a blank chat gets the three ways in, and a session on a project gets an offer of what
this agent can do to one — every clause of which must be a tool it actually binds.
"""
from __future__ import annotations

from app.agents.compiler.config import CONFIG as EDITING_CONFIG
from app.core.agent.session import create_agent_session
from app.core.agent.store import open_session_store
from app.services import project as project_service
from app.services import workspace
from app.tools.editing import EditingContext, build_editing_tools

_store = open_session_store()

_BASE_URL = "https://carbon.example/"

_WAYS_IN = (
    "1. Upload your data and describe the investigation you want to run on it.",
    "2. Upload a methodology document, and the input data it is meant to run on.",
    "3. Describe changes you want made to a project that already exists.",
)

# Each clause of the project-bound message, and the tools that make it true. A clause
# whose tools this agent no longer binds is an offer it cannot keep.
_OFFERS = {
    "Add, edit or remove stages.": ("add_stage", "edit_stage", "delete_stage"),
    "Run it — all of it, or a slice of rows as a test": (
        "run_workflow", "run_workflow_test"),
    "read the rows each stage produced": ("read_stage_output_rows",),
    "Save the result as a version": ("save_version",),
    "with a walkthrough for whoever reviews it": ("write_review_guide",),
}


def _opening_message(context: dict) -> str:
    sid = create_agent_session("editing", context, base_url=_BASE_URL, title="t")
    messages = _store.load(sid)["messages"]
    assert len(messages) == 1, messages
    return messages[0]["parts"][0]["text"]


def _make_project(tmp_path) -> str:
    workspace.set_projects_dir(tmp_path)
    return project_service.create_project("Lobbying spend", "doc text", source="test").id


def _bound_tool_names() -> set[str]:
    context = EditingContext(project_id="anything", base_url=_BASE_URL)
    return {spec.name for spec in build_editing_tools(context)}


def test_a_blank_chat_opens_on_all_three_ways_in() -> None:
    message = _opening_message({})

    for way_in in _WAYS_IN:
        assert way_in in message


def test_a_blank_chat_says_how_a_file_gets_attached() -> None:
    # Two of the three ways in start with an upload, so it is part of the instruction.
    assert "paperclip" in _opening_message({})


def test_a_session_on_a_project_opens_by_naming_it(tmp_path) -> None:
    project_id = _make_project(tmp_path)

    message = _opening_message({"project_id": project_id})

    # The name every other surface shows, not the opaque id the URL carries.
    assert "lobbying_spend" in message
    assert project_id not in message
    assert message != _opening_message({})


def test_every_offer_the_project_message_makes_is_a_tool_this_agent_binds(tmp_path) -> None:
    message = _opening_message({"project_id": _make_project(tmp_path)})
    bound = _bound_tool_names()

    for clause, tools in _OFFERS.items():
        assert clause in message
        assert set(tools) <= bound, f"{clause!r} rests on tools this agent lacks"


def test_the_project_message_promises_no_publishing_because_nothing_publishes(
    tmp_path,
) -> None:
    # Publishing a version is the human's approval, and this agent binds no tool for it.
    assert not [name for name in _bound_tool_names() if "publish" in name]

    assert "Publishing a version stays yours." in _opening_message(
        {"project_id": _make_project(tmp_path)})


def test_the_hook_the_editing_agent_registered_is_the_one_that_runs() -> None:
    # Without this, every test above would pass on an agent carrying no hook.
    assert EDITING_CONFIG.render_opening_message is not None
