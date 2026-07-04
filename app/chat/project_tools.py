"""project_tools.py — in-process tools the editing agent calls to read and edit
ONE project's workflow. `make_project_tools(name)` returns callables closed over
that project's directory, so the agent for `<name>` sees only its own context
(plus cross-project `list_projects`). Each tool calls a service directly — no HTTP.

Every write tool validates before it writes and never fabricates a value: a
missing stage or column is a raised error, not an invented default."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from app.compiler import compile_methodology as compile_prose_to_workflow, read_input
from app.errors import RegenerateWithoutSnapshotError
from app.services import stage_edit, versioning, workspace
from app.services.compilation import write_methodology
from app.services.loader import find_stage_file

# read_section caps at this many collected lines; grep_doc at this many matches
# — bounds the agent's context intake from a document that otherwise never
# lands in context as full text (see module docstring: doc stays on disk).
_MAX_SECTION_LINES = 400
_MAX_GREP_MATCHES = 50


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

    def fetch_document(src_path: str) -> dict[str, Any]:
        """Copy a local source document into this project's source/ folder and
        return a handle: its on-disk path plus a cheap outline (byte size, line
        count, markdown headings) — never the body. Then read bounded slices with
        read_section / grep_doc, or compile it with compile_workflow. Raises if the
        path does not exist (it is not guessed or treated as a URL)."""
        src = Path(src_path)
        if not src.is_file():
            raise ValueError(f"no document at '{src_path}' (fetch_document takes a local file path)")
        source_dir = project_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        dest = source_dir / src.name
        shutil.copyfile(src, dest)
        lines = dest.read_text(encoding="utf-8", errors="replace").splitlines()
        headings = [ln for ln in lines if ln.lstrip().startswith("#")]
        return {"path": str(dest), "bytes": dest.stat().st_size, "lines": len(lines), "headings": headings}

    def read_section(doc_path: str, heading: str) -> str:
        """Return the lines under the first heading containing `heading`, up to the
        next heading of the same or higher level. Capped at 400 lines."""
        lines = Path(doc_path).read_text(encoding="utf-8", errors="replace").splitlines()
        start = next(
            (
                i
                for i, ln in enumerate(lines)
                if ln.lstrip().startswith("#") and heading.lower() in ln.lower()
            ),
            None,
        )
        if start is None:
            raise ValueError(f"no heading matching '{heading}' in {doc_path}")
        level = len(lines[start]) - len(lines[start].lstrip("#").lstrip())
        collected = [lines[start]]
        for ln in lines[start + 1 :]:
            if ln.lstrip().startswith("#") and (len(ln) - len(ln.lstrip("#").lstrip())) <= level:
                break
            collected.append(ln)
            if len(collected) >= _MAX_SECTION_LINES:
                break
        return "\n".join(collected)

    def grep_doc(doc_path: str, query: str) -> str:
        """Return up to 50 lines of the document matching `query` (case-insensitive),
        each prefixed with its 1-based line number."""
        needle = query.lower()
        out: list[str] = []
        for lineno, ln in enumerate(
            Path(doc_path).read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if needle in ln.lower():
                out.append(f"{lineno}: {ln}")
                if len(out) >= _MAX_GREP_MATCHES:
                    break
        return "\n".join(out)

    def compile_workflow(doc_path: str, confirm_overwrite: bool = False) -> dict[str, Any]:
        """Compile a source document (already on disk — pass its path) into this
        project's workflow, writing every stage into compiled/ as unreviewed
        (amber). This OVERWRITES the current compiled/. If any node carries review
        work (approved/edited/rejected), pass confirm_overwrite=True; a version
        snapshot is taken first so nothing is lost. If the compiler reports
        validation issues, nothing is written and the issues are returned."""
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
        text = read_input(doc_path)
        result = compile_prose_to_workflow(text, name)
        if result["validation"]:
            return {"ok": False, "issues": result["validation"]}
        write_methodology(result, project_dir)
        return {"ok": True, "stages": [stage["id"] for stage in result["stages"]]}

    return [
        list_projects,
        describe_workflow,
        read_stage,
        edit_stage,
        create_version,
        fetch_document,
        read_section,
        grep_doc,
        compile_workflow,
    ]
