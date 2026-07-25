"""The "glassbox" FastMCP server: authoring tools over app.services.

Every tool takes an explicit `project_id` (the examples/<name>/ directory name)
and goes through the name-based service surface — tools resolve project
directories only through workspace.resolve_project_dir (which refuses names
escaping the workspace); any further path use stays inside that resolved
directory. Generation tools start LIVE chat turns on the server event loop and
return immediately; callers poll get_project_status. Failures raise — FastMCP
surfaces the exception message as a tool error — never a fabricated success.

The WORKFLOW is authored incrementally by the CLIENT: add_stage / edit_stage /
remove_stage each re-validate the whole resulting workflow and write at most one
stage. There is deliberately NO whole-workflow generate/regenerate tool — no tool
here can overwrite or reset a workflow that already has stages."""
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
pipeline. YOU are the compiler: there is no one-shot "generate the workflow" call.
The workflow is authored INCREMENTALLY — one whole, validated stage at a time — and
no tool here can overwrite or reset a workflow that already has stages.

# The authoring loop

1. create_project(name, document) — the methodology prose is the project's source of
   record. Write it first; it is what every later step grounds on.
2. generate_data_model(project_id) — runs in the background; poll get_project_status
   until schemas appear. A HUMAN then approves the data model in the web UI.
3. read_data_model(project_id) — the approved nouns your stages import and generate.
4. Author the workflow STAGE BY STAGE, in DEPENDENCY ORDER: sources first, then only
   stages whose upstreams already exist. For each stage:
     a. describe_workflow(project_id) — what is already there (ids, types, edges).
     b. read_stage(project_id, <upstream id>) for EVERY producer this stage reads
        from. Write the stage's `inputs[].schema` against the columns that upstream
        really outputs; never guess an upstream's columns, and never copy a
        neighbouring stage's block without reading it.
     c. add_stage(project_id, <full stage JSON>) — the WHOLE stage (id, name, type,
        the type's handle block, output_schema, inputs), never a fragment.
5. Repair as you go: edit_stage (a JSON Merge Patch of ONLY the fields that change)
   and remove_stage (the undo). Both re-validate the whole workflow.
6. Per python-transform stage: generate_stage_tests, then run_stage_tests. Loop
   edit_stage → run_stage_tests until the stage's tests pass. Fix the CODE, never
   bend a test to the code.
7. run_workflow_test to watch the pipeline execute over a slice of real data;
   run_workflow for a production run of a published version.

# What a write means here

Every write re-validates the ENTIRE resulting workflow — each stage's own
invariants, the graph (unique ids, inputs resolve, acyclic), and edge conformance:
an input schema its upstream does not actually supply is refused, naming the
columns. On {"ok": false} nothing was written, so there is nothing to undo — read
`issues`, fix the stage JSON, and call again. A tool that raises has written
nothing either; a failure is never reported as success.

# What stays human

Every stage you add lands `unreviewed` (amber) and every stage you edit drops to
`edited_stale`, for a human to approve in the web UI. You cannot approve, version,
or publish anything. Never fabricate a column, source, model, or value — if you
lack it, ask."""

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
    workflow. A just-created project appears here only once its FIRST stage has
    been added — use get_project_status(project_id) to inspect one before that."""
    return project_service.list_projects()


@mcp.tool()
def create_project(name: str, document: str) -> dict[str, Any]:
    """Create a NEW project from a methodology document (prose describing how the
    investigation finds, verifies, and surfaces its claims). Writes the document
    as the project's source of record. Returns the project_id (the sanitized
    name). Fails loudly if the name is taken — never overwrites. The project starts
    with an EMPTY workflow you then author one stage at a time. Next step:
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
    human then reviews/approves the data model in the web UI; read it back with
    read_data_model and author the workflow yourself, stage by stage, with
    add_stage."""
    pdir = _resolve_existing_project(project_id)
    document = _read_document(pdir, project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    session_id = generation.start_generation(pdir, document=document, model=model)
    return {
        "status": "started",
        "watch": f"/chat/{session_id}",
        "poll": "get_project_status",
        "next": "the human approves the data model, then author stages with add_stage",
    }


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
    ids, and review state. Read this before every add/edit/remove so you know the
    current shape — an empty `stages` list is a project whose workflow you have not
    started authoring yet, not an error. Does not return full stage specs — use
    read_stage for one."""
    _resolve_existing_project(project_id)
    return project_service.describe_workflow(project_id)


@mcp.tool()
def read_stage(project_id: str, stage_id: str) -> str:
    """Return the JSON of one stage from the workflow. Read every UPSTREAM producer
    before authoring a stage that reads from it — its `output_schema` is the only
    truthful source for the columns your new stage may declare on that input — and
    read a stage before editing it."""
    return project_service.read_stage(project_id, stage_id)


@mcp.tool()
def edit_stage(project_id: str, stage_id: str, changes_json: str) -> dict[str, Any]:
    """Change specific fields of one stage. `changes_json` is a JSON object of
    ONLY the fields to change (a JSON Merge Patch): {"limit": 100} sets limit;
    {"llm": {"model": "opus"}} changes only llm.model and leaves the rest of the
    llm block intact; {"name": null} deletes a field. Fields you do not mention
    are preserved exactly. The WHOLE resulting workflow is validated first; if
    invalid, nothing is written and the issues are returned — fix the patch and
    call again. A successful edit drops the node to 'edited_stale' (amber) for a
    human to re-approve — you cannot approve it yourself. You cannot change a
    stage's id this way (add_stage the new one, then remove_stage the old)."""
    result = project_service.edit_stage(project_id, stage_id, changes_json)
    return {"ok": result.ok, "issues": result.issues}


@mcp.tool()
def add_stage(project_id: str, stage_json: str) -> dict[str, Any]:
    """Create a NEW stage in the workflow — the one way a workflow grows, called
    once per stage in dependency order. `stage_json` is a FULL stage as JSON, never
    a fragment: id (new and unique — use edit_stage to change an existing one),
    name, type, the type's handle block (e.g. connector / llm / function),
    output_schema, and inputs. Every id listed in `inputs` must ALREADY be a stage
    in this workflow — author sources first, dependents after — and read_stage each
    of those upstreams before you declare the columns you read from them. The WHOLE
    resulting workflow is validated, including edge conformance (an input schema
    the upstream's output_schema does not supply is refused, naming the columns);
    if invalid, nothing is written and the issues are returned — repair the stage
    JSON and call again. The new node lands 'unreviewed' (amber) for a human to
    approve. Next step: generate_stage_tests + run_stage_tests for a python
    transform, or add_stage for the next stage down the graph."""
    result = project_service.add_stage(project_id, stage_json)
    return {"ok": result.ok, "issues": result.issues}


@mcp.tool()
def remove_stage(project_id: str, stage_id: str) -> dict[str, Any]:
    """Delete one stage from the workflow — the undo of the authoring loop. The
    workflow MINUS this stage is validated first: if any remaining stage still
    lists it in `inputs`, the removal is refused with those dangling edges as
    `issues` and NOTHING is deleted (remove or re-point the dependents first, then
    retry). Removing the last stage is allowed — that just returns the project to
    an empty workflow you can author into again. An unknown stage_id is a loud
    error, never a silent no-op."""
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
