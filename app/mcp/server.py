"""The "sift" FastMCP server: authoring tools over app.services.

Every tool takes an explicit `project_id` (the examples/<name>/ directory name)
and goes through the name-based service surface — tools never build filesystem
paths beyond resolving the project directory via workspace.resolve_project_dir
(which refuses names that escape the workspace). Generation tools start LIVE
chat turns on the server event loop and return immediately; callers poll
get_project_status. Failures raise — FastMCP surfaces the exception message as
a tool error — never a fabricated success."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.services import generation
from app.services import project as project_service
from app.services import workspace

INSTRUCTIONS = """\
sift turns an investigation methodology (prose) into a reviewable, runnable data
pipeline. Authoring order: create_project → generate_data_model → the human
approves the data model in the web UI → generate_workflow → refine with
edit_stage / add_stage. Generation runs in the background: poll
get_project_status until the data model / workflow appears. Approval is
human-only and happens in the web UI, never through these tools."""

mcp = FastMCP(
    name="sift",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


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
    pdir = workspace.resolve_project_dir(project_id)
    if not pdir.is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    return project_service.project_state(pdir).model_dump(mode="json")


@mcp.tool()
async def generate_data_model(project_id: str) -> dict[str, Any]:
    """Generate the project's DATA MODEL (named schemas) from its methodology
    document. Starts a live generation turn in the background and returns
    immediately — poll get_project_status until schemas appear, and tell the user
    they can watch it stream at the returned `watch` path in the web UI. The
    human then reviews/approves the data model in the web UI before the workflow
    is generated."""
    pdir = workspace.resolve_project_dir(project_id)
    document = _read_document(pdir, project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    session_id = generation.start_generation(pdir, document=document, model=model)
    return {"status": "started", "watch": f"/chat/{session_id}", "poll": "get_project_status"}


@mcp.tool()
async def generate_workflow(project_id: str) -> dict[str, Any]:
    """Generate the project's WORKFLOW from its methodology document, grounded on
    the data model ONLY if a human has approved it in the web UI (an unapproved
    model is not passed — approve first for a grounded workflow). Never touches
    the schemas. Starts a live generation turn in the background and returns
    immediately — poll get_project_status until the workflow appears."""
    pdir = workspace.resolve_project_dir(project_id)
    document = _read_document(pdir, project_id)
    model = project_service.project_meta(pdir).model or "sonnet"
    data_model = generation.load_approved_data_model(pdir)
    session_id = generation.start_workflow_generation(
        pdir, document=document, model=model, data_model=data_model
    )
    return {
        "status": "started",
        "grounded_on_approved_data_model": data_model is not None,
        "watch": f"/chat/{session_id}",
        "poll": "get_project_status",
    }


@mcp.tool()
def read_data_model(project_id: str) -> list[dict[str, Any]]:
    """The project's data model: every named schema as JSON (empty list if none
    generated yet)."""
    return workspace.load_schemas(workspace.resolve_project_dir(project_id))


@mcp.tool()
def describe_workflow(project_id: str) -> dict[str, Any]:
    """Summarize a project's workflow: each stage's id, type, name, upstream input
    ids, and review state. Read this before editing so you know the current
    shape. Does not return full stage specs — use read_stage for one."""
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


def _read_document(pdir: Path, project_id: str) -> str:
    """The project's methodology document — the input every generation grounds on.
    A missing document is a raised error, never an empty-string fallback."""
    doc_path = pdir / "document.md"
    if not doc_path.is_file():
        raise ValueError(f"project '{project_id}' has no document.md to generate from")
    return doc_path.read_text(encoding="utf-8")
