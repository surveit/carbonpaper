"""Tests for the editing agent's tool factory.

Asserts the agent's tools are built and correctly named by checking the tool
factory's output (stable, our own names). The engine wiring is covered by
tests/test_project_chat_sdk.py."""
from __future__ import annotations

from typing import Any, Callable

from app.models import SchemaLibrary, Terms
from app.services import workspace
from app.tools.editing import EditingContext, build_editing_tools
from app.tools.submitted_stage import SubmittedStage

_EXPECTED_TOOL_NAMES = {
    "list_projects",
    "get_current_url",
    "create_project",
    "read_workflow_summary",
    "read_stage_output_rows",
    "read_stage",
    "edit_stages",
    "add_stage",
    "delete_stage",
    "save_version",
    "read_review_guide",
    "write_review_guide",
    "read_terms",
    "write_terms",
    "get_project_status",
    "generate_stage_tests",
    "run_stage_tests",
    "report_compiler_warnings",
    "list_files",
    "move_file_to_project",
    "profile_file",
    "survey_workbook",
    "run_workflow",
    "run_workflow_test",
    "list_runs",
    "get_run_status",
    "sleep",
    "profile_stage_output_data_range",
}


def test_editing_tools_factory_yields_expected_tool_names() -> None:
    tools = build_editing_tools(EditingContext(project_id="alpha", base_url="http://reader.test/"))
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
    call = _tools_of(EditingContext(project_id=None, base_url="http://reader.test/"))

    created = call["create_project"](name="GLP-1 lobbying", document="Follow the filings.")

    project_id = created.id
    call["write_terms"](project_id=project_id, terms=Terms(nouns=SchemaLibrary(schemas=[]), verbs=[]))
    added = call["add_stage"](project_id=project_id, stages=[_LOAD_STAGE])

    assert added["added"] == ["load"]
    assert call["read_workflow_summary"](project_id=project_id).stages[0].id == "load"


def test_creating_a_project_does_not_rebind_the_session(tmp_path) -> None:
    """The session note promises this — a binding is what the chat was OPENED with."""
    workspace.set_projects_dir(tmp_path)
    context = EditingContext(project_id=None, base_url="http://reader.test/")

    _tools_of(context)["create_project"](name="second", document="Follow the filings.")

    assert context.project_id is None


def _tools_of(ctx: EditingContext) -> dict[str, Callable[..., Any]]:
    return {spec.name: spec.fn for spec in build_editing_tools(ctx)}
