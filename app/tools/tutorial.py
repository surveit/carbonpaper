"""The tour's fixture, and the two tools it holds alone because they need its context.

Importing is app.services.project.import_project — the same call admin's load-bundle
makes. The fixture's CSVs are supplied per run as bindings, so nothing is rewritten
into the stored workflow and the project stays portable."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.agent.tool_spec import ToolSpec
from app.core.frames import collapse_null_forms, convert_cell_to_json_native, list_rows
from app.core.run_status import StageStatus
from app.models.review_guide import ReviewGuideDraft
from app.services import project as project_service, run as run_service, workspace
from app.services.project import WorkflowFile, import_project
from app.services.project import find_projects_by_name

_FIXTURE_STEM = "tutorial_lobbying_triage"
_DATA_DIR = Path(__file__).resolve().parents[1] / "seeds" / "data"
_FIXTURE = _DATA_DIR / f"{_FIXTURE_STEM}.json"
_GUIDE = _DATA_DIR / "review_guides" / f"{_FIXTURE_STEM}.json"
# The fixture carries no path for these — a committed file cannot know where the
# workspace is — so each run says which file its input stage reads.
_CSV_BY_STAGE_ID = {
    "raw_filings": _DATA_DIR / f"{_FIXTURE_STEM}.csv",
    "public_commitments": _DATA_DIR / "tutorial_public_commitments.csv",
}
# Enough of a stage's output for the tour to find a row worth linking, and few enough
# that the whole of it can be read in the chat.
_MAX_LINKED_ROWS = 20
# Both wrote the output they promised; warnings are reported on the run's own page.
_FINISHED_STATUSES = (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)


class TutorialContext(BaseModel):
    # Ends in "/": every link the tour hands over is built from it.
    base_url: str


class TutorialProject(BaseModel):
    name: str
    version_id: str
    # Pass straight to run_workflow's `bindings`: which file each input stage reads.
    input_bindings: dict[str, dict[str, str]]
    workflow_url: str
    guide_url: str
    runs_url_prefix: str
    mcp_command: str


def seed_tutorial_project(ctx: TutorialContext) -> TutorialProject:
    name = _find_reusable_tour_project()
    # A second tour reuses what the first seeded: the workspace is not the tour's to
    # fill up, and re-importing would discard whatever the reader did to it.
    if name is None:
        for path in (_FIXTURE, _GUIDE, *_CSV_BY_STAGE_ID.values()):
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
    return TutorialProject(
        name=name,
        version_id=version_id,
        input_bindings={
            stage_id: {"path": str(path), "format": "csv"}
            for stage_id, path in _CSV_BY_STAGE_ID.items()
        },
        workflow_url=f"{ctx.base_url}project/{name}/workflow",
        guide_url=f"{ctx.base_url}project/{name}/workflow/version/{version_id}",
        runs_url_prefix=f"{ctx.base_url}project/{name}/runs/",
        mcp_command=f"claude mcp add --transport http carbonpaper {ctx.base_url}mcp",
    )


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


class RowLineageLink(BaseModel):
    ordinal: int
    values: dict[str, Any]
    lineage_url: str


class StageRowLineage(BaseModel):
    stage_id: str
    # The stage's whole output, so a reader of `rows` knows what it is a prefix of.
    row_count: int
    rows: list[RowLineageLink]


def read_row_lineage_links(
    ctx: TutorialContext, project_id: str, run_id: str, stage_id: str
) -> StageRowLineage:
    _refuse_a_stage_that_did_not_finish(project_id, run_id, stage_id)
    frame = run_service.read_stage_output(project_id, run_id, stage_id)
    return StageRowLineage(
        stage_id=stage_id,
        row_count=len(frame),
        rows=[
            RowLineageLink(
                ordinal=ordinal,
                values=_to_json_cells(row),
                lineage_url=_absolute(
                    ctx, run_service.build_row_trace_url(project_id, run_id, stage_id, ordinal)
                ),
            )
            for ordinal, row in enumerate(list_rows(frame.head(_MAX_LINKED_ROWS)))
        ],
    )


def _refuse_a_stage_that_did_not_finish(project_id: str, run_id: str, stage_id: str) -> None:
    """A stage that errored still wrote a frame: its untouched columns are nulls, not results."""
    records = run_service.read_run_status(project_id, run_id).get("stage_records", [])
    status = next(
        (record["status"] for record in records if record["stage_id"] == stage_id), None
    )
    # None: the stage is not in this run at all, which read_stage_output names better.
    if status is not None and status not in _FINISHED_STATUSES:
        raise ValueError(
            f"stage '{stage_id}' of run '{run_id}' is '{status}', so the rows it holds are "
            "not a result to show anyone — read a stage that finished"
        )


def _to_json_cells(row: dict[str, Any]) -> dict[str, Any]:
    return {name: _to_json_cell(value) for name, value in row.items()}


def _to_json_cell(value: object) -> object:
    """A null stays null: a blank cell the tour reads as blank must not arrive as "None"."""
    cell = collapse_null_forms(value)
    if cell is None or isinstance(cell, (bool, int, float, str)):
        return cell
    return convert_cell_to_json_native(cell)


def _absolute(ctx: TutorialContext, root_relative: str) -> str:
    return f"{ctx.base_url}{root_relative.lstrip('/')}"


CREATE_TUTORIAL_PROJECT = ToolSpec(
    name="create_tutorial_project",
    description=(
        "Seed the committed tutorial project into this workspace and return it: its "
        "name, the stored version, the `input_bindings` its run needs, and the URLs of "
        "its workflow, review guide and runs. Takes no arguments — the "
        "fixture is fixed. If the tutorial project is already in this workspace it is "
        "returned as it stands, not replaced, so a second tour never overwrites the first."
    ),
)

READ_ROW_LINEAGE_LINKS = ToolSpec(
    name="read_row_lineage_links",
    description=(
        f"The first {_MAX_LINKED_ROWS} rows one stage of a run produced, each carrying its "
        "`ordinal`, its cell `values`, and the `lineage_url` of that row's lineage page — "
        "a whole link, to hand over as it stands. `row_count` is the stage's entire "
        "output, so you can see what these rows are a prefix of. Read the values to "
        "choose WHICH row is worth showing: a row's ordinal is recorded nowhere else, so "
        "a lineage link you did not get here is a guess. A stage that wrote no output "
        "(it errored, or never ran) is an error here, never an empty list."
    ),
)
