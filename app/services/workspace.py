"""workspace.py — read-only helpers for enumerating projects and summarizing one
project's workflow (stage ids/types/inputs + per-node review state). These back
the editing agent's read tools. Uses the tolerant loader (a malformed compiled
file becomes an issue, not an exception) and the node-review store; imports
nothing from the web layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services import node_review
from app.services.loader import load_compiled_dir, stage_to_spec_dict


def list_project_names(examples_dir: Path) -> list[str]:
    """Sorted names of every project under `examples_dir` — a directory counts
    only if it contains a `compiled/` subdirectory (an authored workflow)."""
    if not examples_dir.is_dir():
        return []
    return sorted(
        child.name
        for child in examples_dir.iterdir()
        if child.is_dir() and (child / "compiled").is_dir()
    )


def project_workflow_summary(project_dir: Path) -> dict[str, Any]:
    """A compact summary of one project's workflow: each stage's id, type, name,
    upstream input ids, and review state. Never returns full stage specs — that is
    `read_stage`'s job. A single malformed compiled file surfaces in `issues`."""
    compiled = load_compiled_dir(project_dir / "compiled")
    decisions = node_review.load_node_decisions(project_dir)

    stages: list[dict[str, Any]] = []
    issues: list[str] = []
    for compiled_file in compiled:
        if compiled_file.stage is None:
            issues.append(f"{compiled_file.filename}: {'; '.join(compiled_file.issues)}")
            continue
        stage = compiled_file.stage
        spec = stage_to_spec_dict(stage)
        state = node_review.approval_state_for(spec, decisions)["state"]
        stages.append({
            "id": stage.id,
            "type": stage.type,
            "name": stage.name,
            "inputs": [ref.id for ref in stage.inputs],
            "review_state": state,
        })
    return {"name": project_dir.name, "stages": stages, "issues": issues}
