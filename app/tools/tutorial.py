"""The tutorial agent's four tools: seed the committed tour fixture, run it, read it.

Reaches the workspace only through app.services (import_project / start_run /
wait_for_run_to_finish), never sqlite3 or app.core.persistence. No tool edits a stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Callable

from pydantic import BaseModel

from app.core.agent.bound_tool import BoundToolSpec
from app.models import StageType
from app.models.review_guide import ReviewGuideDraft
from app.services import project as project_service, run as run_service
from app.services.project import WorkflowFile, find_unused_project_name, import_project
from app.tools.run_tools import (
    RUN_TOOL_LABELS,
    RUN_TOOL_SCHEMAS,
    RunStarted,
    start_run_of_stored_workflow,
    wait_for_started_run,
)
from app.tools.tool_specs import CREATE_TUTORIAL_PROJECT, TOOL_SPECS

_FIXTURE_STEM = "tutorial_lobbying_triage"
_DATA_DIR = Path(__file__).resolve().parents[1] / "seeds" / "data"
# Not under data/*.json, which app.seeds.seed globs as WorkflowFile fixtures.
_GUIDE_PATH = _DATA_DIR / "review_guides" / f"{_FIXTURE_STEM}.json"
# The fixture records no path at all, so every file committed beside it is bound here:
# an input stage id -> the CSV it reads, plus the report template the publish stage fills.
CSV_BY_STAGE_ID = {
    "raw_filings": _DATA_DIR / f"{_FIXTURE_STEM}.csv",
    "public_commitments": _DATA_DIR / "tutorial_public_commitments.csv",
}
_PUBLISH_STAGE_ID = "publish_report"
_TEMPLATE_PATH = _DATA_DIR / "tutorial_triage_report.html"
_TEMPLATE_TOKEN = "[[TEMPLATE_PATH]]"


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
    mcp_command: str


def make_tutorial_tools(ctx: TutorialContext) -> list[BoundToolSpec]:
    def create_tutorial_project() -> TutorialProject:
        workflow_file = _read_fixture_bound_to(CSV_BY_STAGE_ID, _TEMPLATE_PATH)
        name = import_project(
            workflow_file, name=find_unused_project_name(workflow_file.name)
        )
        version_id = _write_bundled_review_guide(name)
        return TutorialProject(
            name=name,
            version_id=version_id,
            bound_inputs=[
                BoundInput(stage_id=stage_id, csv_path=str(path))
                for stage_id, path in CSV_BY_STAGE_ID.items()
            ],
            stages=[
                TutorialStage(
                    id=s.id, type=StageType(s.type).value, description=s.description
                )
                for s in workflow_file.stages
            ],
            workflow_url=f"{ctx.base_url}project/{name}/workflow",
            guide_url=f"{ctx.base_url}project/{name}/workflow/version/{version_id}",
            mcp_command=f"claude mcp add --transport http carbonpaper {ctx.base_url}mcp",
        )

    def run_workflow(
        project_id: str,
        version_id: str = "",
        limits: dict[str, int] | None = None,
    ) -> RunStarted:
        return start_run_of_stored_workflow(
            project_id, version_id, limits, base_url=ctx.base_url
        )

    def wait_for_run(
        project_id: str, run_id: str, timeout_seconds: int = 0
    ) -> run_service.RunOutcome:
        return wait_for_started_run(project_id, run_id, timeout_seconds)

    def describe_workflow(project_id: str) -> dict[str, Any]:
        return project_service.describe_workflow(project_id)

    tools: list[Callable[..., Any]] = [
        create_tutorial_project,
        run_workflow,
        wait_for_run,
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


def _write_bundled_review_guide(project_name: str) -> str:
    """Returns the version the guide was written for: the one import_project minted."""
    version_id = run_service.resolve_version(project_name, None)
    # A WorkflowFile carries no review state (#135), so the guide is committed beside
    # the fixture instead and stored separately, here.
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )
    project_service.write_review_guide(project_name, version_id, guide)
    return version_id


def _read_fixture_bound_to(
    csv_by_stage_id: dict[str, Path], template_path: Path
) -> WorkflowFile:
    for path in (*csv_by_stage_id.values(), template_path):
        if not path.is_file():
            raise FileNotFoundError(f"a file the tutorial fixture needs is missing: {path}")
    raw: dict[str, Any] = json.loads(
        (_DATA_DIR / f"{_FIXTURE_STEM}.json").read_text(encoding="utf-8")
    )
    bound = [_bind_stage(stage, csv_by_stage_id, template_path) for stage in raw["stages"]]
    # Validated, not patched in place: Connector refuses a relative params.path, so a
    # path a run could not resolve fails here rather than at the first stage.
    return WorkflowFile.model_validate({**raw, "stages": bound})


def _bind_stage(
    stage: dict[str, Any], csv_by_stage_id: dict[str, Path], template_path: Path
) -> dict[str, Any]:
    csv_path = csv_by_stage_id.get(str(stage.get("id")))
    if csv_path is not None:
        return _with_csv_path(stage, csv_path)
    if stage.get("id") == _PUBLISH_STAGE_ID:
        return _with_template_path(stage, template_path)
    return stage


def _with_csv_path(stage: dict[str, Any], csv_path: Path) -> dict[str, Any]:
    connector = stage["connector"]
    params = {**connector.get("params", {}), "path": str(csv_path)}
    return {**stage, "connector": {**connector, "params": params}}


def _with_template_path(stage: dict[str, Any], template_path: Path) -> dict[str, Any]:
    # Posix: this lands in a Python string literal, where a backslash is an escape.
    function = stage["function"]
    code: str = function["code"]
    if _TEMPLATE_TOKEN not in code:
        raise ValueError(
            f"stage {stage.get('id')} no longer carries {_TEMPLATE_TOKEN}, so the report "
            f"template at {template_path} would never be read"
        )
    return {
        **stage,
        "function": {**function, "code": code.replace(_TEMPLATE_TOKEN, template_path.as_posix())},
    }


# ── tool input schemas + display labels ──────────────────────────────────────
# Keyed by tool __name__, verified against make_tutorial_tools above. Same form as
# app.tools.editing's: a plain type, or an Annotated[type, "description"] the SDK
# turns into the JSON Schema the CLI sees. Empty dict = no parameters.
ToolInputSchema = dict[str, object]
TOOL_SCHEMAS: dict[str, ToolInputSchema] = {
    "create_tutorial_project": {},
    "describe_workflow": {
        "project_id": Annotated[
            str, "The project name create_tutorial_project returned."
        ],
    },
    **RUN_TOOL_SCHEMAS,
}

_DESCRIPTIONS = TOOL_SPECS | {"create_tutorial_project": CREATE_TUTORIAL_PROJECT}

TOOL_LABELS: dict[str, str] = {
    "create_tutorial_project": "Setting up the tutorial project",
    "describe_workflow": "Reading the workflow",
    **RUN_TOOL_LABELS,
}
