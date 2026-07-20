"""
compilation.py — the compile-writer service.

The compile MECHANISM (prose → draft workflow) lives in `app.compiler`. This service
owns persisting compile results to a project's disk layout: `write_methodology` writes
compiled stages to `<project_dir>/compiled/NN_<id>.json` + `methodology_raw.md`.
The regenerate_* functions are full-reset writers called by the editing agent and
generation subsystem."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.compiler import compile_methodology
from app.core.errors import RegenerateWithoutSnapshotError
from app.core.models.workflow import validate_workflow_draft
from app.services import versioning, workspace


def write_methodology(result: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    """Write the compiled workflow to a folder shaped like a project artifact:
      <out_dir>/compiled/NN_<id>.json   (one per stage, in order)
      <out_dir>/methodology_raw.md
      <out_dir>/compiler_result.json    (raw alongside cooked: full result, audit)
    Returns a manifest of written paths.

    Stages are written as JSON — the on-disk format the loader
    (app.services.loader) reads. The compiler emits raw draft dicts (which may
    be invalid; the manifest records that), so they are dumped as-is rather than
    round-tripped through the typed Stage model."""
    out_dir = Path(out_dir)
    compiled = out_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for i, stage in enumerate(result["stages"], start=1):
        sid = stage.get("id") or f"stage{i}"
        fname = f"{i:02d}_{sid}.json"
        fpath = compiled / fname
        fpath.write_text(
            json.dumps(stage, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written.append(str(fpath))

    raw_md = out_dir / "methodology_raw.md"
    raw_md.write_text(result.get("methodology_raw") or "", encoding="utf-8")

    # Raw-alongside-cooked: persist the full result (minus the bulky prompt echo)
    # so the compile is auditable and re-sliceable.
    audit = {
        "name": result.get("name"),
        "compiler_notes": result.get("compiler_notes"),
        "validation": result.get("validation"),
        "stages": result.get("stages"),
    }
    audit_path = out_dir / "compiler_result.json"
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "stage_files": written,
        "methodology_raw": str(raw_md),
        "audit": str(audit_path),
    }


def regenerate_workflow(result: dict[str, Any], project_dir: str | Path) -> dict[str, Any]:
    """Replace a project's whole compiled/ workflow with `result`'s stages: remove
    stale stage files a shrinking recompile would otherwise leave behind, then
    write the new set. The full-reset counterpart to write_methodology's plain
    write; the disk manipulation lives here in the compile-writer service, not in
    the caller."""
    compiled = Path(project_dir) / "compiled"
    if compiled.is_dir():
        for stale in compiled.glob("*.json"):
            stale.unlink()
    return write_methodology(result, project_dir)


def regenerate_workflow_from_conversation(
    name: str,
    conversation: str,
    confirm_overwrite: bool = False,
    examples_dir: Path | None = None,
) -> dict[str, Any]:
    """Rebuild a project's ENTIRE workflow from `conversation` — a full reset. If any
    node carries review work, raise RegenerateWithoutSnapshotError unless
    confirm_overwrite is set (in which case a version snapshot is taken first). The
    compiled draft is held to the same stage + graph validation every write obeys;
    an invalid result is returned, not written. Called by the editing agent's
    compile_workflow tool; lives here (not in the status model) because it drives the
    compiler."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    summary = workspace.project_workflow_summary(project_dir)
    has_review_work = any(s["review_state"] != "unreviewed" for s in summary["stages"])
    if has_review_work:
        if not confirm_overwrite:
            raise RegenerateWithoutSnapshotError(
                f"'{name}' has reviewed stages; re-call with confirm_overwrite=True to snapshot and regenerate."
            )
        existing = versioning.list_versions(project_dir)
        parent = existing[0].id if existing else None
        versioning.create_version_from_disk(
            project_dir,
            message=f"pre-regenerate snapshot of {name}",
            reviewer="agent",
            parent_version=parent,
        )
    result = compile_methodology(conversation, name)
    if result["validation"]:
        return {"ok": False, "issues": result["validation"]}
    draft_issues = validate_workflow_draft(result["stages"])
    if draft_issues:
        return {"ok": False, "issues": draft_issues}
    regenerate_workflow(result, project_dir)
    return {"ok": True, "stages": [stage["id"] for stage in result["stages"]]}
