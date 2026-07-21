"""workspace.py — the examples workspace: the projects storage root, name→directory
resolution, the named-schema data-model reader, and project enumeration + workflow
summaries. These back the editing agent's read tools and the status model. Uses the
tolerant loader (a malformed compiled file becomes an issue, not an exception) and
the node-review store; imports nothing from the web layer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.services import node_review
from app.services.loader import load_compiled_dir, stage_to_spec_dict

# The projects storage root, defined once: examples/<name>/ working copies live
# here. Both app.services and app.web read it; app.web.config re-exports it.
# CW_EXAMPLES_DIR overrides the default — used to point the workspace at a temp
# dir (a standalone CLI run, or a subprocess test); unset, it's the repo's
# examples/. Read once at import; in-process callers still pass examples_dir
# explicitly or monkeypatch this attribute.
EXAMPLES_DIR = (
    Path(os.environ["CW_EXAMPLES_DIR"])
    if os.environ.get("CW_EXAMPLES_DIR")
    else Path(__file__).resolve().parents[2] / "examples"
)


def resolve_project_dir(name: str, examples_dir: Path | None = None) -> Path:
    """Resolve a project NAME to its working-copy directory under the examples root,
    refusing a name that would escape it (the name comes from the model, so a
    `../…` value must not read or write outside the workspace)."""
    root = Path(examples_dir if examples_dir is not None else EXAMPLES_DIR).resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"invalid project id '{name}'")
    return candidate


def load_schemas(project_dir: Path) -> list[dict[str, Any]]:
    """Load the named-schema data model from <project_dir>/schemas/*.json — one schema
    object per file (the shape the schema writer emits). Returns [] if the project has
    no data model yet. A JSON parse error surfaces as an _error schema rather than
    dropping the file silently."""
    schemas_dir = Path(project_dir) / "schemas"
    if not schemas_dir.is_dir():
        return []
    schemas: list[dict[str, Any]] = []
    for schema_file in sorted(schemas_dir.glob("*.json")):
        try:
            doc = json.loads(schema_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            schemas.append({
                "name": schema_file.stem,
                "title": f"[JSON ERROR] {schema_file.name}",
                "kind": "reference",
                "notes": f"JSON parse error: {exc}",
                "_filename": schema_file.name,
                "_error": True,
            })
            continue
        if not doc:
            continue
        doc["_filename"] = schema_file.name
        schemas.append(doc)
    return schemas


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
