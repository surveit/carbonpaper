"""The Carbon Paper FastMCP server: JSON-RPC transport over the tools in app.tools.

Every body, input schema and description belongs to app.tools, so a tool cannot exist here
and nowhere else — an import-linter contract holds it. What is left in a function here is
the wire signature, which IS the schema a client reads."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from app.mcp.instructions import INSTRUCTIONS
from app.models.claims import ClaimShapeInput
from app.models.records.claims import ClaimShape
from app.tools import claim_shapes as claim_shape_tools, shared, working_copy
from app.models.stage import StageEdit
from app.tools.submitted_stage import (
    SubmittedStage,
    add_stages_reporting_drops,
    edit_stages_reporting_drops,
)
from app.tools.tool_specs import read_tool_description

_RUN_TOOL_ERRORS = shared.RUN_TOOL_ERRORS


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


@mcp.tool(description=read_tool_description("list_projects"))
def list_projects() -> list[shared.ProjectListing]:
    return shared.list_projects()


@mcp.tool(description=read_tool_description("create_project"))
def create_project(name: str, document: str) -> shared.Project:
    return shared.create_project(name, document, source="mcp")


@mcp.tool(description=read_tool_description("get_project_status"))
def get_project_status(project_id: str) -> dict[str, Any]:
    return shared.get_project_status(project_id)


@mcp.tool(description=read_tool_description("generate_stage_tests"))
async def generate_stage_tests(project_id: str, stage_id: str) -> dict[str, Any]:
    return await shared.generate_stage_tests(project_id, stage_id)


@mcp.tool(description=read_tool_description("run_stage_tests"))
def run_stage_tests(project_id: str, stage_id: str | None = None) -> dict[str, Any]:
    return shared.run_stage_tests(project_id, stage_id)


@mcp.tool(description=read_tool_description("report_compiler_warnings"))
def report_compiler_warnings(project_id: str) -> dict[str, Any]:
    return shared.report_compiler_warnings(project_id)


@mcp.tool(description=read_tool_description("approve_code_execution"))
def approve_code_execution(project_id: str, reason: str) -> str:
    return shared.approve_code_execution(project_id, reason)


@mcp.tool(description=read_tool_description("read_terms"))
def read_terms(project_id: str) -> shared.Terms:
    return shared.read_terms(project_id)


@mcp.tool(description=read_tool_description("write_terms"))
def write_terms(project_id: str, terms: shared.Terms) -> shared.Terms:
    return shared.write_terms(project_id, terms)


@mcp.tool(description=read_tool_description("read_claim_shapes"))
def read_claim_shapes(project_id: str) -> list[ClaimShape]:
    return claim_shape_tools.read_claim_shapes(project_id)


@mcp.tool(description=read_tool_description("write_claim_shapes"))
def write_claim_shapes(
    project_id: str, shapes: list[ClaimShapeInput]
) -> list[ClaimShape]:
    return claim_shape_tools.write_claim_shapes(project_id, shapes)


@mcp.tool(description=read_tool_description("read_workflow_summary"))
def read_workflow_summary(project_id: str) -> shared.workspace.WorkflowSummary:
    return shared.read_workflow_summary(project_id)


@mcp.tool(description=read_tool_description("read_stage"))
def read_stage(project_id: str, stage_id: str) -> str:
    return shared.read_stage(project_id, stage_id)


@mcp.tool(description=read_tool_description("edit_stages"))
def edit_stages(project_id: str, edits: list[StageEdit]) -> shared.EditedStages:
    return working_copy.catch_stage_edit_refusals(
        lambda: edit_stages_reporting_drops(project_id, edits)
    )


@mcp.tool(description=read_tool_description("add_stage"))
def add_stage(project_id: str, stages: list[SubmittedStage]) -> dict[str, Any]:
    return add_stages_reporting_drops(project_id, stages)


@mcp.tool(description=read_tool_description("delete_stage"))
def delete_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    return shared.delete_stage(project_id, stage_id)


@mcp.tool(description=read_tool_description("save_version"))
def save_version(
    project_id: str, message: str, parent_version: str | None = None
) -> dict[str, Any]:
    return working_copy.save_working_copy_as_version(project_id, message, parent_version)


@mcp.tool(description=read_tool_description("read_review_guide"))
def read_review_guide(project_id: str, version_id: str) -> shared.ReviewGuide | None:
    return shared.read_review_guide(project_id, version_id)


@mcp.tool(description=read_tool_description("write_review_guide"))
def write_review_guide(
    project_id: str, version_id: str, guide: shared.ReviewGuideDraft
) -> shared.ReviewGuide:
    return shared.write_review_guide(project_id, version_id, guide)


@mcp.tool(description=read_tool_description("run_workflow"))
def run_workflow(
    project_id: str,
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    files: dict[str, str | list[str]] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    try:
        return shared.run_workflow(project_id, version_id, limits, files, bust_cache)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(description=read_tool_description("move_file_to_project"))
def move_file_to_project(project_id: str, file_id: str) -> shared.StoredFileView:
    return shared.move_file_to_project(project_id, file_id)


@mcp.tool(description=read_tool_description("run_workflow_test"))
def run_workflow_test(
    project_id: str,
    limit: int | None,
    version_id: str | None = None,
    stage_ids: list[str] | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    return shared.run_workflow_test(project_id, limit, version_id, stage_ids, offset)


@mcp.tool(description=read_tool_description("profile_stage_output_data_range"))
def profile_stage_output_data_range(
    project_id: str, run_id: str, stage_id: str, columns: list[str], max_values: int,
) -> dict[str, Any]:
    return shared.profile_stage_output_data_range(
        project_id, run_id, stage_id, columns, max_values)


@mcp.tool(description=read_tool_description("profile_file"))
def profile_file(
    project_id: str, file_id: str, columns: list[str] | None = None, max_values: int = 20,
    sheet_name: str | int = 0, header_row: int = 0, first_column: int = 0,
) -> dict[str, Any]:
    try:
        profile = shared.profile_file(project_id, file_id, columns, max_values,
                                      sheet_name, header_row, first_column)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **profile.model_dump()}


@mcp.tool(description=read_tool_description("survey_workbook"))
def survey_workbook(project_id: str, file_id: str, from_row: int = 0) -> dict[str, Any]:
    try:
        sheets = shared.survey_workbook(project_id, file_id, from_row)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "sheets": [sheet._asdict() for sheet in sheets]}


@mcp.tool(description=read_tool_description("list_files"))
def list_files(project_id: str | None = None) -> shared.ProjectFilesView:
    return shared.list_files(project_id, _resolve_file_upload_url(project_id))


def _resolve_file_upload_url(project_id: str | None) -> str:
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
    # No project: the upload has nowhere to go through a project route, so the URL is
    # the one a conversation posts to, which stores a file against no project.
    if project_id is None:
        return f"{scheme}://{host}/files"
    return f"{scheme}://{host}/project/{project_id}/files"


@mcp.tool(description=read_tool_description("list_runs"))
def list_runs(project_id: str, limit: int = shared.MAX_RUNS_LISTED) -> shared.RunHistory:
    return shared.list_runs(project_id, limit)


@mcp.tool(description=read_tool_description("get_run_status"))
def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    try:
        return shared.get_run_status(project_id, run_id)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(description=read_tool_description("read_stage_output_rows"))
def read_stage_output_rows(
    project_id: str,
    run_id: str,
    stage_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> shared.StageOutputRows:
    return shared.read_stage_output_rows(project_id, run_id, stage_id, limit, offset)
