"""The tour's fixture, and the one tool it holds alone because it needs its context.

Importing is app.services.project.import_project — the same call admin's load-bundle
makes. The fixture's CSVs go into the project's files the way any upload does, so no
path is rewritten into the stored workflow and the project stays portable."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from app.models import EvalConfig
from app.models.review_guide import ReviewGuideDraft
from app.services import (
    agent as agent_service,
    project as project_service,
    run as run_service,
    uploads,
    workspace,
)
from app.services.project import Project, WorkflowFile, import_project
from app.services.project import find_projects_by_name
from app.services import methodology
from app.tools.types import ToolProse

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


class TutorialAgentReference(BaseModel):
    """The seeded project as any surface holds it, plus what only the tour needs."""

    project: Project
    version_id: str
    # The stages as seeded: the tour reads its stage ids and types off this rather than
    # off a name written into its prompt.
    workflow: workspace.WorkflowSummary
    # Pass straight to run_workflow's `files`: which stored file each input step reads.
    input_files: dict[str, str]
    workflow_url: str
    guide_url: str
    runs_url_prefix: str
    # What run_eval takes. Slicing it off a URL is a guess, and the seeded eval is the
    # only one that answers here.
    eval_id: str
    # Live the moment this is returned: the editing agent is waiting in that chat.
    edit_chat_url: str
    # The three ways to say the same handoff, headline first: this workspace speaks MCP
    # at `mcp_url`, so an assistant the reader ALREADY has open can be told to connect
    # to it — `mcp_ask_your_assistant` is what they say. `mcp_command` is the same thing
    # for someone who would rather type it at a terminal, and needs the CLI installed.
    mcp_url: str
    mcp_ask_your_assistant: str
    mcp_command: str


def seed_tutorial_project(ctx: TutorialContext) -> TutorialAgentReference:
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
    return TutorialAgentReference(
        project=_read_seeded_record(name),
        version_id=version_id,
        workflow=project_service.read_workflow_summary(name),
        input_files=_store_tour_files(name),
        workflow_url=f"{ctx.base_url}project/{name}/workflow",
        guide_url=f"{ctx.base_url}project/{name}/workflow/version/{version_id}",
        runs_url_prefix=f"{ctx.base_url}project/{name}/runs/",
        eval_id=eval_config.id,
        edit_chat_url=ctx.base_url.rstrip("/") + agent_service.open_agent_chat(
            "editing", name),
        mcp_url=f"{ctx.base_url}mcp",
        mcp_ask_your_assistant=(
            f"Add the MCP server at {ctx.base_url}mcp over streamable HTTP, then use "
            "its tools to turn my methodology into a workflow."
        ),
        mcp_command=f"claude mcp add --transport http carbonpaper {ctx.base_url}mcp",
    )


def _store_tour_files(project_id: str) -> dict[str, str]:
    """stage id -> sha256, stored the way an upload is so the tour shows the real flow."""
    stored = {}
    for stage_id, path in _CSV_BY_STAGE_ID.items():
        with path.open("rb") as handle:
            stored[stage_id] = uploads.save_upload(path.name, handle, project_id).sha256
    return stored


def read_seed_eval_config(project_id: str) -> EvalConfig:
    return EvalConfig.model_validate(
        {**json.loads(_EVAL.read_text(encoding="utf-8")), "project": project_id})


def _read_seeded_record(project_id: str) -> Project:
    record = project_service.read_project_record(project_id)
    if record is None:
        raise FileNotFoundError(
            f"the tour just seeded '{project_id}', but no project record can be read for it")
    return record


def _find_reusable_tour_project() -> str | None:
    """Newest first, so a reader who toured twice lands on the tour they last used."""
    label = project_service.sanitize_project_name(_FIXTURE_STEM)
    seeded = sorted(
        (record.id for record in find_projects_by_name(label)), reverse=True
    )
    return next((project_id for project_id in seeded if _is_on_disk(project_id)), None)


def _is_on_disk(project_id: str) -> bool:
    """A project the workspace can run — not merely an id the store knows."""
    return (workspace.projects_dir() / project_id).is_dir() and methodology.exists(project_id)


CREATE_TUTORIAL_PROJECT = ToolProse(
    parameters={},
    description=(
        "Seed the committed tutorial project into this workspace and return it: the "
        "ordinary `project` record (its `id` is what every other tool takes), the stored "
        "version, its `workflow` (every stage's id, type and inputs), the "
        "`input_files` its run needs, `eval_id`, the URLs of its workflow, review "
        "guide and runs, `edit_chat_url` — a chat with the editing agent, already open "
        "and waiting — and the three forms of the MCP handoff. Takes no arguments — the "
        "fixture is fixed. If the tutorial project is already in this workspace it is "
        "returned as it stands, not replaced, so a second tour never overwrites the first."
    ),
)
