"""The in-process tools the editing agent calls to read and edit a project's workflow.

Tools go through the name-based `app.services` surfaces and never build a filesystem
path. `get_current_project` must be called first — its value is what every other tool
passes as `project_id`. A missing stage or column raises, never an invented default."""

from __future__ import annotations

from typing import Annotated, Any, Callable

from pydantic import BaseModel

from app.core.agent.bound_tool import BoundToolSpec
from app.models import StageDraft
from app.models.review_guide import ReviewGuideDraft
from app.services.versioning import ReviewGuide
from app.services import drafts, project as project_service
from app.tools.tool_specs import SAVE_VERSION_FROM_DRAFT, TOOL_SPECS
from app.services.drafts import DraftDetail, DraftEdit, DraftView, SaveResult


class EditingContext(BaseModel):
    """What one editing session needs to bind its tools: the project it edits."""

    project_id: str


def make_editing_tools(ctx: EditingContext) -> list[BoundToolSpec]:
    def list_projects() -> list[str]:
        return project_service.list_projects()

    def get_current_project() -> str:
        return ctx.project_id

    def describe_workflow(project_id: str) -> dict[str, Any]:
        return project_service.describe_workflow(project_id)

    def read_stage(project_id: str, stage_id: str) -> str:
        return project_service.read_stage(project_id, stage_id)

    def edit_stage(project_id: str, stage_id: str, changes_json: str) -> dict[str, Any]:
        result = project_service.edit_stage(project_id, stage_id, changes_json)
        return {"ok": result.ok, "issues": result.issues}

    def add_stage(project_id: str, stages: list[StageDraft]) -> dict[str, Any]:
        return project_service.add_stages_reporting_drops(project_id, stages)

    def remove_stage(project_id: str, stage_id: str) -> dict[str, Any]:
        result = project_service.remove_stage(project_id, stage_id)
        return {"ok": result.ok, "issues": result.issues}

    def create_draft(project_id: str, from_version: str = "") -> DraftView:
        return drafts.create_draft(project_id, from_version=from_version or None)

    def read_draft(project_id: str, draft_id: str) -> DraftDetail:
        return drafts.read_draft(project_id, draft_id)

    def set_draft_stage(project_id: str, draft_id: str, stage_json: str) -> DraftEdit:
        return drafts.set_draft_stage(project_id, draft_id, stage_json)

    def remove_draft_stage(project_id: str, draft_id: str, stage_id: str) -> DraftEdit:
        return drafts.remove_draft_stage(project_id, draft_id, stage_id)

    def save_version(project_id: str, draft_id: str, message: str) -> SaveResult:
        return drafts.save_version(project_id, draft_id, message=message)

    def read_review_guide(project_id: str, version_id: str) -> ReviewGuide | None:
        return project_service.read_review_guide(project_id, version_id)

    def write_review_guide(
        project_id: str, version_id: str, guide: ReviewGuideDraft
    ) -> ReviewGuide:
        return project_service.write_review_guide(project_id, version_id, guide)

    tools: list[Callable[..., Any]] = [
        list_projects,
        get_current_project,
        describe_workflow,
        read_stage,
        edit_stage,
        add_stage,
        remove_stage,
        create_draft,
        read_draft,
        set_draft_stage,
        remove_draft_stage,
        save_version,
        read_review_guide,
        write_review_guide,
    ]
    return [
        BoundToolSpec(
            name=fn.__name__,
            description=_DESCRIPTIONS[fn.__name__].description,
            fn=fn,
            input_schema=TOOL_SCHEMAS[fn.__name__],
            label=TOOL_LABELS[fn.__name__],
        )
        for fn in tools
    ]


# ── tool input schemas + display labels ──────────────────────────────────────
# Input schemas keyed by tool __name__, verified against make_editing_tools above.
# Each parameter maps to its type annotation — a plain type like `str` or an
# `Annotated[type, "description"]` the SDK turns into the JSON Schema the CLI sees.
# Empty dict = no parameters. The value type is `object`, not `Any`: the entries are
# opaque type-annotation objects we never introspect, so `object` types them
# honestly without letting `Any` leak past the schema.
ToolInputSchema = dict[str, object]
TOOL_SCHEMAS: dict[str, ToolInputSchema] = {
    "list_projects": {},
    "get_current_project": {},
    "describe_workflow": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
    },
    "read_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "stage_id": Annotated[str, "The stage's id, as shown by describe_workflow."],
    },
    "edit_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "stage_id": Annotated[str, "The id of the stage to change."],
        "changes_json": Annotated[
            str,
            "A JSON object (encoded as a string) of ONLY the fields to change — a "
            "JSON Merge Patch. Fields you omit are preserved verbatim; a null value "
            "deletes a field. Nested objects merge (they are not replaced whole). "
            'Examples: {"limit": 100} sets limit; {"llm": {"model": "claude-opus-5"}} '
            "changes only llm.model. You cannot change a stage's id this way.",
        ],
    },
    "add_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "stages": Annotated[
            list[StageDraft],
            "The complete NEW stages: each with id (new and unique), name, type, the "
            "config block(s) its type requires (connector / llm / function / ...; "
            "`publish` needs BOTH its `publish` block and a `function` block), a MANDATORY "
            "`signature`, and inputs each with a MANDATORY `schema`. Every id in inputs "
            "must already be a stage in this workflow or in this same call.",
        ],
    },
    "remove_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "stage_id": Annotated[
            str,
            "The id of the stage to delete from the workflow. Refused if another "
            "stage still lists it in its inputs.",
        ],
    },
    "create_draft": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "from_version": Annotated[
            str,
            "Optional: a version id whose stages seed the draft. Omit (or pass "
            '"") to start from an empty stage list.',
        ],
    },
    "read_draft": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "draft_id": Annotated[str, "The word-triplet id returned by create_draft."],
    },
    "set_draft_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "draft_id": Annotated[str, "The word-triplet id returned by create_draft."],
        "stage_json": Annotated[
            str,
            "The complete stage as a JSON object (encoded as a string), including "
            "its id. An existing stage with the same id is replaced; otherwise "
            "the stage is added. A malformed stage (bad JSON, wrong shape, unknown "
            "type) is rejected outright and nothing is written.",
        ],
    },
    "remove_draft_stage": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "draft_id": Annotated[str, "The word-triplet id returned by create_draft."],
        "stage_id": Annotated[str, "The id of the stage to delete from the draft."],
    },
    "save_version": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "draft_id": Annotated[str, "The word-triplet id returned by create_draft."],
        "message": Annotated[
            str,
            "What this version changes and why — shown to the human reviewer "
            "deciding whether to publish it.",
        ],
    },
    "read_review_guide": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "version_id": Annotated[
            str,
            "The version whose guide to read — the id save_version returned for it.",
        ],
    },
    "write_review_guide": {
        "project_id": Annotated[str, "The project id (call get_current_project first)."],
        "version_id": Annotated[
            str,
            "The version this guide describes — the id save_version returned for it. The "
            "guide is validated against THAT version's stages.",
        ],
        "guide": Annotated[
            ReviewGuide,
            "The complete guide: `steps`, each with `title`, `prose` and `stage_ids`, "
            "plus `unnarrated`. Sent whole every time — it replaces any earlier guide "
            "rather than merging into it.",
        ],
    },
}


# This agent's own view of the shared registry: every tool as described there,
# except save_version — the agent freezes a DRAFT, the glassbox server snapshots the
# working copy, so the two carry different prose under one name (see issue #357).
_DESCRIPTIONS = TOOL_SPECS | {"save_version": SAVE_VERSION_FROM_DRAFT}


# Present-tense labels shown in the chat while a tool runs (e.g. "Reading the
# workflow…"), keyed by the bare tool name. The full args/result stay available
# behind a click-to-expand disclosure in the UI.
TOOL_LABELS: dict[str, str] = {
    "list_projects": "Listing projects",
    "get_current_project": "Checking the current project",
    "describe_workflow": "Reading the workflow",
    "read_stage": "Reading a stage",
    "edit_stage": "Editing a stage",
    "add_stage": "Adding a stage",
    "remove_stage": "Removing a stage",
    "create_draft": "Starting a draft",
    "read_draft": "Reading the draft",
    "set_draft_stage": "Editing the draft",
    "remove_draft_stage": "Removing a draft stage",
    "save_version": "Saving the draft as a version",
    "read_review_guide": "Reading the review guide",
    "write_review_guide": "Writing the review guide",
}
