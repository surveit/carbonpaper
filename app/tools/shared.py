"""Tools more than one surface offers, defined once and REFERENCED rather than rewritten.
Each closes over nothing, so the MCP server can decorate it and an agent config can wrap
it in a BoundToolSpec. A tool that must close over a session's context is not one of
these: it belongs to the agent owning that context.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from app.core.agent.bound_tool import BoundToolSpec, bind_function
from app.core.errors import (
    MissingInputBindingError,
    NoVersionToRunError,
    NoWorkflowTestVersionError,
    RunNotFoundError,
)
from app.core.frames import convert_row_to_json_cells, list_rows
from app.models.run_manifest import FINISHED_STAGE_STATUSES
from app.core.source_files import SheetSurvey
from app.core.column_profile import TableProfile
from app.models.review_guide import ReviewGuideDraft
from app.models.terms import Terms
from app.tools.types import ToolParameterProse
from app.services import (
    frame_profile,
    generation,
    project as project_service,
    run as run_service,
    stage_tests as stage_tests_service,
    terms as terms_service,
    uploads,
    workflow_test as workflow_test_service,
    workspace,
)
from app.services.errors import FileNotStoredError, WorkflowLoadError
from app.services.project import Project, ProjectListing
from app.services.versioning import ReviewGuide
from app.tools.tool_specs import TOOL_SPECS

_PROJECT_ID = "The project's name."

# read_stage_output_rows builds links, so its reader's address is the CALLER's to
# supply — never something the model is asked for.
_CALLER_SUPPLIED = frozenset({"base_url"})

# Domain failures a run tool turns into {ok: False, error: str(exc)} — a loud, honest
# verdict rather than a traceback or a fabricated run id/status. Anything outside this
# set propagates as a genuine internal fault.
RUN_TOOL_ERRORS = (
    FileNotStoredError,
    NoVersionToRunError,
    MissingInputBindingError,
    WorkflowLoadError,
    RunNotFoundError,
    NoWorkflowTestVersionError,
    ValueError,
)

# Domain failures an authoring tool turns into {ok: False, issues: [...]} — the same
# refusal channel a validation failure comes back on, which is the one the instructions
# tell a client to watch. WorkflowLoadError is a stored workflow that does not load;
# FileNotFoundError is a stage id that is not in the workflow, or a project with no
# compiled workflow to snapshot.
STAGE_TOOL_ERRORS = (WorkflowLoadError, FileNotFoundError)

# One sleep's ceiling, kept SHORT because a reader is watching the transcript: each call
# is a row on their screen, so short sleeps read as a job in progress where one long one
# reads as a hang. Waiting longer is more calls, which the caller can always make.
MAX_SLEEP_SECONDS = 3

# One call's ceiling: a window a model can read in full, and a bound on what a row-by-row
# read pulls into its context. A caller wanting more pages with `offset`.
MAX_OUTPUT_ROWS = 50


def resolve_existing_project(project_id: str) -> Path:
    """Loud on a project that is not in the workspace, rather than a later confusing miss."""
    pdir = workspace.resolve_project_dir(project_id)
    if not pdir.is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    return pdir


def create_project(name: str, document: str, *, source: str) -> Project:
    """`source` records WHICH surface authored the project, so it is the surface's to state."""
    return project_service.create_project(name, document, source=source)


def get_project_status(project_id: str) -> dict[str, Any]:
    pdir = resolve_existing_project(project_id)
    return project_service.project_state(pdir).model_dump(mode="json")


async def generate_stage_tests(project_id: str, stage_id: str) -> dict[str, Any]:
    pdir = resolve_existing_project(project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    session_id = generation.start_stage_test_generation(pdir, stage_id=stage_id, model=model)
    # Root-relative: the caller's reader is either in this app already or knows the
    # address it reached the server on, and a guessed host resolves nowhere.
    return {
        "status": "started",
        "watch": f"/chat/{session_id}",
        "poll": "get_project_status",
        "note": "read_stage to see the generated tests once done",
    }


def list_projects() -> list[ProjectListing]:
    return project_service.list_project_listings()


def read_stage(project_id: str, stage_id: str) -> str:
    return project_service.read_stage(project_id, stage_id)


def remove_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    try:
        result = project_service.remove_stage(project_id, stage_id)
    except STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}
    return {"ok": result.ok, "issues": result.issues}


def read_review_guide(project_id: str, version_id: str) -> ReviewGuide | None:
    resolve_existing_project(project_id)
    return project_service.read_review_guide(project_id, version_id)


def write_review_guide(
    project_id: str, version_id: str, guide: ReviewGuideDraft
) -> ReviewGuide:
    resolve_existing_project(project_id)
    return project_service.write_review_guide(project_id, version_id, guide)


def run_stage_tests(project_id: str, stage_id: str | None = None) -> dict[str, Any]:
    resolve_existing_project(project_id)
    return stage_tests_service.run_project_stage_tests(project_id, stage_id).model_dump(
        mode="json")


def report_compiler_warnings(project_id: str) -> dict[str, Any]:
    resolve_existing_project(project_id)
    report = stage_tests_service.find_project_compiler_warnings(project_id)
    return {"warnings": [w.model_dump(mode="json") for w in report.warnings]}


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


def list_files(project_id: str | None, file_upload_url: str) -> ProjectFilesView:
    """`project_id` None lists the files that are in no project yet."""
    if project_id is not None:
        resolve_existing_project(project_id)
    # file_upload_url is the caller's: only it knows the address it was reached on.
    used = uploads.measure_files_used_bytes()
    return ProjectFilesView(
        file_upload_url=file_upload_url,
        max_bytes=uploads.max_upload_bytes(),
        remaining_bytes=max(uploads.files_quota_bytes() - used, 0),
        files=[_view(record) for record in uploads.list_project_files(project_id)],
    )


def profile_file(
    project_id: str, sha256: str, columns: list[str] | None = None, max_values: int = 20,
    sheet_name: str | int = 0, header_row: int = 0, first_column: int = 0,
) -> TableProfile:
    resolve_existing_project(project_id)
    return frame_profile.profile_stored_file(
        project_id, sha256, columns, max_values=max_values, sheet_name=sheet_name,
        header_row=header_row, first_column=first_column)


def survey_workbook(
    project_id: str, sha256: str, from_row: int = 0,
) -> list[SheetSurvey]:
    resolve_existing_project(project_id)
    return frame_profile.survey_stored_workbook(project_id, sha256, from_row=from_row)


def profile_stage_output_data_range(
    project_id: str, run_id: str, stage_id: str, columns: list[str], max_values: int,
) -> dict[str, Any]:
    resolve_existing_project(project_id)
    try:
        profile = frame_profile.profile_stage_output(
            project_id, run_id, stage_id, columns, max_values=max_values)
    except RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **profile.model_dump()}


def _view(record: uploads.UploadedFile) -> StoredFileView:
    return StoredFileView(sha256=record.sha256, filename=record.filename,
                          bytes=record.byte_count, added=record.created_at)


def move_file_to_project(project_id: str, sha256: str) -> StoredFileView:
    """Move a file that is in no project into one. Moves no bytes."""
    resolve_existing_project(project_id)
    return _view(uploads.move_file_to_project(sha256, project_id))


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


def run_workflow_test(
    project_id: str,
    limit: int | None,
    version_id: str | None = None,
    stage_ids: list[str] | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    resolve_existing_project(project_id)
    try:
        return workflow_test_service.run_workflow_test(
            project_id, version_id=version_id, stage_ids=stage_ids,
            limit=limit, offset=offset)
    except RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    resolve_existing_project(project_id)
    return run_service.read_run_status(project_id, run_id)


async def sleep(seconds: int) -> dict[str, int]:
    """Reports what it slept, since the ask is clamped rather than refused."""
    slept = min(max(seconds, 0), MAX_SLEEP_SECONDS)
    # Async, so a caller waiting on a background thread blocks nothing but itself.
    await asyncio.sleep(slept)
    return {"slept_seconds": slept}


def read_workflow_summary(project_id: str) -> workspace.WorkflowSummary:
    resolve_existing_project(project_id)
    return project_service.read_workflow_summary(project_id)


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
                values=convert_row_to_json_cells(row),
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
    if status is not None and status not in FINISHED_STAGE_STATUSES:
        raise ValueError(
            f"stage '{stage_id}' of run '{run_id}' is '{status}', so the rows it holds are "
            "not a result to show anyone — read a stage that finished"
        )


# ── binding them onto an agent ───────────────────────────────────────────────

# create_project is absent: both surfaces WRAP it to stamp their own `source`, so
# neither binds the body. Its schema is here because both wrappers advertise it.
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "read_terms": read_terms,
    "write_terms": write_terms,
    "get_project_status": get_project_status,
    "list_projects": list_projects,
    "read_stage": read_stage,
    "remove_stage": remove_stage,
    "read_review_guide": read_review_guide,
    "write_review_guide": write_review_guide,
    "run_stage_tests": run_stage_tests,
    "report_compiler_warnings": report_compiler_warnings,
    "generate_stage_tests": generate_stage_tests,
    "run_workflow": run_workflow,
    "run_workflow_test": run_workflow_test,
    "get_run_status": get_run_status,
    "sleep": sleep,
    "read_workflow_summary": read_workflow_summary,
    "read_stage_output_rows": read_stage_output_rows,
    "profile_stage_output_data_range": profile_stage_output_data_range,
    "move_file_to_project": move_file_to_project,
    "profile_file": profile_file,
    "survey_workbook": survey_workbook,
}

_SCHEMAS: dict[str, ToolParameterProse] = {
    "create_project": {
        "name": "What to CALL the project — a label, shown to the human. Two projects may "
            "share one; the id you work with comes back from this call.",
        "document": "The methodology prose, whole. It becomes the project's source of record, "
            "which every later generation reads — so send what the user wrote, never a "
            "summary of it.",
    },
    "read_terms": {"project_id": _PROJECT_ID},
    "write_terms": {
        "project_id": _PROJECT_ID,
        "terms": "The WHOLE vocabulary — `nouns` and `verbs` both, every time. What you send "
            "replaces what is stored, so read_terms first and send that back with your "
            "additions.",
    },
    "get_project_status": {"project_id": _PROJECT_ID},
    "list_projects": {},
    "read_stage": {
        "project_id": _PROJECT_ID,
        "stage_id": "The stage's id, as read_workflow_summary shows it.",
    },
    "remove_stage": {
        "project_id": _PROJECT_ID,
        "stage_id": "The stage to delete. Refused if another stage still lists it in its inputs.",
    },
    "read_review_guide": {
        "project_id": _PROJECT_ID,
        "version_id": "The version whose guide to read.",
    },
    "write_review_guide": {
        "project_id": _PROJECT_ID,
        "version_id": "The version this guide describes. The guide is validated against THAT "
            "version's stages.",
        "guide": "The complete guide: `steps`, each with `title`, `prose` and `stage_ids`, "
            "plus `unnarrated`. Sent whole every time — it replaces any earlier guide.",
    },
    "run_stage_tests": {
        "project_id": _PROJECT_ID,
        "stage_id": "One stage to scope the run to. Omit to run every stage with tests.",
    },
    "report_compiler_warnings": {"project_id": _PROJECT_ID},
    "generate_stage_tests": {
        "project_id": _PROJECT_ID,
        "stage_id": "The stage to generate tests for.",
    },
    "run_workflow": {
        "project_id": _PROJECT_ID,
        "version_id": "Omit for the project's newest stored version.",
        "limits": 'Caps how many rows a stage READS: {"<stage id>": N}.',
        "files": 'The stored file each input stage reads for THIS run: '
            '{"<stage id>": "<sha256 from list_files>"}.',
    },
    "run_workflow_test": {
        "project_id": _PROJECT_ID,
        "limit": "How many rows of the bound source to run on — the run's budget, since every "
            "LLM stage pays per row. null runs the whole source.",
        "version_id": "Omit for the project's newest stored version.",
        "stage_ids": "Which stages to execute. Omit to run every stage that is not an input.",
        "offset": "The source row the window starts at. 0 is the first.",
    },
    "get_run_status": {
        "project_id": _PROJECT_ID,
        "run_id": "The run id run_workflow returned.",
    },
    "sleep": {
        "seconds": f"How long to sleep. Clamped to {MAX_SLEEP_SECONDS} — sleep again to wait longer.",
    },
    "read_workflow_summary": {"project_id": _PROJECT_ID},
    "read_stage_output_rows": {
        "project_id": _PROJECT_ID,
        "run_id": "The run whose stored output you want to read.",
        "stage_id": "The stage whose output rows you want.",
        "limit": f"How many rows to read, from `offset`. Clamped to {MAX_OUTPUT_ROWS}, which "
            f"is also the default.",
        "offset": "The row ordinal to start at. 0 is the first row.",
    },
    "profile_stage_output_data_range": {
        "project_id": _PROJECT_ID,
        "run_id": "The run whose stored output you want to profile.",
        "stage_id": "The stage whose output columns you want.",
        "columns": "The columns to profile — every one you are about to declare.",
        "max_values": "How many distinct values to show per column, commonest first. `truncated` "
            "says whether there were more.",
    },
    "move_file_to_project": {
        "project_id": _PROJECT_ID,
        "sha256": "The stored file's sha256, as list_files reported it.",
    },
    "profile_file": {
        "project_id": _PROJECT_ID,
        "sha256": "The stored file's sha256, as list_files reported it.",
        "columns": "Which columns to profile. Omit for every column in the file.",
        "max_values": "How many distinct values to show per column, commonest first. "
            "`truncated` says whether there were more.",
        "sheet_name": "xlsx only: the sheet, by name or 0-based position.",
        "header_row": "xlsx only: the 0-based row the header sits on.",
        "first_column": "xlsx only: the 0-based column the table starts at.",
    },
    "survey_workbook": {
        "project_id": _PROJECT_ID,
        "sha256": "The stored xlsx's sha256, as list_files reported it.",
        "from_row": "The 0-based sheet row the window starts at. Raise it to look past a "
                 "preamble longer than the window.",
    },
}

_LABELS = {
    "create_project": "Creating the project",
    "read_terms": "Reading the project's words",
    "write_terms": "Storing the project's words",
    "get_project_status": "Checking the project",
    "list_projects": "Listing projects",
    "read_stage": "Reading a stage",
    "remove_stage": "Removing a stage",
    "read_review_guide": "Reading the review guide",
    "write_review_guide": "Writing the review guide",
    "run_stage_tests": "Running the stage's tests",
    "report_compiler_warnings": "Reading the workflow's warnings",
    "generate_stage_tests": "Generating the stage's tests",
    "run_workflow": "Running the workflow",
    "run_workflow_test": "Testing the workflow on real rows",
    "get_run_status": "Checking the run",
    "sleep": "Waiting",
    "read_workflow_summary": "Reading the workflow",
    "read_stage_output_rows": "Reading the stage's rows",
    "profile_stage_output_data_range": "Reading what the stage's columns hold",
    "move_file_to_project": "Putting the file in the project",
    "profile_file": "Reading what the file holds",
    "survey_workbook": "Looking over the workbook's sheets",
}


def read_parameter_prose(name: str) -> ToolParameterProse:
    """For a surface that WRAPS a shared tool instead of binding it."""
    return _SCHEMAS[name]


def bind(*names: str) -> list[BoundToolSpec]:
    """The named shared tools as BoundToolSpecs — an agent config lists names, not bodies."""
    return [
        bind_function(
            name=name,
            description=TOOL_SPECS[name].description,
            fn=_FUNCTIONS[name],
            label=_LABELS[name],
            parameters=_SCHEMAS[name],
            skip=_CALLER_SUPPLIED,
        )
        for name in names
    ]
