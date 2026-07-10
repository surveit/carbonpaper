"""project_tools.py — in-process tools the editing agent calls to read and edit
ONE project's workflow. `make_project_tools(name)` returns callables closed over
that project's directory, so the agent for `<name>` sees only its own context
(plus cross-project `list_projects`). Each tool calls a service directly — no HTTP.

Every write tool validates before it writes and never fabricates a value: a
missing stage or column is a raised error, not an invented default."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.compiler import compile_methodology as compile_prose_to_workflow
from app.errors import RegenerateWithoutSnapshotError
from app.models import NODE_TYPES
from app.models.workflow import validate_workflow_draft
from app.services import stage_edit, versioning, workspace
from app.services.compilation import regenerate_workflow
from app.services.loader import load_compiled_dir, stage_to_json


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

    def describe_stage_types() -> dict[str, Any]:
        """Return the catalog of stage types and each one's handle-block contract:
        which handle field it uses (connector / llm / function / join / aggregate /
        queue / publish), that handle's required and optional keys, and whether the
        stage needs upstream inputs. Call this before add_stage to build a valid
        stage of a type not already in the workflow."""
        return dict(NODE_TYPES)

    def read_stage(stage_id: str) -> str:
        """Return the JSON of one stage from the loaded workflow. Read before editing."""
        stages = {c.stage.id: c.stage
                  for c in load_compiled_dir(project_dir / "compiled") if c.stage is not None}
        stage = stages.get(stage_id)
        if stage is None:
            raise ValueError(f"no stage '{stage_id}' in project '{name}'")
        return stage_to_json(stage)

    def edit_stage(stage_id: str, changes_json: str) -> dict[str, Any]:
        """Change specific fields of one stage. `changes_json` is a JSON object of
        ONLY the fields to change (a JSON Merge Patch): {"limit": 100} sets limit;
        {"llm": {"model": "opus"}} changes only llm.model and leaves the rest of the
        llm block intact; {"name": null} deletes a field. Fields you do not mention
        are preserved exactly — so you never alter anything you were not asked to.
        The result is validated first; if invalid, nothing is written and the issues
        are returned. A successful edit drops the node to 'edited_stale' (amber) for a
        human to re-approve — you cannot approve it yourself. You cannot change a
        stage's id this way."""
        result = stage_edit.patch_stage_spec(project_dir, stage_id, changes_json)
        return {
            "ok": result.ok,
            "issues": result.issues,
            "content_hash": result.content_hash,
            "state": result.state,
        }

    def add_stage(stage_json: str) -> dict[str, Any]:
        """Create a NEW stage in this project's workflow. `stage_json` is a full
        stage as JSON: id (new and unique — use edit_stage to change an existing
        one), name, type, the type's handle block (e.g. connector / llm / function),
        output_schema, and inputs. Every id listed in `inputs` must ALREADY be a
        stage in this workflow — a dangling input is rejected. If you are unsure of
        the type or its handle block, call describe_stage_types first (and read_stage
        on a similar existing stage for the output_schema / inputs shape). Validated
        first; if invalid, nothing is written and the issues are returned. The new
        node lands 'unreviewed' (amber) for a human to approve."""
        result = stage_edit.add_stage_spec(project_dir, stage_json)
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

    def compile_workflow(conversation: str, confirm_overwrite: bool = False) -> dict[str, Any]:
        """Regenerate this project's ENTIRE workflow from `conversation` — the whole
        conversation so far, passed verbatim (do NOT summarise or paraphrase). This
        OVERWRITES the whole workflow, so use it ONLY when the user explicitly asks
        to rebuild it from scratch — never for a tweak (use edit_stage / add_stage
        for those). Warn the user first: it replaces everything and takes a few
        minutes. If any node carries review work, pass confirm_overwrite=True (a
        version snapshot is taken first). An invalid result is returned, not written."""
        summary = workspace.project_workflow_summary(project_dir)
        has_review_work = any(s["review_state"] != "unreviewed" for s in summary["stages"])
        if has_review_work:
            if not confirm_overwrite:
                raise RegenerateWithoutSnapshotError(
                    f"'{name}' has reviewed stages; re-call with confirm_overwrite=True to snapshot and regenerate."
                )
            existing = versioning.list_versions(project_dir)
            parent = existing[0]["id"] if existing else None
            versioning.create_version(
                project_dir,
                message=f"pre-regenerate snapshot of {name}",
                reviewer="agent",
                parent_version=parent,
            )
        result = compile_prose_to_workflow(conversation, name)
        if result["validation"]:
            return {"ok": False, "issues": result["validation"]}
        # Hold the compiler's raw dicts to the same stage + graph validation every
        # other write obeys, so a compile can never persist an unloadable workflow.
        draft_issues = validate_workflow_draft(result["stages"])
        if draft_issues:
            return {"ok": False, "issues": draft_issues}
        regenerate_workflow(result, project_dir)
        return {"ok": True, "stages": [stage["id"] for stage in result["stages"]]}

    return [
        list_projects,
        describe_workflow,
        describe_stage_types,
        read_stage,
        edit_stage,
        add_stage,
        create_version,
        compile_workflow,
    ]
