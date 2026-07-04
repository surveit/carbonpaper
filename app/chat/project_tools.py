"""project_tools.py — in-process tools the editing agent calls to read and edit
ONE project's workflow. `make_project_tools(name)` returns callables closed over
that project's directory, so the agent for `<name>` sees only its own context
(plus cross-project `list_projects`). Each tool calls a service directly — no HTTP.

Every write tool validates before it writes and never fabricates a value: a
missing stage or column is a raised error, not an invented default."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.services import stage_edit, versioning, workspace
from app.services.loader import find_stage_file


def make_project_tools(name: str, *, examples_dir: Path) -> list[Callable[..., Any]]:
    project_dir = examples_dir / name

    def list_projects() -> list[str]:
        """List the names of every authored project in the workspace."""
        return workspace.list_project_names(examples_dir)

    def describe_workflow() -> dict[str, Any]:
        """Summarize this project's workflow: each stage's id, type, name, upstream
        input ids, and review state. Read this before editing so you know the
        current shape. Does not return full stage specs — use read_stage for one."""
        return workspace.project_workflow_summary(project_dir)

    def read_stage(stage_id: str) -> str:
        """Return the on-disk JSON of one stage. Read a stage before editing it."""
        target = find_stage_file(project_dir / "compiled", stage_id)
        if target is None:
            raise ValueError(f"no stage '{stage_id}' in project '{name}'")
        return target.read_text(encoding="utf-8")

    def edit_stage(stage_id: str, spec_json: str) -> dict[str, Any]:
        """Replace one stage's spec with `spec_json` (the full stage as JSON). The
        spec is validated first; if invalid, nothing is written and the issues are
        returned. A successful edit drops the node to 'edited_stale' (amber) for a
        human to re-approve — you cannot approve it yourself. The `id` in the JSON
        must equal `stage_id`."""
        result = stage_edit.edit_stage_spec(project_dir, stage_id, spec_json)
        return {
            "ok": result.ok,
            "issues": result.issues,
            "content_hash": result.content_hash,
            "state": result.state,
        }

    def create_version(message: str) -> dict[str, Any]:
        """Snapshot the current compiled/ (+ schemas/ if present) as an immutable
        version, freezing review coverage. Do this before regenerating from scratch
        so prior work is never lost. Recorded with reviewer='agent'."""
        existing = versioning.list_versions(project_dir)
        parent = existing[0]["id"] if existing else None
        return versioning.create_version(
            project_dir, message=message, reviewer="agent", parent_version=parent
        )

    return [list_projects, describe_workflow, read_stage, edit_stage, create_version]
