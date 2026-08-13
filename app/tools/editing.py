"""The in-process tools the editing agent calls to read and edit a project's workflow.

Tools go through the name-based `app.services` surfaces and never build a filesystem
path. A session need not name a project — `get_current_project` returns None when none
is bound. A missing stage or column raises, never an invented default."""

from __future__ import annotations

from typing import Annotated, Any, Callable

from pydantic import BaseModel

from app.core.agent.bound_tool import BoundToolSpec
from app.tools.types import ToolInputSchema
from app.tools import shared, working_copy
from app.tools.submitted_stage import (
    SubmittedStage,
    add_stages_reporting_drops,
    edit_stage_reporting_drops,
)
from app.tools.tool_specs import TOOL_SPECS
from app.services.project import Project


class EditingContext(BaseModel):
    project_id: str | None = None


def make_editing_tools(ctx: EditingContext) -> list[BoundToolSpec]:
    def get_current_project() -> str | None:
        return ctx.project_id

    def create_project(name: str, document: str) -> Project:
        return shared.create_project(name, document, source="editing agent")

    def edit_stage(project_id: str, stage_id: str, changes_json: str) -> dict[str, Any]:
        return edit_stage_reporting_drops(project_id, stage_id, changes_json)

    def add_stage(project_id: str, stages: list[SubmittedStage]) -> dict[str, Any]:
        return add_stages_reporting_drops(project_id, stages)

    def save_version(
        project_id: str, message: str, parent_version: str | None = None
    ) -> dict[str, Any]:
        return working_copy.save_working_copy_as_version(project_id, message, parent_version)

    def list_files(project_id: str | None = None) -> shared.ProjectFilesView:
        where = "/files" if project_id is None else f"/project/{project_id}/files"
        # Root-relative: this reader is already in the app, so the address it was
        # reached on is theirs and not something this session can be told.
        return shared.list_files(project_id, where)

    tools: list[Callable[..., Any]] = [
        get_current_project,
        create_project,
        edit_stage,
        add_stage,
        save_version,
        list_files,
    ]
    return [
        BoundToolSpec(
            name=fn.__name__,
            description=TOOL_SPECS[fn.__name__].description,
            fn=fn,
            input_schema=TOOL_SCHEMAS[fn.__name__],
            label=TOOL_LABELS[fn.__name__],
        )
        for fn in tools
    ] + shared.bind(
        "list_projects", "read_workflow_summary", "read_stage", "remove_stage",
        "read_stage_output_rows", "read_terms", "write_terms",
        "read_review_guide", "write_review_guide",
        "get_project_status", "generate_stage_tests",
        "run_stage_tests", "report_compiler_warnings",
        "move_file_to_project", "profile_file", "survey_workbook",
        "run_workflow", "run_workflow_test", "get_run_status", "sleep",
        "profile_stage_output_data_range",
    )


# ── tool input schemas + display labels ──────────────────────────────────────
# Input schemas keyed by tool __name__, verified against make_editing_tools above.
# Each parameter maps to its type annotation — a plain type like `str` or an
# `Annotated[type, "description"]` the SDK turns into the JSON Schema the CLI sees.
# Empty dict = no parameters. The value type is `object`, not `Any`: the entries are
# opaque type-annotation objects we never introspect, so `object` types them
# honestly without letting `Any` leak past the schema.
TOOL_SCHEMAS: dict[str, ToolInputSchema] = {
    "get_current_project": {},
    "create_project": shared.schema_of("create_project"),
    "edit_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "stage_id": Annotated[str, "The id of the stage to change."],
        "changes_json": Annotated[
            str,
            "A JSON object (encoded as a string) of ONLY the fields to change — a "
            "JSON Merge Patch. Fields you omit are preserved verbatim; a null value "
            "deletes a field. Nested objects merge (they are not replaced whole). "
            'Examples: {"cache": true} turns caching on; {"llm": {"model": "claude-opus-5"}} '
            "changes only llm.model. You cannot change a stage's id this way.",
        ],
    },
    "add_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "stages": Annotated[
            list[SubmittedStage],
            "The complete NEW stages: each with id (new and unique — the stage's only "
            "name), description, type, the "
            "config block(s) its type requires (connector / llm / function / ...; "
            "`publish` needs BOTH its `publish` block and a `function` block), a MANDATORY "
            "`signature`, and inputs each with a MANDATORY `schema`. Every id in inputs "
            "must already be a stage in this workflow or in this same call.",
        ],
    },
    "save_version": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "message": Annotated[
            str,
            "What this version changes and why — shown to the human reviewer "
            "deciding whether to publish it.",
        ],
        "parent_version": Annotated[
            str | None,
            "The version you started this edit FROM, if you loaded one. Omit otherwise: "
            "nothing is inferred from what else the project has stored.",
        ],
    },
    "list_files": {
        "project_id": Annotated[
            str | None,
            "The project whose files to list. Omit for the files that are in no "
            "project yet.",
        ],
    },
}


# Present-tense labels shown in the chat while a tool runs (e.g. "Reading the
# workflow…"), keyed by the bare tool name. The full args/result stay available
# behind a click-to-expand disclosure in the UI.
TOOL_LABELS: dict[str, str] = {
    "get_current_project": "Checking the current project",
    "create_project": "Creating the project",
    "edit_stage": "Editing a stage",
    "add_stage": "Adding a stage",
    "save_version": "Saving the workflow as a version",
    "list_files": "Listing the project's files",
}
