"""The tour's fixture, and the one tool it holds alone because it needs its context.

Importing is app.services.project.import_project — the same call admin's load-bundle
makes. The fixture's CSVs are supplied per run as bindings, so nothing is rewritten
into the stored workflow and the project stays portable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel

from app.core.agent.tool_spec import ToolSpec
from app.tools.types import ToolInputSchema
from app.models import EvalConfig
from app.models.review_guide import ReviewGuideDraft
from app.services import (
    editing_session,
    project as project_service,
    run as run_service,
    workspace,
)
from app.services.project import WorkflowFile, import_project
from app.services.project import find_projects_by_name

_FIXTURE_STEM = "tutorial_lobbying_triage"
_DATA_DIR = Path(__file__).resolve().parents[1] / "seeds" / "data"
_FIXTURE = _DATA_DIR / f"{_FIXTURE_STEM}.json"
_GUIDE = _DATA_DIR / "review_guides" / f"{_FIXTURE_STEM}.json"
# The project id is minted at import, so the committed eval names no project and is
# told which one it belongs to here.
_EVAL = _DATA_DIR / "evals" / f"{_FIXTURE_STEM}.json"
# The fixture carries no path for these — a committed file cannot know where the
# workspace is — so each run says which file its input stage reads.
_CSV_BY_STAGE_ID = {
    "raw_filings": _DATA_DIR / f"{_FIXTURE_STEM}.csv",
    "public_commitments": _DATA_DIR / "tutorial_public_commitments.csv",
}


class TutorialContext(BaseModel):
    # Ends in "/": every link the tour hands over is built from it.
    base_url: str


class TutorialProject(BaseModel):
    name: str
    version_id: str
    # The stages as seeded: the tour reads its stage ids and types off this rather than
    # off a name written into its prompt.
    workflow: workspace.WorkflowSummary
    # Pass straight to run_workflow's `bindings`: which file each input stage reads.
    input_bindings: dict[str, dict[str, str]]
    workflow_url: str
    guide_url: str
    runs_url_prefix: str
    # Both halves: the page to hand over, and the id run_eval takes. Slicing the id
    # off the URL is a guess, and the seeded eval is the only one that answers here.
    eval_url: str
    eval_id: str
    # The three ways to say the same handoff, headline first: this workspace speaks MCP
    # at `mcp_url`, so an assistant the reader ALREADY has open can be told to connect
    # to it — `mcp_ask_your_assistant` is what they say. `mcp_command` is the same thing
    # for someone who would rather type it at a terminal, and needs the CLI installed.
    mcp_url: str
    mcp_ask_your_assistant: str
    mcp_command: str


class EditingChat(BaseModel):
    project: str
    # Live the moment this is returned: the agent is waiting in that chat.
    edit_chat_url: str


def seed_tutorial_project(ctx: TutorialContext) -> TutorialProject:
    name = _find_reusable_tour_project()
    # A second tour reuses what the first seeded: the workspace is not the tour's to
    # fill up, and re-importing would discard whatever the reader did to it.
    if name is None:
        for path in (_FIXTURE, _GUIDE, _EVAL, *_CSV_BY_STAGE_ID.values()):
            if not path.is_file():
                raise FileNotFoundError(f"the tutorial fixture needs {path}, which is missing")
        name = import_project(
            WorkflowFile.model_validate_json(_FIXTURE.read_text(encoding="utf-8")),
        )
    version_id = run_service.resolve_version(name, None)
    project_service.write_review_guide(
        name, version_id, ReviewGuideDraft.model_validate_json(
            _GUIDE.read_text(encoding="utf-8")
        )
    )
    eval_config = read_seed_eval_config(name)
    project_service.write_eval_config(name, eval_config)
    return TutorialProject(
        name=name,
        version_id=version_id,
        workflow=project_service.describe_workflow(name),
        input_bindings={
            stage_id: {"path": str(path), "format": "csv"}
            for stage_id, path in _CSV_BY_STAGE_ID.items()
        },
        workflow_url=f"{ctx.base_url}project/{name}/workflow",
        guide_url=f"{ctx.base_url}project/{name}/workflow/version/{version_id}",
        runs_url_prefix=f"{ctx.base_url}project/{name}/runs/",
        eval_url=f"{ctx.base_url}project/{name}/evals/{eval_config.id}",
        eval_id=eval_config.id,
        mcp_url=f"{ctx.base_url}mcp",
        mcp_ask_your_assistant=(
            f"Add the MCP server at {ctx.base_url}mcp over streamable HTTP, then use "
            "its tools to turn my methodology into a workflow."
        ),
        mcp_command=f"claude mcp add --transport http carbonpaper {ctx.base_url}mcp",
    )


def open_editing_chat(ctx: TutorialContext, project: str) -> EditingChat:
    return EditingChat(
        project=project,
        edit_chat_url=ctx.base_url.rstrip("/") + editing_session.open_editing_chat(project),
    )


def read_seed_eval_config(project: str) -> EvalConfig:
    return EvalConfig.model_validate(
        {**json.loads(_EVAL.read_text(encoding="utf-8")), "project": project})


def _find_reusable_tour_project() -> str | None:
    """Newest first, so a reader who toured twice lands on the tour they last used."""
    label = project_service.sanitize_project_name(_FIXTURE_STEM)
    seeded = sorted(
        (record.id for record in find_projects_by_name(label)), reverse=True
    )
    return next((project_id for project_id in seeded if _is_on_disk(project_id)), None)


def _is_on_disk(project_id: str) -> bool:
    """A project the workspace can run — not merely an id the store knows."""
    return (workspace.projects_dir() / project_id / "document.md").is_file()


CREATE_TUTORIAL_PROJECT = ToolSpec(
    name="create_tutorial_project",
    description=(
        "Seed the committed tutorial project into this workspace and return it: its "
        "name, the stored version, its `workflow` (every stage's id, type and inputs), "
        "the `input_bindings` its run needs, the URLs of its workflow, review guide, "
        "seeded eval and runs, and the three forms of the MCP handoff. Takes no arguments — the "
        "fixture is fixed. If the tutorial project is already in this workspace it is "
        "returned as it stands, not replaced, so a second tour never overwrites the first."
    ),
)

OPEN_EDITING_CHAT = ToolSpec(
    name="open_editing_chat",
    description=(
        "Open a NEW chat with the editing agent on this project and return "
        "`edit_chat_url`, the page it is already waiting on. Hand that URL over as it "
        "stands: the reader clicks it and is in a conversation with the agent that "
        "WRITES stages from their methodology, which you cannot do. It is the same "
        "session the 'Edit with agent' button opens, so the link and the button are not "
        "two offers. Call it only once the reader has said they want it — every call "
        "mints a session, and a second one leaves an empty chat behind them."
    ),
)

OPEN_EDITING_CHAT_SCHEMA: ToolInputSchema = {
    "project_id": Annotated[str, "The project the editing agent opens on."],
}
