"""The "glassbox" FastMCP server: authoring tools over app.services.

Tools resolve project directories only through workspace.resolve_project_dir, which refuses
names escaping the workspace. Generation tools start LIVE chat turns on the server event
loop and return immediately; callers poll get_project_status. Failures raise, never fake success."""
from __future__ import annotations

import textwrap
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
from app.models import (
    StageDraft,
    find_workflow_compiler_warnings,
)
from app.models.authoring_lifecycle_note import AUTHORING_LIFECYCLE_GUIDANCE
from app.models.enum_from_data_note import ENUM_FROM_DATA_GUIDANCE
from app.tools.tool_specs import SAVE_VERSION_FROM_WORKING_COPY, TOOL_SPECS
from app.models.review_guide import ReviewGuideDraft
from app.services.versioning import ReviewGuide
from app.models.stages.node_types import NODE_TYPES
from app.runtime import stage_tests
from app.services import data_model
from app.services import methodology
from app.services import generation
from app.services import loader
from app.services import frame_profile
from app.services import project as project_service
from app.services import run as run_service
from app.services import versioning
from app.services import workflow_test as workflow_test_service
from app.services import workspace
from app.services.errors import WorkflowLoadError
from app.services.stage_edit import EditStageResult

# Domain failures a run/workflow-test tool turns into {ok: False, error: str(exc)} — a
# loud, honest verdict rather than a traceback or a fabricated run id/status.
# Anything outside this set propagates as a genuine internal fault.
_RUN_TOOL_ERRORS = (
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


def _render_node_type_constraints() -> str:
    """Every node type's notes as bullets, from NODE_TYPES so the two prompts cannot drift."""
    return "\n".join(
        textwrap.fill(f"- {stage_type} — {spec.notes}", width=88, subsequent_indent="  ")
        for stage_type, spec in NODE_TYPES.items()
    )


_NODE_TYPE_CONSTRAINTS = _render_node_type_constraints()

INSTRUCTIONS = f"""\
glassbox turns an investigation methodology (prose) into a reviewable, runnable data
pipeline. YOU author the workflow through these tools. Every stage is validated against
the whole graph before it is stored.

# The lifecycle every project follows
{AUTHORING_LIFECYCLE_GUIDANCE}
(Here, a limited run is run_workflow_test's `limit`/`offset` slice; a full run is
run_workflow.)

{ENUM_FROM_DATA_GUIDANCE}
(Here: save_version, then run_workflow_test(stage_ids=["<the input stage id>"],
limit=null) — a named source stage EXECUTES, and a null limit is the whole bound
file — then profile_stage_output_data_range on what it wrote. edit_stage tightens
the schema afterwards.)

# Setup
1. create_project(name, document) — the methodology prose becomes the project's source
   of record. Returns the project_id every other tool takes.
2. generate_data_model(project_id) — generates the named schemas from the document. Runs in
   the background; poll get_project_status until schemas appear.
3. The HUMAN approves the data model in the web UI. No tool approves it.

# Authoring the workflow
4. Read the methodology document and read_data_model(project_id). The approved schemas are
   the vocabulary the stages carry.
5. Plan the stages, then add_stage(project_id, stages) them — `stages` is a LIST, so send
   every stage you are ready to author in ONE call rather than one per call. Order does not
   matter: they are sorted by the `inputs` they declare, and an input may name a stage in
   the same call or one already in the workflow. Stages that validate are stored even if
   another in the batch fails; the result's added/failed/skipped says which is which. The
   workflow starts with an input_data stage that reads the source and takes no inputs.
6. An upstream stage's output_schema is what flows down the edge. A stage's MANDATORY
   declared input schema is usually that schema verbatim; it differs when the stage reads
   only part of what upstream emits. Either way it must be a subset the upstream can satisfy.
7. As the graph grows: describe_workflow(project_id) for the shape (ids, types, inputs,
   review state), read_stage(project_id, stage_id) for one stage in full,
   edit_stage(project_id, stage_id, changes_json) to change only the fields you name (a
   JSON Merge Patch), remove_stage(project_id, stage_id) to undo a stage you added
   (refused while another stage still lists it in `inputs`).

# The review guide, and why it exists
A workflow you author is not self-explaining. The human who owns the methodology has to
decide whether it does what they meant — and they read the stage graph, not the code. The
review guide is the prose that makes that decision possible: an ordered walkthrough,
each step naming the stages it covers and saying what a reviewer should check.

8. write_review_guide(project_id, version_id, guide) — write it once the workflow needs a
   human to understand it before acting on it, which is any version you expect to be
   published or run. Nothing generates one and nothing seeds one; you write it from a blank
   page. read_review_guide shows what a version already carries.
   Write it FOR the methodology's owner, not a programmer: use the document's terms of
   art, wrap column names in `backticks`, and say what could be quietly wrong rather than
   restating the stage names and order the page already shows.

Added stages land `unreviewed`. REVIEW AND APPROVAL ARE HUMAN-ONLY, in the web UI, and
only a human publishes. Your job ends at a saved version carrying a review guide, with a
workflow test run for the human to review.

# Per-stage tests
Once a python-transform stage exists, generate_stage_tests writes its tests from the
methodology; then loop edit_stage → run_stage_tests until they pass.

# Finishing
report_compiler_warnings(project_id) reports what is wrong with the workflow,
including any stage whose examples do not pass. Dirty is fine while you build.

Two different things you can ask a human for, with different bars:
- A look at a smoke test — run_workflow_test and a review of what came out. Fine with
  warnings outstanding; say which ones are open.
- FINAL SIGNOFF. Do not ask for this with any warning outstanding. Either clear it, or
  state plainly why that specific warning is safe to ignore here. A warning you leave
  unmentioned spends the reviewer's attention on something you already knew about.

# Running
Runs execute a stored version; save_version(project_id, message) creates one, then
run_workflow_test against it is how you finish. Publishing is human-only.
run_workflow(project_id, version_id?) starts a real run and returns a run_id,
get_run_status(project_id, run_id) follows it to its outcome, and
run_workflow_test(project_id, limit, version_id?, stage_ids?, offset?) executes any stored
version — published or not — over `limit` rows of the real source, as a run marked
is_test_run; profile_stage_output_data_range then profiles what a stage of it wrote.

# Constraints
{_NODE_TYPE_CONSTRAINTS}
- Never fabricate a column, source, model, or value. If the methodology does not supply it,
  leave it out and say what is missing.

list_projects() names the projects that already have an authored workflow;
get_project_status(project_id) is the full snapshot of any one project."""

mcp = FastMCP(
    name="glassbox",
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
    """ASGI endpoint for /mcp: delegates to the CURRENT lifespan's manager. A
    class instance (not a function) so Starlette's Route treats it as an ASGI
    app rather than wrapping it as a request-response handler."""

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
    """Run a fresh MCP session manager for one server lifetime (the app lifespan
    enters this). Mirrors the manager FastMCP.streamable_http_app() would build
    from this module's `mcp` settings; `_mcp_server` is the SDK's only handle to
    the underlying low-level server (no public accessor — pyproject pins the mcp
    version bound this relies on)."""
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
def list_projects() -> list[str]:
    return project_service.list_projects()


@mcp.tool(description=TOOL_SPECS["create_project"].description)
def create_project(name: str, document: str) -> dict[str, Any]:
    project_id = project_service.create_project(name, document, source="mcp")
    return {"project_id": project_id, "next": "generate_data_model"}


@mcp.tool(description=TOOL_SPECS["get_project_status"].description)
def get_project_status(project_id: str) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    return project_service.project_state(pdir).model_dump(mode="json")


@mcp.tool(description=TOOL_SPECS["generate_data_model"].description)
async def generate_data_model(project_id: str) -> dict[str, Any]:
    pdir = _resolve_existing_project(project_id)
    document = _read_document(project_id)
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
    _resolve_existing_project(project_id)
    stages = loader.load_workflow(project_id)
    report: stage_tests.StageTestsReport = stage_tests.run_stage_tests(stages, stage_id)
    return report.model_dump(mode="json")


@mcp.tool(description=TOOL_SPECS["report_compiler_warnings"].description)
def report_compiler_warnings(project_id: str) -> dict[str, Any]:
    _resolve_existing_project(project_id)
    stages = loader.load_workflow(project_id)
    failing = stage_tests.run_stage_tests(stages).count_failing_by_stage()
    report = find_workflow_compiler_warnings(stages, failing)
    return {
        "is_clean": report.is_clean,
        "errors": [w.model_dump(mode="json") for w in report.errors],
        "warnings": [w.model_dump(mode="json") for w in report.warnings],
    }


@mcp.tool(description=TOOL_SPECS["read_data_model"].description)
def read_data_model(project_id: str) -> list[dict[str, Any]]:
    _resolve_existing_project(project_id)
    return [s.model_dump(mode="json", exclude_none=True)
            for s in data_model.load_schemas(project_id)]


@mcp.tool(description=TOOL_SPECS["describe_workflow"].description)
def describe_workflow(project_id: str) -> dict[str, Any]:
    _resolve_existing_project(project_id)
    return project_service.describe_workflow(project_id)


@mcp.tool(description=TOOL_SPECS["read_stage"].description)
def read_stage(project_id: str, stage_id: str) -> str:
    return project_service.read_stage(project_id, stage_id)


@mcp.tool(description=TOOL_SPECS["edit_stage"].description)
def edit_stage(project_id: str, stage_id: str, changes_json: str) -> dict[str, Any]:
    return catch_stage_edit_refusals(lambda: project_service.edit_stage(project_id, stage_id, changes_json))


@mcp.tool(description=TOOL_SPECS["add_stage"].description)
def add_stage(project_id: str, stages: list[StageDraft]) -> dict[str, Any]:
    return project_service.add_stages_reporting_drops(project_id, stages)


@mcp.tool(description=TOOL_SPECS["remove_stage"].description)
def remove_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    return catch_stage_edit_refusals(lambda: project_service.remove_stage(project_id, stage_id))


def catch_stage_edit_refusals(edit: Callable[[], EditStageResult]) -> dict[str, Any]:
    """Run one stage-mutating service call and convert its expected refusals onto the
    {ok, issues} channel these instructions document. An expected refusal — a stored
    workflow that does not load, a stage id that is not in the workflow — comes back as
    `issues` carrying the failure's own message, not as a tool exception a client is not
    watching for. Any other exception propagates."""
    try:
        result = edit()
    except _STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}
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
def run_workflow(project_id: str, version_id: str | None = None) -> dict[str, Any]:
    _resolve_existing_project(project_id)  # loud if the project doesn't exist
    try:
        run_id = run_service.start_run(project_id, version_id=version_id)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return {"run_id": run_id, "status": run_service.read_run_status(project_id, run_id)["status"]}


@mcp.tool(description=TOOL_SPECS["get_run_status"].description)
def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    _resolve_existing_project(project_id)  # loud if the project doesn't exist
    try:
        return run_service.read_run_status(project_id, run_id)
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
    """Resolve a project id to its directory, raising if no such project exists —
    a typo'd id is a loud error, never an empty result that reads as a real
    (empty) project."""
    pdir = workspace.resolve_project_dir(project_id)
    if not pdir.is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    return pdir


def _read_document(project_id: str) -> str:
    """A missing document is a raised error, never an empty-string fallback."""
    document = methodology.read_methodology(project_id)
    if document is None:
        raise ValueError(f"project '{project_id}' has no methodology to generate from")
    return document
