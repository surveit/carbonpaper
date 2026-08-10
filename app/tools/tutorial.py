"""The tutorial agent's four tools: seed the committed tour fixture, run it, read it.

Reaches the workspace only through app.services (import_project / start_run /
read_run_status), never sqlite3 or app.core.persistence. No tool here edits a stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Callable

from pydantic import BaseModel

from app.core.agent.bound_tool import BoundToolSpec
from app.models import StageType
from app.services import project as project_service, run as run_service
from app.services.project import WorkflowFile, find_unused_project_name, import_project
from app.tools.tool_specs import CREATE_TUTORIAL_PROJECT, TOOL_SPECS

_FIXTURE_STEM = "tutorial_lobbying_triage"
_DATA_DIR = Path(__file__).resolve().parents[1] / "seeds" / "data"
# The fixture's connector records no path, so the CSV committed beside it is bound here.
_INPUT_STAGE_ID = "raw_filings"


class TutorialContext(BaseModel):
    # Ends in "/": every link the tour hands over is built from it.
    base_url: str


class TutorialStage(BaseModel):
    id: str
    type: str
    description: str


class TutorialProject(BaseModel):
    name: str
    csv_path: str
    stages: list[TutorialStage]
    mcp_command: str


class RunStarted(BaseModel):
    run_id: str
    version_id: str
    status: str
    run_url: str


def make_tutorial_tools(ctx: TutorialContext) -> list[BoundToolSpec]:
    def create_tutorial_project() -> TutorialProject:
        csv_path = _DATA_DIR / f"{_FIXTURE_STEM}.csv"
        workflow_file = _read_fixture_bound_to(csv_path)
        name = import_project(
            workflow_file, name=find_unused_project_name(workflow_file.name)
        )
        return TutorialProject(
            name=name,
            csv_path=str(csv_path),
            stages=[
                TutorialStage(
                    id=s.id, type=StageType(s.type).value, description=s.description
                )
                for s in workflow_file.stages
            ],
            mcp_command=f"claude mcp add --transport http carbonpaper {ctx.base_url}mcp",
        )

    def run_workflow(
        project_id: str,
        version_id: str = "",
        limits: dict[str, int] | None = None,
    ) -> RunStarted:
        run_id = run_service.start_run(
            project_id, version_id=version_id or None, limits=limits
        )
        status = run_service.read_run_status(project_id, run_id)
        return RunStarted(
            run_id=run_id,
            version_id=run_service.read_pinned_version(project_id, run_id),
            status=str(status["status"]),
            run_url=f"{ctx.base_url}project/{project_id}/runs/{run_id}",
        )

    def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
        return run_service.read_run_status(project_id, run_id)

    def describe_workflow(project_id: str) -> dict[str, Any]:
        return project_service.describe_workflow(project_id)

    tools: list[Callable[..., Any]] = [
        create_tutorial_project,
        run_workflow,
        get_run_status,
        describe_workflow,
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


def _read_fixture_bound_to(csv_path: Path) -> WorkflowFile:
    if not csv_path.is_file():
        raise FileNotFoundError(f"the tutorial's bundled CSV is missing: {csv_path}")
    raw: dict[str, Any] = json.loads(
        (_DATA_DIR / f"{_FIXTURE_STEM}.json").read_text(encoding="utf-8")
    )
    bound = [
        _with_csv_path(stage, csv_path) if stage.get("id") == _INPUT_STAGE_ID else stage
        for stage in raw["stages"]
    ]
    # Validated, not patched in place: Connector refuses a relative params.path, so a
    # path a run could not resolve fails here rather than at the first stage.
    return WorkflowFile.model_validate({**raw, "stages": bound})


def _with_csv_path(stage: dict[str, Any], csv_path: Path) -> dict[str, Any]:
    connector = stage["connector"]
    params = {**connector.get("params", {}), "path": str(csv_path)}
    return {**stage, "connector": {**connector, "params": params}}


# ── tool input schemas + display labels ──────────────────────────────────────
# Keyed by tool __name__, verified against make_tutorial_tools above. Same form as
# app.tools.editing's: a plain type, or an Annotated[type, "description"] the SDK
# turns into the JSON Schema the CLI sees. Empty dict = no parameters.
ToolInputSchema = dict[str, object]
TOOL_SCHEMAS: dict[str, ToolInputSchema] = {
    "create_tutorial_project": {},
    "run_workflow": {
        "project_id": Annotated[
            str, "The project name create_tutorial_project returned."
        ],
        "version_id": Annotated[
            str,
            'Optional: the stored version to run. Pass "" for the newest stored one, '
            "or the version_id an earlier run reported to re-run that same workflow.",
        ],
        "limits": Annotated[
            dict[str, int],
            'A per-stage row cap, {"<stage id>": N} — that stage READS only the first '
            "N rows. Omit it to run the whole bound source.",
        ],
    },
    "get_run_status": {
        "project_id": Annotated[
            str, "The project name create_tutorial_project returned."
        ],
        "run_id": Annotated[str, "The run id run_workflow returned."],
    },
    "describe_workflow": {
        "project_id": Annotated[
            str, "The project name create_tutorial_project returned."
        ],
    },
}

_DESCRIPTIONS = TOOL_SPECS | {"create_tutorial_project": CREATE_TUTORIAL_PROJECT}

TOOL_LABELS: dict[str, str] = {
    "create_tutorial_project": "Setting up the tutorial project",
    "run_workflow": "Starting a run",
    "get_run_status": "Checking the run",
    "describe_workflow": "Reading the workflow",
}
