"""The tour's fixture, and the one tool it holds alone because it needs its context.

Importing is app.services.project.import_project — the same call admin's load-bundle
makes. The fixture's CSVs go into the project's files the way any upload does, so no
path is rewritten into the stored workflow and the project stays portable."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from pydantic import BaseModel

from app.core import files as file_store
from app.core.agent.store import NextSteps, Offer
from app.models.records.eval_config import EvalConfig
from app.models.review_guide import ReviewGuideDraft
from app.services import (
    project as project_service,
    run as run_service,
    workspace,
)
from app.services.project import WorkflowFile, import_project
from app.services.errors import CacheArchiveRejected
from app.services.stage_cache_transfer import import_stage_cache
from app.services.project import find_projects_by_name
from app.models.records.project import Project
from app.services import methodology
from app.tools.types import ToolProse

_FIXTURE_STEM = "tutorial_lobbying_triage"
_DATA_DIR = Path(__file__).resolve().parents[1] / "seeds" / "data"
_FIXTURE = _DATA_DIR / f"{_FIXTURE_STEM}.json"
_GUIDE = _DATA_DIR / "review_guides" / f"{_FIXTURE_STEM}.json"
# The project id is minted at import, so the committed eval names no project and is
# told which one it belongs to here.
_EVAL = _DATA_DIR / "evals" / f"{_FIXTURE_STEM}.json"
# Built by scripts/build_tutorial_cache.py from a run of the fixture beside it.
TUTORIAL_CACHE_BUNDLE = _DATA_DIR / f"{_FIXTURE_STEM}.cache.zip"
# The fixture carries no path for these — a committed file cannot know where the
# workspace is — so each run says which file its input stage reads.
_CSV_BY_STAGE_ID = {
    "lobbying_filings": _DATA_DIR / f"{_FIXTURE_STEM}.csv",
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
    eval_url: str
    # A draft: nothing is created until the reader actually replies in it.
    edit_chat_url: str
    # The same agent in a chat bound to NO project, for a reader who wants one of their
    # own: it creates the project from their methodology (create_project) rather than
    # editing the tour's.
    new_project_chat_url: str
    # The advanced handoff, for a reader who would rather stay in the coding assistant
    # they already have open: this workspace speaks MCP at `mcp_url`, and `mcp_command`
    # adds it to Claude Code. Needs the CLI installed.
    mcp_url: str
    mcp_command: str


def seed_tutorial_project(ctx: TutorialContext) -> TutorialAgentReference:
    reused = _find_reusable_tour_project()
    # A second tour reuses what the first seeded: the workspace is not the tour's to
    # fill up, and re-importing would discard whatever the reader did to it.
    name = reused if reused is not None else import_tour_fixture()
    version_id = run_service.resolve_version(name, None)
    if reused is None:
        # A reader who edited the tour has moved past the bundle it no longer matches.
        _seed_stage_cache(name)
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
        input_files=store_tour_files(name),
        workflow_url=f"{ctx.base_url}project/{name}/workflow",
        guide_url=f"{ctx.base_url}project/{name}/workflow/version/{version_id}",
        runs_url_prefix=f"{ctx.base_url}project/{name}/runs/",
        eval_id=eval_config.eval_id,
        eval_url=f"{ctx.base_url}project/{name}/evals/{eval_config.eval_id}",
        edit_chat_url=(
            f"{ctx.base_url.rstrip('/')}/chat/agent/editing/new?"
            f"{urlencode({'project_id': name})}"
        ),
        new_project_chat_url=f"{ctx.base_url.rstrip('/')}/chat/agent/editing/new",
        mcp_url=f"{ctx.base_url}mcp",
        mcp_command=f"claude mcp add --transport http carbonpaper {ctx.base_url}mcp",
    )


def import_tour_fixture() -> str:
    """The committed fixture as a runnable project, before the cache and the tour's extras."""
    for path in (_FIXTURE, _GUIDE, _EVAL, *_CSV_BY_STAGE_ID.values()):
        if not path.is_file():
            raise FileNotFoundError(f"the tutorial fixture needs {path}, which is missing")
    return import_project(
        WorkflowFile.model_validate_json(_FIXTURE.read_text(encoding="utf-8")),
    )


def _seed_stage_cache(project_id: str) -> None:
    """Without it the tour's first run spends the model stage, and needs a key to do it."""
    if not TUTORIAL_CACHE_BUNDLE.is_file():
        raise FileNotFoundError(
            f"the tour needs {TUTORIAL_CACHE_BUNDLE}, which is missing. Build it with "
            "`python -m scripts.build_tutorial_cache`."
        )
    report = import_stage_cache(TUTORIAL_CACHE_BUNDLE.read_bytes(), project_id)
    if report.reachable == 0:
        raise CacheArchiveRejected(
            f"{TUTORIAL_CACHE_BUNDLE.name} carries {report.written} entries and the project "
            f"just seeded from {_FIXTURE.name} can read none of them. The fixture's "
            "stages have moved since the bundle was built; rebuild it with "
            "`python -m scripts.build_tutorial_cache`."
        )


def store_tour_files(project_id: str) -> dict[str, str]:
    """stage id -> file id, stored the way an upload is so the tour shows the real flow."""
    stored = {}
    for stage_id, path in _CSV_BY_STAGE_ID.items():
        with path.open("rb") as handle:
            stored[stage_id] = file_store.save_upload(path.name, handle, project_id).id
    return stored


def read_seed_eval_config(project_id: str) -> EvalConfig:
    return EvalConfig.model_validate(
        {**json.loads(_EVAL.read_text(encoding="utf-8")), "project": project_id})


def _read_seeded_record(project_id: str) -> Project:
    record = Project.load_or_none(project_id)
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


def offer_next_steps(options: list[Offer]) -> str:
    """An echo reads as "carry on": the AI model wrote a second summary and offered again."""
    shown = " | ".join(_describe_offer(o) for o in NextSteps(options=options).options)
    return f"Drawn under this turn as buttons: {shown}. Nothing follows this."


def _describe_offer(offer: Offer) -> str:
    return f"{offer.text} -> {offer.url}" if offer.url else offer.text


# Not in tool_specs: everything there is offered over MCP, where nothing draws a button.
OFFER_NEXT_STEPS = ToolProse(
    parameters={
        "options": "Two to four of them. Each is `text` — at most 70 characters, in the "
            "reader's own voice: \"Open the review queue\", not \"Open the queue for them\" "
            "— and, for a step that is go-and-look-at-a-page, a `url`: the path of a URL a "
            "tool gave you, which opens in a new tab instead of replying to you.",
    },
    description="""Offer the reader replies to click instead of typing.

Call it ONCE, when everything you meant to say is said. A turn cannot end on
a call, so close with one short sentence addressed to the reader — "Say the
word and I'll run it." Never a stage direction: not "(End of turn)", not
"(waiting for your choice)", not "pick one above". The reader can see the
buttons; a line about them is a line about the furniture.

Each option is drawn as a button under this turn, and clicking one sends that
option's exact words as their next message — so these are not a menu you may
narrow the conversation to. The reader can still type anything, and whatever
comes back is an ordinary turn.

Offer only steps you can carry out when one comes back, and never one that just
ends the conversation.

    offer_next_steps(options=[
        {"text": "Open the review queue", "url": "/project/<id>/runs/<id>/queue/<stage>"},
        {"text": "Trace a published figure back to its row"},
    ])""",
)


CREATE_TUTORIAL_PROJECT = ToolProse(
    parameters={},
    description=(
        "Seed the committed tutorial project into this workspace and return it: the "
        "ordinary `project` record (its `id` is what every other tool takes), the stored "
        "version, its `workflow` (every stage's id, type and inputs), the "
        "`input_files` its run needs, `eval_id` and `eval_url`, the URLs of its "
        "workflow, review guide and runs, `edit_chat_url` — a chat with the editing "
        "agent on THIS project, ready to open — `new_project_chat_url`, the same agent in a "
        "chat bound to no project, which is where a reader starts one of their own — and "
        "`mcp_url` with the `mcp_command` that adds it. Takes no arguments — the "
        "fixture is fixed. If the tutorial project is already in this workspace it is "
        "returned as it stands, not replaced, so a second tour never overwrites the first."
    ),
)
