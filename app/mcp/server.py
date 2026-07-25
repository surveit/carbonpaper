"""The "glassbox" FastMCP server: authoring tools over app.services.

Every tool takes an explicit `project_id` (the examples/<name>/ directory name)
and goes through the name-based service surface — tools resolve project
directories only through workspace.resolve_project_dir (which refuses names
escaping the workspace); any further path use stays inside that resolved
directory. Generation tools start LIVE chat turns on the server event loop and
return immediately; callers poll get_project_status. Failures raise — FastMCP
surfaces the exception message as a tool error — never a fabricated success."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from app.core.errors import (
    MissingInputBindingError,
    NoVersionToRunError,
    NoWorkflowTestVersionError,
    RunNotFoundError,
)
from app.runtime import stage_tests
from app.services import generation
from app.services import loader
from app.services import project as project_service
from app.services import run as run_service
from app.services import workflow_test as workflow_test_service
from app.services import workspace
from app.services.errors import WorkflowLoadError

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

INSTRUCTIONS = """\
glassbox turns an investigation methodology (prose) into a reviewable, runnable data
pipeline. Authoring order: create_project → generate_data_model → the human
approves the data model in the web UI → generate_workflow → refine with
edit_stage / add_stage. Generation runs in the background: poll
get_project_status until the data model / workflow appears. Approval is
human-only and happens in the web UI, never through these tools.

Once a workflow exists, derive and run per-stage tests:
generate_stage_tests(project_id, stage_id) derives one python-transform stage's
tests from the methodology (background; read_stage to see them once done), and
run_stage_tests(project_id, stage_id?) runs the authored tests against the stage's
current code — omit stage_id to run every python-transform stage, or pass one to
scope it. Loop edit_stage → run_stage_tests until a stage's tests pass."""

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


@mcp.tool()
def list_projects() -> list[str]:
    """List the names of every project in the workspace that has an authored
    workflow. A just-created project appears here only after its workflow is
    generated — use get_project_status(project_id) to inspect one before that."""
    return project_service.list_projects()


@mcp.tool()
def create_project(name: str, document: str) -> dict[str, Any]:
    """Create a NEW project from a methodology document (prose describing how the
    investigation finds, verifies, and surfaces its claims). Writes the document
    as the project's source of record. Returns the project_id (the sanitized
    name). Fails loudly if the name is taken — never overwrites. Next step:
    generate_data_model(project_id)."""
    project_id = project_service.create_project(name, document, source="mcp")
    return {"project_id": project_id, "next": "generate_data_model"}


@mcp.tool()
def get_project_status(project_id: str) -> dict[str, Any]:
    """One project's full status snapshot: document present?, data-model state
    (generating shows no schemas yet; then unapproved/approved), workflow stage
    counts and review coverage, versions, runs. Poll this after generate_* to see
    the result land."""
    pdir = _resolve_existing_project(project_id)
    return project_service.project_state(pdir).model_dump(mode="json")


@mcp.tool()
async def generate_data_model(project_id: str) -> dict[str, Any]:
    """Generate the project's DATA MODEL (named schemas) from its methodology
    document. Starts a live generation turn in the background and returns
    immediately — poll get_project_status until schemas appear, and tell the user
    they can watch it stream at the returned `watch` path in the web UI. The
    human then reviews/approves the data model in the web UI before the workflow
    is generated."""
    pdir = _resolve_existing_project(project_id)
    document = _read_document(pdir, project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    session_id = generation.start_generation(pdir, document=document, model=model)
    return {"status": "started", "watch": f"/chat/{session_id}", "poll": "get_project_status"}


@mcp.tool()
async def generate_stage_tests(project_id: str, stage_id: str) -> dict[str, Any]:
    """Derive tests for one python-transform stage FROM THE METHODOLOGY. The
    derivation is code-blind by construction: the deriver only ever sees the
    methodology document plus the data model / stage schemas, never the stage's
    code or any existing tests — so calling this right after generating or
    editing the code cannot anchor the tests on the implementation (that would
    assert the code equals itself). Starts a background turn and returns
    immediately; on completion the derived suite REPLACES the stage's tests
    wholesale. Fails loudly if the stage is not a python transform or has no
    output schema."""
    pdir = _resolve_existing_project(project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    session_id = generation.start_stage_test_generation(pdir, stage_id=stage_id, model=model)
    return {
        "status": "started",
        "watch": f"/chat/{session_id}",
        "poll": "get_project_status",
        "note": "read_stage to see the derived tests once done",
    }


@mcp.tool()
def run_stage_tests(project_id: str, stage_id: str | None = None) -> dict[str, Any]:
    """Run a stage's authored tests against its CURRENT code and report the
    result. Omit `stage_id` to run every python-transform stage that has tests,
    or pass one to scope the run to that stage. Use this after regenerating code
    with edit_stage to see which tests the new code fails — the report carries a
    summary plus, per test, its status and any cell diffs, and lists
    `untested_python_stages` (python transforms with no tests, a coverage gap).
    This does NOT edit tests: a failing test means the code disagrees with the
    frozen test, and the fix is to the code (or to re-derive via
    generate_stage_tests), never to bend the test to the code."""
    pdir = _resolve_existing_project(project_id)
    stages = loader.load_workflow(pdir)
    report: stage_tests.StageTestsReport = stage_tests.run_stage_tests(stages, stage_id)
    return report.model_dump(mode="json")


@mcp.tool()
def read_data_model(project_id: str) -> list[dict[str, Any]]:
    """The project's data model: every named schema as JSON (empty list if none
    generated yet)."""
    pdir = _resolve_existing_project(project_id)
    return workspace.load_schemas(pdir)


@mcp.tool()
def describe_workflow(project_id: str) -> dict[str, Any]:
    """Summarize a project's workflow: each stage's id, type, name, upstream input
    ids, and review state. Read this before editing so you know the current
    shape. Does not return full stage specs — use read_stage for one."""
    _resolve_existing_project(project_id)
    return project_service.describe_workflow(project_id)


@mcp.tool()
def read_stage(project_id: str, stage_id: str) -> str:
    """Return the JSON of one stage from the workflow. Read before editing."""
    return project_service.read_stage(project_id, stage_id)


@mcp.tool()
def edit_stage(project_id: str, stage_id: str, changes_json: str) -> dict[str, Any]:
    """Change specific fields of one stage. `changes_json` is a JSON object of
    ONLY the fields to change (a JSON Merge Patch): {"limit": 100} sets limit;
    {"llm": {"model": "opus"}} changes only llm.model and leaves the rest of the
    llm block intact; {"name": null} deletes a field. Fields you do not mention
    are preserved exactly. Validated first; if invalid, nothing is written and
    the issues are returned. A successful edit drops the node to 'edited_stale'
    (amber) for a human to re-approve — you cannot approve it yourself. You
    cannot change a stage's id this way."""
    result = project_service.edit_stage(project_id, stage_id, changes_json)
    return {"ok": result.ok, "issues": result.issues}


@mcp.tool()
def add_stage(project_id: str, stage_json: str) -> dict[str, Any]:
    """Create a NEW stage in the workflow. `stage_json` is a full stage as JSON:
    id (new and unique — use edit_stage to change an existing one), name, type,
    the type's handle block (e.g. connector / llm / function), output_schema, and
    inputs. Every id listed in `inputs` must ALREADY be a stage in this workflow —
    a dangling input is rejected. read_stage on a similar existing stage shows the
    shape. Validated first; if invalid, nothing is written and the issues are
    returned. The new node lands 'unreviewed' (amber) for a human to approve."""
    result = project_service.add_stage(project_id, stage_json)
    return {"ok": result.ok, "issues": result.issues}


@mcp.tool()
def remove_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    """Delete one stage from the workflow — the undo for a stage you added. The
    workflow WITHOUT the stage is validated first: if another stage still lists it
    in `inputs`, the removal is refused, nothing is deleted, and the issues are
    returned (remove or repoint the downstream stage first). Removing the last
    remaining stage is allowed."""
    result = project_service.remove_stage(project_id, stage_id)
    return {"ok": result.ok, "issues": result.issues}


@mcp.tool()
def run_workflow(project_id: str, version_id: str | None = None) -> dict[str, Any]:
    """Start a REAL production run of the project's published workflow and return
    its `run_id` immediately — the run executes in the background. This is a run
    of record: it writes a manifest under the project's runs/ dir and produces the
    workflow's published artifacts. `version_id` pins a specific published version
    (omit for the newest published one); an unpublished or missing version is a
    loud error, never a silent fallback. Poll get_run_status(project_id, run_id)
    for live progress and the final status. On a pre-run failure (nothing
    published, an unbound input) returns {ok: False, error} and starts no run."""
    _resolve_existing_project(project_id)  # loud if the project doesn't exist
    try:
        run_id = run_service.start_run(project_id, version_id=version_id)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}
    return {"run_id": run_id, "status": run_service.read_run_status(project_id, run_id)["status"]}


@mcp.tool()
def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    """The current manifest of one production run as a dict: its overall status
    (running / ok / errors / halted), per-stage statuses, and run metadata. Poll
    this after run_workflow to follow progress and see the outcome. An unknown or
    expired run_id returns {ok: False, error} rather than a fabricated status."""
    _resolve_existing_project(project_id)  # loud if the project doesn't exist
    try:
        return run_service.read_run_status(project_id, run_id)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def run_workflow_test(
    project_id: str, version_id: str | None = None, limit: int = 20, offset: int = 0,
) -> dict[str, Any]:
    """Run a NON-production workflow test: take the first `limit` rows (from
    `offset`) of the workflow's bound source and run the frontier over just that
    slice, so an author can watch the pipeline execute on real data before
    publishing. It is NOT a run of record — it writes only under the project's
    separate workflow_tests/ dir (publish stages run, but their artifacts land
    run-scoped there) and carries no cross-run state. Accepts any stored version,
    published or not (omit `version_id` for the newest). Returns the verdict
    {ok, workflow_test_id, version_id, stages_run, error}: `ok` False on any stage
    error, with `error` naming what failed. Per-stage row counts are in the
    manifest. A project with no stored version is a loud error."""
    _resolve_existing_project(project_id)  # loud if the project doesn't exist
    try:
        return workflow_test_service.run_workflow_test(
            project_id, version_id=version_id, limit=limit, offset=offset)
    except _RUN_TOOL_ERRORS as exc:
        return {"ok": False, "error": str(exc)}


def _resolve_existing_project(project_id: str) -> Path:
    """Resolve a project id to its directory, raising if no such project exists —
    a typo'd id is a loud error, never an empty result that reads as a real
    (empty) project."""
    pdir = workspace.resolve_project_dir(project_id)
    if not pdir.is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    return pdir


def _read_document(pdir: Path, project_id: str) -> str:
    """The project's methodology document — the input every generation grounds on.
    A missing document is a raised error, never an empty-string fallback."""
    doc_path = pdir / "document.md"
    if not doc_path.is_file():
        raise ValueError(f"project '{project_id}' has no document.md to generate from")
    return doc_path.read_text(encoding="utf-8")
