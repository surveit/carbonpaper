"""Tools more than one surface offers, defined once and REFERENCED rather than rewritten.
Each closes over nothing, so the MCP server can decorate it and an agent config can wrap
it in a BoundToolSpec. A tool that must close over a session's context is not one of
these: it belongs to the agent owning that context.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Annotated, Callable

from app.core.agent.bound_tool import BoundToolSpec
from app.tools.types import ToolInputSchema
from app.services import project as project_service, run as run_service, workspace
from app.tools.tool_specs import TOOL_SPECS

_PROJECT_ID = Annotated[str, "The project's name."]


def resolve_existing_project(project_id: str) -> Path:
    """Loud on a project that is not in the workspace, rather than a later confusing miss."""
    pdir = workspace.resolve_project_dir(project_id)
    if not pdir.is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    return pdir


def run_workflow(
    project_id: str,
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    bindings: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    resolve_existing_project(project_id)
    run_id = run_service.start_run(
        project_id, version_id=version_id or None, limits=limits, bindings=bindings
    )
    status = run_service.read_run_status(project_id, run_id)["status"]
    return {"run_id": run_id, "status": status}


def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    resolve_existing_project(project_id)
    return run_service.read_run_status(project_id, run_id)


def describe_workflow(project_id: str) -> dict[str, Any]:
    resolve_existing_project(project_id)
    return project_service.describe_workflow(project_id)


# ── binding them onto an agent ───────────────────────────────────────────────

_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "run_workflow": run_workflow,
    "get_run_status": get_run_status,
    "describe_workflow": describe_workflow,
}

_SCHEMAS: dict[str, ToolInputSchema] = {
    "run_workflow": {
        "project_id": _PROJECT_ID,
        "version_id": Annotated[str, "Omit for the project's newest stored version."],
        "limits": Annotated[
            dict[str, int] | None,
            'Caps how many rows a stage READS: {"<stage id>": N}.',
        ],
        "bindings": Annotated[
            dict[str, dict[str, str]] | None,
            'The file each input stage reads for THIS run, merged over what the stage '
            'was authored with: {"<stage id>": {"path": "...", "format": "csv"}}.',
        ],
    },
    "get_run_status": {
        "project_id": _PROJECT_ID,
        "run_id": Annotated[str, "The run id run_workflow returned."],
    },
    "describe_workflow": {"project_id": _PROJECT_ID},
}

_LABELS = {
    "run_workflow": "Running the workflow",
    "get_run_status": "Checking the run",
    "describe_workflow": "Reading the workflow",
}


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
