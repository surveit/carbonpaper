"""Tests for the editing agent's tool factory.

Asserts the agent's tools are built and correctly named by checking the tool
factory's output (stable, our own names). The engine wiring is covered by
tests/test_project_chat_sdk.py."""
from __future__ import annotations

from typing import Any, Callable

from app.models import SchemaLibrary, Terms
from app.services import workspace
from app.tools.editing import EditingContext, make_editing_tools
from app.tools.submitted_stage import SubmittedStage

_EXPECTED_TOOL_NAMES = {
    "list_projects",
    "get_current_project",
    "create_project",
    "describe_workflow",
    "read_stage_output_rows",
    "read_stage",
    "edit_stage",
    "add_stage",
    "remove_stage",
    "create_draft",
    "read_draft",
    "set_draft_stage",
    "remove_draft_stage",
    "save_version",
    "read_review_guide",
    "write_review_guide",
    "read_terms",
    "write_terms",
}


def test_editing_tools_factory_yields_expected_tool_names() -> None:
    tools = make_editing_tools(EditingContext(project_id="alpha"))
    assert {spec.name for spec in tools} == _EXPECTED_TOOL_NAMES


_LOAD_STAGE = SubmittedStage.model_validate({
    "id": "load", "description": "Load", "type": "input_data", "connector": {"kind": "file"},
    "signature": {
        "form": "replaces",
        "produces": [{"name": "doc_id", "type": "str", "nullable": False}],
    },
})


def test_a_session_bound_to_no_project_can_build_one_from_nothing(tmp_path) -> None:
    workspace.set_projects_dir(tmp_path)
    call = _tools_of(EditingContext(project_id=None))

    assert call["get_current_project"]() is None
    created = call["create_project"](name="GLP-1 lobbying", document="Follow the filings.")

    project_id = created.project_id
    call["write_terms"](project_id=project_id, terms=Terms(nouns=SchemaLibrary(schemas=[]), verbs=[]))
    added = call["add_stage"](project_id=project_id, stages=[_LOAD_STAGE])

    assert added["added"] == ["load"]
    assert call["describe_workflow"](project_id=project_id).stages[0].id == "load"


def test_creating_a_project_does_not_rebind_the_session(tmp_path) -> None:
    """The prose promises this — a session's binding is what it was OPENED with."""
    workspace.set_projects_dir(tmp_path)
    call = _tools_of(EditingContext(project_id=None))

    call["create_project"](name="second", document="Follow the filings.")

    assert call["get_current_project"]() is None


def _tools_of(ctx: EditingContext) -> dict[str, Callable[..., Any]]:
    return {spec.name: spec.fn for spec in make_editing_tools(ctx)}
