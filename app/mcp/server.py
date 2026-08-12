"""The Carbon Paper FastMCP server: authoring tools over app.services.

Tools resolve project directories only through workspace.resolve_project_dir, which refuses
names escaping the workspace. Generation tools start LIVE chat turns on the server event
loop and return immediately; callers poll get_project_status. Failures raise, never fake success."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from app.core.errors import (
    MissingInputBindingError,
    NoVersionToRunError,
    NoWorkflowTestVersionError,
    RunNotFoundError,
)
from app.models import find_workflow_compiler_warnings
from app.models.terms import Terms
from app.tools import shared
from app.tools.submitted_stage import (
    SubmittedStage,
    add_stages_reporting_drops,
    edit_stage_reporting_drops,
)
from app.tools.tool_specs import SAVE_VERSION_FROM_WORKING_COPY, TOOL_SPECS
from app.mcp.instructions import INSTRUCTIONS
from app.models.review_guide import ReviewGuideDraft
from app.services.versioning import ReviewGuide
from app.runtime import stage_tests
from app.services import generation
from app.services import loader
from app.services import frame_profile
from app.services import project as project_service
from app.services.project import Project, ProjectListing
from app.services import terms as terms_service
from app.services import versioning
from app.services import workflow_test as workflow_test_service
from app.services.errors import FileNotStoredError, WorkflowLoadError
from app.services.stage_edit import EditStageResult

# Domain failures a run/workflow-test tool turns into {ok: False, error: str(exc)} — a
# loud, honest verdict rather than a traceback or a fabricated run id/status.
# Anything outside this set propagates as a genuine internal fault.
_RUN_TOOL_ERRORS = (
    FileNotStoredError,
    NoVersionToRunError,
    MissingInputBindingError,
    WorkflowLoadError,
    RunNotFoundError,
    NoWorkflowTestVersionError,
    ValueError,
)

# Domain failures an authoring tool turns into {ok: False, issues: [...]} — the
# same refusal channel a validation failure comes back on, which is the one these
# instructions tell a client to watch. WorkflowLoadError is a stored workflow that
# does not load; FileNotFoundError is a stage id that is not in the workflow, or a
# project with no compiled workflow to snapshot.
# Anything outside this set propagates as a genuine internal fault.
_STAGE_TOOL_ERRORS = (WorkflowLoadError, FileNotFoundError)


mcp = FastMCP(
    name="carbon_paper",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
)


# ─── Session-manager lifecycle (re-entrant) ──────────────────────────────────
# The SDK caches ONE StreamableHTTPSessionManager on the FastMCP instance, and
# its run() is once-per-instance — so an app lifespan built on it can only ever
# be entered once per process (a second TestClient(app) would RuntimeError).
# Instead, run_session_manager() builds a FRESH manager per lifespan entry, and
# the /mcp endpoint delegates to whichever manager the current lifespan owns.

_active_manager: StreamableHTTPSessionManager | None = None


class _StreamableHTTPEndpoint:
    """A class instance, not a function, so Starlette's Route treats it as an ASGI app."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        manager = _active_manager
        if manager is None:
            raise RuntimeError(
                "/mcp requested outside the app lifespan — the MCP session "
                "manager only runs while the server is up."
            )
        await manager.handle_request(scope, receive, send)


handle_streamable_http = _StreamableHTTPEndpoint()


@asynccontextmanager
async def run_session_manager() -> AsyncIterator[None]:
    """`mcp._mcp_server` is private — the SDK has no public accessor; pyproject pins the mcp version."""
    global _active_manager
    manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,
        json_response=mcp.settings.json_response,
        stateless=mcp.settings.stateless_http,
        security_settings=mcp.settings.transport_security,
    )
    async with manager.run():
        _active_manager = manager
        try:
            yield
        finally:
            _active_manager = None


@mcp.tool(description=TOOL_SPECS["list_projects"].description)
def list_projects() -> list[ProjectListing]:
    return project_service.list_project_listings()


@mcp.tool(description=TOOL_SPECS["create_project"].description)
def create_project(name: str, document: str) -> Project:
    return shared.create_project(name, document, source="mcp")


@mcp.tool(description=TOOL_SPECS["get_project_status"].description)
def get_project_status(project_id: str) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    return project_service.project_state(pdir).model_dump(mode="json")


@mcp.tool(description=TOOL_SPECS["generate_data_model"].description)
async def generate_data_model(project_id: str) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    document = _read_document(pdir, project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    session_id = generation.start_generation(pdir, document=document, model=model)
    return {"status": "started", "watch": f"/chat/{session_id}", "poll": "get_project_status"}


@mcp.tool(description=TOOL_SPECS["generate_stage_tests"].description)
async def generate_stage_tests(project_id: str, stage_id: str) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    session_id = generation.start_stage_test_generation(pdir, stage_id=stage_id, model=model)
    return {
        "status": "started",
        "watch": f"/chat/{session_id}",
        "poll": "get_project_status",
        "note": "read_stage to see the generated tests once done",
    }


@mcp.tool(description=TOOL_SPECS["run_stage_tests"].description)
def run_stage_tests(project_id: str, stage_id: str | None = None) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    stages = loader.load_workflow(pdir)
    report: stage_tests.StageTestsReport = stage_tests.run_stage_tests(stages, stage_id)
    return report.model_dump(mode="json")


@mcp.tool(description=TOOL_SPECS["report_compiler_warnings"].description)
def report_compiler_warnings(project_id: str) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    stages = loader.load_workflow(pdir)
    failing = stage_tests.run_stage_tests(stages).count_failing_by_stage()
    report = find_workflow_compiler_warnings(stages, failing)
    return {"warnings": [w.model_dump(mode="json") for w in report.warnings]}


@mcp.tool(description=TOOL_SPECS["read_data_model"].description)
def read_data_model(project_id: str) -> list[dict[str, Any]]:
    _resolve_existing_project(project_id)
    return [noun.model_dump(mode="json", exclude_none=True)
            for noun in terms_service.load_terms(project_id).nouns.schemas]


@mcp.tool(description=TOOL_SPECS["read_terms"].description)
def read_terms(project_id: str) -> Terms:
    return shared.read_terms(project_id)


@mcp.tool(description=TOOL_SPECS["write_terms"].description)
def write_terms(project_id: str, terms: Terms) -> Terms:
    return shared.write_terms(project_id, terms)


@mcp.tool(description=TOOL_SPECS["describe_workflow"].description)
def describe_workflow(project_id: str) -> shared.workspace.WorkflowSummary:
    return shared.describe_workflow(project_id)


@mcp.tool(description=TOOL_SPECS["read_stage"].description)
def read_stage(project_id: str, stage_id: str) -> str:
    return project_service.read_stage(project_id, stage_id)


@mcp.tool(description=TOOL_SPECS["edit_stage"].description)
def edit_stage(project_id: str, stage_id: str, changes_json: str) -> dict[str, Any]:
    return catch_stage_edit_refusals(
        lambda: edit_stage_reporting_drops(project_id, stage_id, changes_json)
    )


@mcp.tool(description=TOOL_SPECS["add_stage"].description)
def add_stage(project_id: str, stages: list[SubmittedStage]) -> dict[str, Any]:
    return add_stages_reporting_drops(project_id, stages)


@mcp.tool(description=TOOL_SPECS["remove_stage"].description)
def remove_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    return catch_stage_edit_refusals(
        lambda: _as_tool_result(project_service.remove_stage(project_id, stage_id))
    )


def catch_stage_edit_refusals(edit: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return edit()
    except _STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}


def _as_tool_result(result: EditStageResult) -> dict[str, Any]:
    return {"ok": result.ok, "issues": result.issues}


@mcp.tool(description=SAVE_VERSION_FROM_WORKING_COPY.description)
def save_version(
    project_id: str, message: str, parent_version: str | None = None
) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    try:
        if parent_version is not None:
            versioning.validate_version_exists(pdir, parent_version)
        version = project_service.save_working_copy_as_version(
            pdir, message=message, reviewer="agent", parent_version=parent_version
        )
    except _STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}
    return {"ok": True, "issues": [], "version_id": version.version_id}


@mcp.tool(description=TOOL_SPECS["read_review_guide"].description)
def read_review_guide(project_id: str, version_id: str) -> ReviewGuide | None:
    _resolve_existing_project(project_id)
    return project_service.read_review_guide(project_id, version_id)


@mcp.tool(description=TOOL_SPECS["write_review_guide"].description)
def write_review_guide(
    project_id: str, version_id: str, guide: ReviewGuideDraft
) -> ReviewGuide:
    _resolve_existing_project(project_id)
    return project_service.write_review_guide(project_id, version_id, guide)


@mcp.tool(description=TOOL_SPECS["run_workflow"].description)
def run_workflow(
    project_id: str,
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    files: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        return shared.run_workflow(project_id, version_id, limits, files)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(description=TOOL_SPECS["list_files"].description)
def list_files(project_id: str) -> dict[str, Any]:
    try:
        view = shared.list_files(project_id, _resolve_file_upload_url(project_id))
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return view.model_dump()


def _resolve_file_upload_url(project_id: str) -> str:
    """The address THIS call arrived on, so the link handed back is one that resolves."""
    request = mcp.get_context().request_context.request
    # Refusing beats composing a plausible URL from a configured guess: a link that
    # resolves nowhere costs the person it is handed to more than an error costs us.
    if request is None:
        raise ValueError(
            "list_files cannot tell what address this server is reached on — no HTTP "
            "request is attached to this tool call, so there is no upload URL to give")
    # X-Forwarded-Proto ahead of the scheme uvicorn reports: uvicorn honours forwarded
    # headers only from `forwarded_allow_ips` (127.0.0.1 by default), and a proxy in
    # front of this app is not on that list — so an https request reads back as http.
    # The header is trusted for this one string, not for the app's own request handling.
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/project/{project_id}/files"


@mcp.tool(description=TOOL_SPECS["get_run_status"].description)
def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    try:
        return shared.get_run_status(project_id, run_id)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(description=TOOL_SPECS["run_workflow_test"].description)
def run_workflow_test(
    project_id: str,
    limit: int | None,
    version_id: str | None = None,
    stage_ids: list[str] | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    _resolve_existing_project(project_id)  # loud if the project doesn't exist
    try:
        return workflow_test_service.run_workflow_test(
            project_id, version_id=version_id, stage_ids=stage_ids,
            limit=limit, offset=offset)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(description=TOOL_SPECS["read_stage_output_rows"].description)
def read_stage_output_rows(
    project_id: str,
    run_id: str,
    stage_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> shared.StageOutputRows:
    return shared.read_stage_output_rows(project_id, run_id, stage_id, limit, offset)


@mcp.tool(description=TOOL_SPECS["profile_stage_output_data_range"].description)
def profile_stage_output_data_range(
    project_id: str,
    run_id: str,
    stage_id: str,
    columns: list[str],
    max_values: int,
) -> dict[str, Any]:
    _resolve_existing_project(project_id)  # loud if the project doesn't exist
    try:
        profile = frame_profile.profile_stage_output(
            project_id, run_id, stage_id, columns, max_values=max_values)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **profile.model_dump()}


def _resolve_existing_project(project_id: str) -> Path:
    return shared.resolve_existing_project(project_id)


def _read_document(pdir: Path, project_id: str) -> str:
    doc_path = pdir / "document.md"
    if not doc_path.is_file():
        raise ValueError(f"project '{project_id}' has no document.md to generate from")
    return doc_path.read_text(encoding="utf-8")
