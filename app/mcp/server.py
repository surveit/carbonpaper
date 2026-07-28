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
from app.models import HUMAN_REVIEW_QUEUE_CONTRACT_NOTE, NODE_TYPES
from app.runtime import stage_tests
from app.services import generation
from app.services import loader
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

# The two per-node-type facts an authoring client gets wrong most often, rendered from
# app.models so this prompt and the editing agent's cannot drift apart on them, and
# wrapped to the width of the surrounding prose.
_NODE_TYPE_CONSTRAINTS = "\n".join(
    textwrap.fill(f"- {stage_type} — {note}", width=88, subsequent_indent="  ")
    for stage_type, note in (
        ("input_data", NODE_TYPES["input_data"]["notes"]),
        ("human_review_queue", HUMAN_REVIEW_QUEUE_CONTRACT_NOTE),
    )
)

INSTRUCTIONS = f"""\
glassbox turns an investigation methodology (prose) into a reviewable, runnable data
pipeline. YOU author the workflow, one validated stage at a time, through these tools.
A stage is written, validated against the whole graph, and only then stored.

# Setup
1. create_project(name, document) — the methodology prose becomes the project's source
   of record. Returns the project_id every other tool takes.
2. generate_data_model(project_id) — derives the named schemas from the document. Runs in
   the background; poll get_project_status until schemas appear.
3. The HUMAN approves the data model in the web UI. No tool approves it.

# Authoring the workflow, one stage at a time
4. Read the methodology document and read_data_model(project_id). The approved schemas are
   the vocabulary the stages carry.
5. Plan the stages, then author them in DEPENDENCY ORDER: a stage's `inputs` may name only
   stages that already exist in the workflow. The first stage you add starts the workflow,
   so it takes no inputs — it is the input_data stage that reads the source.
6. An upstream stage's output_schema is what flows down the edge. A stage's declared input
   schema is usually that schema verbatim; it differs when the stage reads only part of what
   upstream emits. Either way it must be a subset the upstream can satisfy.
7. As the graph grows: describe_workflow(project_id) for the shape (ids, types, inputs,
   review state), read_stage(project_id, stage_id) for one stage in full,
   edit_stage(project_id, stage_id, changes_json) to change only the fields you name (a
   JSON Merge Patch), remove_stage(project_id, stage_id) to undo a stage you added
   (refused while another stage still lists it in `inputs`).

Added stages land `unreviewed`. REVIEW AND APPROVAL ARE HUMAN-ONLY, in the web UI, and
only a human publishes. Your job ends at a saved version with a workflow test run for the
human to review.

# Per-stage tests
generate_stage_tests(project_id, stage_id) derives one python-transform stage's tests from
the methodology (background; read_stage to see them once done). run_stage_tests(project_id,
stage_id?) runs the authored tests against that stage's current code — omit stage_id to run
every python-transform stage. Loop edit_stage → run_stage_tests until a stage's tests pass:
a failure means the CODE disagrees with the test, so fix the code.

# Running
Runs execute a stored version; save_version(project_id, message) creates one, then
run_workflow_test against it is how you finish. Publishing is human-only.
run_workflow(project_id, version_id?) starts a run of record and returns a run_id,
get_run_status(project_id, run_id) follows it to its outcome, and
run_workflow_test(project_id, version_id?, limit, offset) executes any stored version —
published or not — over a small slice of the real source without producing a run of record.

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


@mcp.tool()
def list_projects() -> list[str]:
    """List the names of every project in the workspace that has an authored
    workflow. A just-created project appears here only once its first stage has
    been added — use get_project_status(project_id) to inspect one before that."""
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
    human then reviews/approves the data model in the web UI; the approved
    schemas are the vocabulary you author the workflow's stages against."""
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
    for a human to re-approve — you cannot approve it yourself. You cannot
    change a stage's id this way."""
    return catch_stage_edit_refusals(lambda: project_service.edit_stage(project_id, stage_id, changes_json))


@mcp.tool()
def add_stage(project_id: str, stage_json: str) -> dict[str, Any]:
    """Create a NEW stage in the workflow. `stage_json` is a FULL stage as JSON:
    id (new and unique — use edit_stage to change an existing one), name, type,
    the type's handle block (e.g. connector / llm / function), output_schema, and
    inputs. Every id listed in `inputs` must ALREADY be a stage in this workflow,
    so author stages in dependency order. read_stage on a similar existing stage
    shows the shape.

    The WHOLE resulting workflow is validated before anything is written: the
    stage's own shape, unique ids, inputs resolving, no cycles, and edge
    conformance — a column a stage declares on an input that the upstream's
    output_schema does not supply is refused. On a refusal nothing is written and
    the issues name the stage, the edge and the offending columns: read_stage the
    named upstream, repair this stage's declared input schema against what that
    stage really outputs, and call add_stage again.

    The new node lands 'unreviewed' for a human to approve. The FIRST stage
    of a project starts its workflow — no other tool creates one."""
    return catch_stage_edit_refusals(lambda: project_service.add_stage(project_id, stage_json))


@mcp.tool()
def remove_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    """Delete one stage from the workflow — the undo for a stage you added. The
    workflow WITHOUT the stage is validated first: if another stage still lists it
    in `inputs`, the removal is refused, nothing is deleted, and the issues are
    returned (remove or repoint the downstream stage first). Removing the last
    remaining stage is allowed."""
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


@mcp.tool()
def save_version(
    project_id: str, message: str, parent_version: str | None = None
) -> dict[str, Any]:
    """Freeze the project's CURRENT workflow into an immutable version — the snapshot
    a run or a workflow test executes. Born UNPUBLISHED: only a human publishes.

    `parent_version` is the version YOU started this edit from. Supply it only when you
    actually loaded that version; it is recorded verbatim as this snapshot's ancestor,
    and an id naming no version of this project is refused. Omitting it is normal and
    records no ancestor — nothing is inferred from what else the project has stored.

    The working copy is strict-loaded first, so an invalid workflow comes back as
    {ok: False, issues} and no version is written."""
    pdir = _resolve_existing_project(project_id)
    try:
        if parent_version is not None:
            versioning.validate_version_exists(pdir, parent_version)
        version = versioning.create_version_from_disk(
            pdir, message=message, reviewer="agent", parent_version=parent_version
        )
    except _STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}
    return {"ok": True, "issues": [], "version_id": version.version_id}


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
