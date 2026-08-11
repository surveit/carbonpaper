"""The tour's fixture, and the one tool the tutorial agent does not share.

Which files the fixture ships is data; importing it is app.seeds.fixture_project's job.
The agent's other three tools are the app's own — see app.agents.tutorial.config."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.core.agent.tool_spec import ToolSpec
from app.models import StageType
from app.seeds.fixture_project import (
    FixtureFiles,
    SeededProject,
    import_fixture_as_project,
)
from app.services import project as project_service, run as run_service, workspace
from app.services.loader import load_workflow
from app.services.project import Project

_FIXTURE_STEM = "tutorial_lobbying_triage"
_DATA_DIR = Path(__file__).resolve().parents[1] / "seeds" / "data"
_FIXTURE = _DATA_DIR / f"{_FIXTURE_STEM}.json"
# What this fixture ships beside itself. The review guide sits in a subdirectory so the
# fixture glob in app.seeds.seed never reads one as a workflow.
_FIXTURE_FILES = FixtureFiles(
    inputs={
        "raw_filings": _DATA_DIR / f"{_FIXTURE_STEM}.csv",
        "public_commitments": _DATA_DIR / "tutorial_public_commitments.csv",
    },
    code_files={"[[TEMPLATE_PATH]]": _DATA_DIR / "tutorial_triage_report.html"},
    review_guide=_DATA_DIR / "review_guides" / f"{_FIXTURE_STEM}.json",
)


class TutorialContext(BaseModel):
    # Ends in "/": every link the tour hands over is built from it.
    base_url: str


class TutorialStage(BaseModel):
    id: str
    type: str
    description: str


class BoundInput(BaseModel):
    stage_id: str
    csv_path: str


class TutorialProject(BaseModel):
    name: str
    version_id: str
    bound_inputs: list[BoundInput]
    stages: list[TutorialStage]
    workflow_url: str
    guide_url: str
    runs_url_prefix: str
    mcp_command: str


def seed_tutorial_project(ctx: TutorialContext) -> TutorialProject:
    name = _tutorial_project_name()
    # A second tour reuses what the first one seeded: the workspace is not the tour's to
    # fill up, and re-importing would discard whatever the reader did to it.
    seeded = (
        SeededProject(name=name, version_id=run_service.resolve_version(name, None))
        if _is_on_disk(name)
        else import_fixture_as_project(_FIXTURE, _FIXTURE_FILES, name=name)
    )
    stages = load_workflow(workspace.resolve_project_dir(seeded.name))
    return TutorialProject(
        name=seeded.name,
        version_id=seeded.version_id,
        bound_inputs=[
            BoundInput(stage_id=stage_id, csv_path=str(path))
            for stage_id, path in _FIXTURE_FILES.inputs.items()
        ],
        stages=[
            TutorialStage(id=s.id, type=StageType(s.type).value, description=s.description)
            for s in stages
        ],
        workflow_url=f"{ctx.base_url}project/{seeded.name}/workflow",
        guide_url=f"{ctx.base_url}project/{seeded.name}/workflow/version/{seeded.version_id}",
        # run_workflow returns a bare run_id, as it does on every surface, so the tour
        # joins this prefix to it rather than inventing a host.
        runs_url_prefix=f"{ctx.base_url}project/{seeded.name}/runs/",
        mcp_command=f"claude mcp add --transport http carbonpaper {ctx.base_url}mcp",
    )


def _tutorial_project_name() -> str:
    """`base` unless a DELETED project still holds it; then base_2, base_3 …"""
    base = project_service.sanitize_project_name(_FIXTURE_STEM)
    candidate, suffix = base, 1
    # A second tour reuses the project the first one seeded. But delete_project rmtree's
    # the directory and leaves the store record, and create_project refuses a name whose
    # record exists, so a deleted project's name is reusable by neither route. Stepping
    # over it is a workaround for #544; delete this once that lands.
    while not _is_on_disk(candidate) and Project.exists(candidate):
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


def _is_on_disk(name: str) -> bool:
    """A project the workspace can run — not merely a name the store knows."""
    return (workspace.projects_dir() / name / "document.md").is_file()


CREATE_TUTORIAL_PROJECT = ToolSpec(
    name="create_tutorial_project",
    description=(
        "Seed the committed tutorial project into this workspace and return it: its "
        "name, the stored version, the CSVs bound to each input stage, its stages, and "
        "the URLs of its workflow, review guide and runs. Takes no arguments — the "
        "fixture is fixed. If the tutorial project is already in this workspace it is "
        "returned as it stands, not replaced, so a second tour never overwrites the first."
    ),
)
