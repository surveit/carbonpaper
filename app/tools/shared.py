"""Tools more than one surface offers, defined once and REFERENCED rather than rewritten.
Each closes over nothing, so the MCP server can decorate it and an agent config can wrap
it in a BoundToolSpec. A tool that must close over a session's context is not one of
these: it belongs to the agent owning that context.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Annotated, Callable

from pydantic import BaseModel

from app.core.agent.bound_tool import BoundToolSpec
from app.core.frames import collapse_null_forms, convert_cell_to_json_native, list_rows
from app.core.run_status import StageStatus
from app.models.terms import Terms
from app.tools.types import ToolInputSchema
from app.services import (
    project as project_service,
    run as run_service,
    terms as terms_service,
    uploads,
    workspace,
)
from app.services.project import Project
from app.tools.tool_specs import TOOL_SPECS

_PROJECT_ID = Annotated[str, "The project's name."]

# One sleep's ceiling, kept SHORT because a reader is watching the transcript: each call
# is a row on their screen, so short sleeps read as a job in progress where one long one
# reads as a hang. Waiting longer is more calls, which the caller can always make.
MAX_SLEEP_SECONDS = 3

# One call's ceiling: a window a model can read in full, and a bound on what a row-by-row
# read pulls into its context. A caller wanting more pages with `offset`.
MAX_OUTPUT_ROWS = 50
# Both wrote the output they promised; warnings are reported on the run's own page.
_FINISHED_STATUSES = (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)


def resolve_existing_project(project_id: str) -> Path:
    """Loud on a project that is not in the workspace, rather than a later confusing miss."""
    pdir = workspace.resolve_project_dir(project_id)
    if not pdir.is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    return pdir


def create_project(name: str, document: str, *, source: str) -> Project:
    """`source` records WHICH surface authored the project, so it is the surface's to state."""
    return project_service.create_project(name, document, source=source)


def read_terms(project_id: str) -> Terms:
    resolve_existing_project(project_id)
    return terms_service.load_terms(project_id)


def write_terms(project_id: str, terms: Terms) -> Terms:
    resolve_existing_project(project_id)
    terms_service.write_terms(project_id, terms)
    # Read back rather than echoed: what the project now says, not what was sent.
    return terms_service.load_terms(project_id)


class StoredFileView(BaseModel):
    """One file a project holds; `sha256` is what names it to run_workflow."""

    sha256: str
    filename: str
    bytes: int
    added: str


class ProjectFilesView(BaseModel):
    """What a project holds and how to add to it."""

    file_upload_url: str
    max_bytes: int
    remaining_bytes: int
    files: list[StoredFileView]


def list_files(project_id: str, file_upload_url: str) -> ProjectFilesView:
    """`file_upload_url` is the caller's — only it knows the address it was reached on."""
    resolve_existing_project(project_id)
    used = uploads.measure_files_used_bytes()
    return ProjectFilesView(
        file_upload_url=file_upload_url,
        max_bytes=uploads.max_upload_bytes(),
        remaining_bytes=max(uploads.files_quota_bytes() - used, 0),
        files=[StoredFileView(sha256=f.sha256, filename=f.filename,
                              bytes=f.byte_count, added=f.created_at)
               for f in uploads.list_project_files(project_id)],
    )


def run_workflow(
    project_id: str,
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    files: dict[str, str] | None = None,
) -> dict[str, Any]:
    resolve_existing_project(project_id)
    # A stage id -> sha256 map, resolved here to the path-and-format params a run
    # binds. Resolving it before start_run means an unknown file id fails naming
    # itself, rather than as a missing-input refusal from preflight.
    bindings = {stage_id: uploads.resolve_file_binding(project_id, sha256)
                for stage_id, sha256 in (files or {}).items()}
    run_id = run_service.start_run(
        project_id, version_id=version_id or None, limits=limits,
        bindings=bindings or None
    )
    status = run_service.read_run_status(project_id, run_id)["status"]
    return {"run_id": run_id, "status": status}


def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    resolve_existing_project(project_id)
    return run_service.read_run_status(project_id, run_id)


async def sleep(seconds: int) -> dict[str, int]:
    """Reports what it slept, since the ask is clamped rather than refused."""
    slept = min(max(seconds, 0), MAX_SLEEP_SECONDS)
    # Async, so a caller waiting on a background thread blocks nothing but itself.
    await asyncio.sleep(slept)
    return {"slept_seconds": slept}


def describe_workflow(project_id: str) -> workspace.WorkflowSummary:
    resolve_existing_project(project_id)
    return project_service.describe_workflow(project_id)


class StageOutputRow(BaseModel):
    ordinal: int
    values: dict[str, Any]
    lineage_url: str


class StageOutputRows(BaseModel):
    stage_id: str
    # The stage's whole output, so a window is read as the window it is.
    row_count: int
    offset: int
    # What the cap allowed, which is not what the caller asked for when it asked for more.
    limit: int
    rows: list[StageOutputRow]


def read_stage_output_rows(
    project_id: str,
    run_id: str,
    stage_id: str,
    limit: int | None = None,
    offset: int = 0,
    *,
    base_url: str = "",
) -> StageOutputRows:
    """`base_url` is for a caller whose reader clicks the link; without it they are root-relative."""
    resolve_existing_project(project_id)
    _refuse_a_stage_that_did_not_finish(project_id, run_id, stage_id)
    window = min(MAX_OUTPUT_ROWS if limit is None else limit, MAX_OUTPUT_ROWS)
    if window < 1 or offset < 0:
        raise ValueError(f"limit must be at least 1 and offset at least 0, got {limit}, {offset}")
    frame = run_service.read_stage_output(project_id, run_id, stage_id)
    return StageOutputRows(
        stage_id=stage_id,
        row_count=len(frame),
        offset=offset,
        limit=window,
        rows=[
            StageOutputRow(
                ordinal=offset + position,
                values=_to_json_cells(row),
                lineage_url=base_url + run_service.build_row_trace_url(
                    project_id, run_id, stage_id, offset + position
                ),
            )
            for position, row in enumerate(list_rows(frame.iloc[offset:offset + window]))
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
    """A null stays null: a blank cell a reader reads as blank must not arrive as "None"."""
    cell = collapse_null_forms(value)
    if cell is None or isinstance(cell, (bool, int, float, str)):
        return cell
    return convert_cell_to_json_native(cell)


# ── binding them onto an agent ───────────────────────────────────────────────

# create_project is absent: both surfaces WRAP it to stamp their own `source`, so
# neither binds the body. Its schema is here because both wrappers advertise it.
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "read_terms": read_terms,
    "write_terms": write_terms,
    "run_workflow": run_workflow,
    "get_run_status": get_run_status,
    "sleep": sleep,
    "describe_workflow": describe_workflow,
    "read_stage_output_rows": read_stage_output_rows,
}

_SCHEMAS: dict[str, ToolInputSchema] = {
    "create_project": {
        "name": Annotated[
            str,
            "What to CALL the project — a label, shown to the human. Two projects may "
            "share one; the id you work with comes back from this call.",
        ],
        "document": Annotated[
            str,
            "The methodology prose, whole. It becomes the project's source of record, "
            "which every later generation reads — so send what the user wrote, never a "
            "summary of it.",
        ],
    },
    "read_terms": {"project_id": _PROJECT_ID},
    "write_terms": {
        "project_id": _PROJECT_ID,
        "terms": Annotated[
            Terms,
            "The WHOLE vocabulary — `nouns` and `verbs` both, every time. What you send "
            "replaces what is stored, so read_terms first and send that back with your "
            "additions.",
        ],
    },
    "run_workflow": {
        "project_id": _PROJECT_ID,
        "version_id": Annotated[str, "Omit for the project's newest stored version."],
        "limits": Annotated[
            dict[str, int] | None,
            'Caps how many rows a stage READS: {"<stage id>": N}.',
        ],
        "files": Annotated[
            dict[str, str] | None,
            'The stored file each input stage reads for THIS run: '
            '{"<stage id>": "<sha256 from list_files>"}.',
        ],
    },
    "get_run_status": {
        "project_id": _PROJECT_ID,
        "run_id": Annotated[str, "The run id run_workflow returned."],
    },
    "sleep": {
        "seconds": Annotated[
            int,
            f"How long to sleep. Clamped to {MAX_SLEEP_SECONDS} — sleep again to wait longer.",
        ],
    },
    "describe_workflow": {"project_id": _PROJECT_ID},
    "read_stage_output_rows": {
        "project_id": _PROJECT_ID,
        "run_id": Annotated[str, "The run whose stored output you want to read."],
        "stage_id": Annotated[str, "The stage whose output rows you want."],
        "limit": Annotated[
            int | None,
            f"How many rows to read, from `offset`. Clamped to {MAX_OUTPUT_ROWS}, which "
            f"is also the default.",
        ],
        "offset": Annotated[int, "The row ordinal to start at. 0 is the first row."],
    },
}

_LABELS = {
    "create_project": "Creating the project",
    "read_terms": "Reading the project's words",
    "write_terms": "Storing the project's words",
    "run_workflow": "Running the workflow",
    "get_run_status": "Checking the run",
    "sleep": "Waiting",
    "describe_workflow": "Reading the workflow",
    "read_stage_output_rows": "Reading the stage's rows",
}


def schema_of(name: str) -> ToolInputSchema:
    """For a surface that WRAPS a shared tool instead of binding it."""
    return _SCHEMAS[name]


def bind(*names: str) -> list[BoundToolSpec]:
    """The named shared tools as BoundToolSpecs — an agent config lists names, not bodies."""
    return [
        BoundToolSpec(
            name=name,
            description=TOOL_SPECS[name].description,
            fn=_FUNCTIONS[name],
            input_schema=_SCHEMAS[name],
            label=_LABELS[name],
        )
        for name in names
    ]
