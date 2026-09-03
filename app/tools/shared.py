"""The tool bodies that close over nothing, so any surface can offer one: the MCP server
decorates it, an agent config binds it by name through app.tools.tool_specs. A tool that
must close over a session's context is not here — it belongs to the agent owning that
context, and app.tools.tool_specs holds nothing of it but the description.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from app.core.errors import (
    FileNotStoredError,
    MissingInputBindingError,
    NoVersionToRunError,
    NoWorkflowTestVersionError,
    RunNotFoundError,
)
from app.core.frames import convert_row_to_json_cells, list_rows
from app.core import files as file_store
from app.models.run_manifest import FINISHED_STAGE_STATUSES, UNREADABLE_RUN_STATUS
from app.core.source_files import SheetSurvey
from app.core.column_profile import TableProfile
from app.models.review_guide import ReviewGuideDraft
from app.models.terms import Terms
from app.services import (
    code_approval,
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
from app.services.errors import WorkflowLoadError
from app.services.project import ProjectListing
from app.models.records.project import Project
from app.models.records.review_guide import ReviewGuide

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
# A history a model can read in full. Older runs than this are reachable only from
# the runs page, so the listing says how many it did not name.
MAX_RUNS_LISTED = 20


def validate_project_exists(project_id: str) -> str:
    """Returns the id it validated, so a caller passes it straight on."""
    workspace.validate_project_id(project_id)  # invalid is a different answer to absent
    if not project_service.project_exists(project_id):
        raise ValueError(f"no project '{project_id}' in the workspace")
    return project_id


def create_project(name: str, document: str, *, source: str) -> Project:
    """`source` records WHICH surface authored the project, so it is the surface's to state."""
    return project_service.create_project(name, document, source=source)


def get_project_status(project_id: str) -> dict[str, Any]:
    validate_project_exists(project_id)
    return project_service.project_state(project_id).model_dump(mode="json")


async def generate_stage_tests(project_id: str, stage_id: str) -> dict[str, Any]:
    validate_project_exists(project_id)
    model = project_service.project_meta(project_id).model or "sonnet"
    session_id = generation.start_stage_test_generation(
        project_id, stage_id=stage_id, model=model)
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


def delete_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    try:
        result = project_service.delete_stage(project_id, stage_id)
    except STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}
    return {"ok": result.ok, "issues": result.issues}


def read_review_guide(project_id: str, version_id: str) -> ReviewGuide | None:
    validate_project_exists(project_id)
    return project_service.read_review_guide(project_id, version_id)


def write_review_guide(
    project_id: str, version_id: str, guide: ReviewGuideDraft
) -> ReviewGuide:
    validate_project_exists(project_id)
    return project_service.write_review_guide(project_id, version_id, guide)


def run_stage_tests(project_id: str, stage_id: str | None = None) -> dict[str, Any]:
    validate_project_exists(project_id)
    return stage_tests_service.run_project_stage_tests(project_id, stage_id).model_dump(
        mode="json")


def report_compiler_warnings(project_id: str) -> dict[str, Any]:
    validate_project_exists(project_id)
    report = stage_tests_service.find_project_compiler_warnings(project_id)
    return {"warnings": [w.model_dump(mode="json") for w in report.warnings]}


def approve_code_execution(project_id: str, reason: str) -> str:
    """Records an answer the OWNER gave. Calling it without one is the misuse to avoid."""
    validate_project_exists(project_id)
    record = code_approval.approve_code_execution(project_id, reason)
    return (
        f"Code execution is on for this project, approved {record.approved_at}. "
        f"python_frame_function stages can now be written. It stays on until someone "
        f"turns it off on the project page — say so, so the owner knows it is standing."
    )


def read_terms(project_id: str) -> Terms:
    validate_project_exists(project_id)
    return terms_service.load_terms(project_id)


def write_terms(project_id: str, terms: Terms) -> Terms:
    validate_project_exists(project_id)
    terms_service.write_terms(project_id, terms)
    # Read back rather than echoed: what the project now says, not what was sent.
    return terms_service.load_terms(project_id)


# `edited` is empty whenever `ok` is false: the batch is written whole or not at all.
class EditedStages(BaseModel):
    ok: bool
    edited: list[str]
    issues: list[str]
    warnings: list[str] = []


class StoredFileView(BaseModel):
    """One file a project holds; `file_id` is what names it to run_workflow."""

    file_id: str
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
        validate_project_exists(project_id)
    # file_upload_url is the caller's: only it knows the address it was reached on.
    used = file_store.measure_files_used_bytes()
    return ProjectFilesView(
        file_upload_url=file_upload_url,
        max_bytes=file_store.max_upload_bytes(),
        remaining_bytes=max(file_store.files_quota_bytes() - used, 0),
        files=[_build_stored_file_view(record) for record in file_store.list_project_files(project_id)],
    )


def profile_file(
    project_id: str, file_id: str, columns: list[str] | None = None, max_values: int = 20,
    sheet_name: str | int = 0, header_row: int = 0, first_column: int = 0,
) -> TableProfile:
    validate_project_exists(project_id)
    return frame_profile.profile_stored_file(
        project_id, file_id, columns, max_values=max_values, sheet_name=sheet_name,
        header_row=header_row, first_column=first_column)


def survey_workbook(
    project_id: str, file_id: str, from_row: int = 0,
) -> list[SheetSurvey]:
    validate_project_exists(project_id)
    return frame_profile.survey_stored_workbook(project_id, file_id, from_row=from_row)


def profile_stage_output_data_range(
    project_id: str, run_id: str, stage_id: str, columns: list[str], max_values: int,
) -> dict[str, Any]:
    validate_project_exists(project_id)
    try:
        profile = frame_profile.profile_stage_output(
            project_id, run_id, stage_id, columns, max_values=max_values)
    except RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **profile.model_dump()}


def _build_stored_file_view(record: file_store.ProjectFile) -> StoredFileView:
    return StoredFileView(file_id=record.id, filename=record.filename,
                          bytes=record.byte_count, added=record.created_at)


def move_file_to_project(project_id: str, file_id: str) -> StoredFileView:
    """Move a file that is in no project into one. Moves no bytes."""
    validate_project_exists(project_id)
    return _build_stored_file_view(file_store.move_file_to_project(file_id, project_id))


def run_workflow(
    project_id: str,
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    files: dict[str, str | list[str]] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    validate_project_exists(project_id)
    # Resolved before start_run so an unknown file id fails naming itself.
    bindings = {
        stage_id: uploads.resolve_files_binding(
            project_id, file_ids if isinstance(file_ids, list) else [file_ids])
        for stage_id, file_ids in (files or {}).items()
    }
    run_id = run_service.start_run(
        project_id, version_id=version_id or None, limits=limits,
        bindings=bindings or None, bust_cache=bust_cache,
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
    validate_project_exists(project_id)
    try:
        return workflow_test_service.run_workflow_test(
            project_id, version_id=version_id, stage_ids=stage_ids,
            limit=limit, offset=offset)
    except RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    validate_project_exists(project_id)
    return run_service.read_run_status_without_tracebacks(project_id, run_id)


class RunListing(BaseModel):
    run_id: str
    # The stored word, which is what get_run_status reports for the same run.
    status: str
    started_at: str | None = None
    workflow_version: str | None = None
    is_test_run: bool = False


class RunHistory(BaseModel):
    # Every run this project recorded, so a cut window is read as the cut it is.
    run_count: int
    limit: int
    runs: list[RunListing]


def list_runs(project_id: str, limit: int = MAX_RUNS_LISTED) -> RunHistory:
    validate_project_exists(project_id)
    entries = list(reversed(run_service.list_run_entries(project_id)))
    kept = min(max(limit, 1), MAX_RUNS_LISTED)
    return RunHistory(
        run_count=len(entries),
        limit=kept,
        runs=[_describe_run(entry) for entry in entries[:kept]],
    )


def _describe_run(entry: run_service.RunEntry) -> RunListing:
    manifest = entry.manifest
    if manifest is None:
        # Its id alone, so one unreadable record does not take the history down with it.
        return RunListing(run_id=entry.run_id, status=UNREADABLE_RUN_STATUS)
    return RunListing(
        run_id=entry.run_id,
        status=str(manifest.status),
        started_at=manifest.started_at,
        workflow_version=manifest.workflow_version,
        is_test_run=manifest.parameters.is_test_run,
    )


async def sleep(seconds: int) -> dict[str, int]:
    """Reports what it slept, since the ask is clamped rather than refused."""
    slept = min(max(seconds, 0), MAX_SLEEP_SECONDS)
    # Async, so a caller waiting on a background thread blocks nothing but itself.
    await asyncio.sleep(slept)
    return {"slept_seconds": slept}


def read_workflow_summary(project_id: str) -> workspace.WorkflowSummary:
    validate_project_exists(project_id)
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
    validate_project_exists(project_id)
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
