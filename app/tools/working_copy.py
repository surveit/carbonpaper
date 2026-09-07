"""The tools that edit a project's WORKING COPY, which only the MCP server offers.

The editing agent builds a draft and freezes that instead, so `save_version` means two
operations under one name — see app.tools.tool_specs. Retiring the working copy is what
would merge them (issue #357).
"""
from __future__ import annotations

from typing import Any, Callable

from app.services import project as project_service, versioning
from app.tools.shared import EditedStages, STAGE_TOOL_ERRORS, validate_project_exists


def save_working_copy_as_version(
    project_id: str, message: str, parent_version: str | None = None
) -> dict[str, Any]:
    validate_project_exists(project_id)
    try:
        if parent_version is not None:
            versioning.validate_version_exists(project_id, parent_version)
        version = project_service.save_working_copy_as_version(
            project_id, message=message, parent_version=parent_version
        )
    except STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}
    return {"ok": True, "issues": [], "version_id": version.version_id}


def catch_stage_edit_refusals(edit: Callable[[], EditedStages]) -> EditedStages:
    """A workflow too broken to load is an issue the caller can read, not a transport error."""
    try:
        return edit()
    except STAGE_TOOL_ERRORS as exc:
        return EditedStages(ok=False, edited=[], issues=[str(exc)])
