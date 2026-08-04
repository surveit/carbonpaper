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
            "`publish` needs BOTH its `publish` block and a `function` block), MANDATORY "
            "output_schema, and inputs each with a MANDATORY `schema`. Every id in inputs "
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


# The model-facing description of each tool, keyed by its __name__ — the same
# keying as TOOL_SCHEMAS and TOOL_LABELS. Held here rather than in the tool's
# docstring so the text the model reads is an explicit registry entry.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_projects": """\
List the names of every authored project in the workspace.""",
    "get_current_project": """\
Return the id of the project this session is editing. Call this FIRST and
pass its value as `project_id` to the other tools.""",
    "describe_workflow": """\
Summarize a project's workflow: each stage's id, type, name, upstream
input ids, and review state. Read this before editing so you know the
current shape. Does not return full stage specs — use read_stage for one.""",
    "read_stage": """\
Return the JSON of one stage from the loaded workflow. Read before editing.""",
    "edit_stage": """\
Change specific fields of one stage. `changes_json` is a JSON object of
ONLY the fields to change (a JSON Merge Patch): {"limit": 100} sets limit;
{"llm": {"model": "claude-opus-5"}} changes only llm.model and leaves the rest of the
llm block intact; {"name": null} deletes a field. Fields you do not mention
are preserved exactly — so you never alter anything you were not asked to.
The result is validated first; if invalid, nothing is written and the issues
are returned. A successful edit drops the node to 'edited_stale' for a
human to re-approve — you cannot approve it yourself. You cannot change a
stage's id this way.""",
    "add_stage": """\
Create a NEW stage in a project's workflow. `stage_json` is a full
stage as JSON: id (new and unique — use edit_stage to change an existing
one), name, type, the config block(s) its type requires (connector / llm /
function / ...; `publish` needs BOTH its `publish` block and a `function`
block), MANDATORY output_schema, and inputs each with a MANDATORY `schema`. Every id
listed in `inputs` must ALREADY be a stage in this workflow — a dangling input
is rejected. The stage-type catalog is
in your instructions; read_stage on a similar existing stage shows the
output_schema / inputs shape. Validated
first; if invalid, nothing is written and the issues are returned. The new
node lands 'unreviewed' for a human to approve.""",
    "remove_stage": """\
Delete one stage from a project's workflow — the undo for a stage you
added. The workflow WITHOUT the stage is validated first: if another stage
still lists it in `inputs`, the removal is refused, nothing is deleted, and
the issues are returned (remove or repoint the downstream stage first).
Removing the last remaining stage is allowed. This edits the project's
workflow directly — use remove_draft_stage for a stage in a draft.""",
    "create_draft": """\
Start a DRAFT: a disposable scratch copy of workflow stages you edit
freely and later freeze with save_version. Each stage you set must be
individually valid, but the WORKFLOW may stay incomplete mid-build (e.g.
a stage whose input references one you have not added yet) until you
save. Pass from_version to seed it from an existing version's stages;
omit it to start empty. Returns the draft, whose `id` (a word triplet
like brisk-otter-lamp) you pass to every draft tool. Drafts are
expendable — if one is lost, start a new one.""",
    "read_draft": """\
The draft's current stages plus `issues` — every cross-stage graph
problem (dangling input, duplicate id, cycle) it would fail on if saved
now ([] means save_version will succeed). Every stored stage is already
individually valid, so `issues` never covers a single stage's own shape.""",
    "set_draft_stage": """\
Add or replace ONE stage in the draft (matched by the stage's `id`).
`stage_json` is the complete stage as a JSON object string. A MALFORMED
stage — invalid JSON, not an object, or failing the stage schema
(unknown type, missing required field, wrong shape, ...) — is REJECTED:
nothing is written, and you get the validation errors back to fix and
retry. A VALID stage whose `inputs` reference a stage id you have not
added yet IS stored — that's the workflow still being built, not a bad
stage — and shows up in the returned `issues`.""",
    "remove_draft_stage": """\
Delete one stage from the draft by id. Removing a stage other stages
still input from leaves dangling edges — visible in `issues` until fixed.""",
    "save_version": """\
Freeze the draft into a new immutable version — your proposal for a
human to review. Validates the whole workflow first: an invalid draft is
refused with the full issue list and nothing is written. The version is
born UNPUBLISHED; only a human can publish it (runs execute published
versions only). `message` says what changed and why, for the reviewer.
Save once per finished proposal, not per edit.""",
    "read_review_guide": """\
The review guide stored on one saved version, or null when it has none.
Read before writing so you amend rather than replace someone's work.""",
    "write_review_guide": """\
Store the walkthrough a human reads to understand what this version of the
workflow does. Replaces any guide already on that version, whole.""",
}
