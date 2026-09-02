"""The in-process tools the editing agent calls to read and edit a project's workflow."""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from app.core.agent.bound_tool import BoundToolSpec, bind_by_signature
from app.tools.types import ToolParameterProse
from app.tools import shared, working_copy
from app.tools.submitted_stage import (
    SubmittedStage,
    add_stages_reporting_drops,
    edit_stages_reporting_drops,
)
from app.tools.tool_specs import (
    PROJECT_ID,
    bind,
    read_parameter_prose,
    read_tool_description,
)
from app.models.records.project import Project
from app.models.stage import StageEdit


class EditingContext(BaseModel):
    project_id: str | None = None
    # The address this session's reader is on, written per turn off their own request.
    base_url: str
    # What the link that opened this chat was for, so the opening turn names it.
    task: str | None = None
    # Where the reader is THIS turn. None when the client did not report a page.
    page: str | None = None
    # The page the chat was started from, which is what binds it to a project.
    opened_on: str | None = None


def build_editing_tools(ctx: EditingContext) -> list[BoundToolSpec]:
    base = ctx.base_url.rstrip("/")

    def get_current_url() -> str | None:
        return base + ctx.page if ctx.page else None

    def create_project(name: str, document: str) -> Project:
        return shared.create_project(name, document, source="editing agent")

    def edit_stages(project_id: str, edits: list[StageEdit]) -> shared.EditedStages:
        return edit_stages_reporting_drops(project_id, edits)

    def add_stage(project_id: str, stages: list[SubmittedStage]) -> dict[str, Any]:
        return add_stages_reporting_drops(project_id, stages)

    def save_version(
        project_id: str, message: str, parent_version: str | None = None
    ) -> dict[str, Any]:
        return working_copy.save_working_copy_as_version(project_id, message, parent_version)

    def list_files(project_id: str | None = None) -> shared.ProjectFilesView:
        where = "/files" if project_id is None else f"/project/{project_id}/files"
        return shared.list_files(project_id, base + where)

    def read_stage_output_rows(
        project_id: str, run_id: str, stage_id: str, limit: int | None = None, offset: int = 0
    ) -> shared.StageOutputRows:
        # The shared reader's lineage links are root-relative; this one's are clicked
        # out of a chat bubble.
        return shared.read_stage_output_rows(
            project_id, run_id, stage_id, limit, offset, base_url=base
        )

    tools: list[Callable[..., Any]] = [
        get_current_url,
        create_project,
        edit_stages,
        add_stage,
        save_version,
        list_files,
        read_stage_output_rows,
    ]
    return [
        bind_by_signature(
            name=fn.__name__,
            description=read_tool_description(fn.__name__),
            fn=fn,
            label=TOOL_LABELS[fn.__name__],
            parameters=TOOL_SCHEMAS[fn.__name__],
        )
        for fn in tools
    ] + bind(
        "list_projects", "read_workflow_summary", "read_stage", "delete_stage",
        "read_terms", "write_terms",
        "read_review_guide", "write_review_guide",
        "get_project_status", "generate_stage_tests",
        "run_stage_tests", "report_compiler_warnings",
        "move_file_to_project", "profile_file", "survey_workbook",
        "run_workflow", "run_workflow_test", "list_runs", "get_run_status", "sleep",
        "profile_stage_output_data_range",
    )


# Empty dict = no args. A shared-body tool's prose lives in app.tools.tool_specs instead.
TOOL_SCHEMAS: dict[str, ToolParameterProse] = {
    "get_current_url": {},
    "create_project": {
        "name": "What to CALL the project — a label, shown to the human. Two projects may "
            "share one; the id you work with comes back from this call.",
        "document": "The methodology prose, whole. It becomes the project's source of record, "
            "which every later generation reads — so send what the user wrote, never a "
            "summary of it.",
    },
    "edit_stages": {
        "project_id": PROJECT_ID,
        "edits": "One entry per stage you are changing; entries sent together are validated "
            "and written as one workflow.",
    },
    "add_stage": {
        "project_id": PROJECT_ID,
        "stages": "The complete NEW stages: each with id (new and unique — the stage's only "
            "name), description, type, the "
            "config block(s) its type requires (connector / llm / function / ...; "
            "`report` needs BOTH its `report` block and a `function` block), and a "
            "MANDATORY `signature` — that is where you declare what each input is read "
            "for. An entry in `inputs` carries the upstream stage id and nothing else; "
            "the columns go in `signature.reads`, keyed by the same id. Every id in "
            "inputs must already be a stage in this workflow or in this same call.",
    },
    "save_version": {
        "project_id": PROJECT_ID,
        "message": "What identifies this version to a reader in 150 characters or fewer.",
        "parent_version": "The version you started this edit FROM, if you loaded one. Omit otherwise: "
            "nothing is inferred from what else the project has stored.",
    },
    "list_files": {
        "project_id": f"{PROJECT_ID} Omit it for the files that are in no project yet.",
    },
    "read_stage_output_rows": read_parameter_prose("read_stage_output_rows"),
}


# Present-tense labels shown in the chat while a tool runs (e.g. "Reading the
# workflow…"), keyed by the bare tool name. The full args/result stay available
# behind a click-to-expand disclosure in the UI.
TOOL_LABELS: dict[str, str] = {
    "get_current_url": "Checking the page you have open",
    "create_project": "Creating the project",
    "edit_stages": "Editing the workflow's stages",
    "add_stage": "Adding a stage",
    "save_version": "Saving the workflow as a version",
    "list_files": "Listing the project's files",
    "read_stage_output_rows": "Reading the stage's rows",
}
