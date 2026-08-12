"""workspace.py — the projects workspace: the projects storage root, name→directory
resolution, the named-schema data-model reader, and project enumeration + workflow
summaries. These back the editing agent's read tools and the status model. Uses the
tolerant loader (a malformed compiled file becomes an issue, not an exception);
imports nothing from the web layer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.paths import REPO_ROOT, repo_root as repo_root
from app.models import StageType
from app.services.loader import load_compiled_dir
# The projects storage root: <root>/<name>/ working copies live here. There is
# exactly ONE in a running process — the app does not serve multiple
# workspaces, so no function takes a root as an argument. Configure it the way
# the document store is configured (app.core.persistence.configure_store): call
# set_projects_dir() once at a process boundary, and read it through
# projects_dir() everywhere else.
#
# It is a FUNCTION, not a module constant, deliberately: a constant is captured
# by value at import (`from ... import PROJECTS_DIR`), so repointing it would
# have to reach into every module that imported a copy. A live read has one
# source of truth.
_projects_dir: Path | None = None


def set_projects_dir(path: Path) -> None:
    global _projects_dir
    _projects_dir = Path(path)


def projects_dir() -> Path:
    return _projects_dir if _projects_dir is not None else REPO_ROOT / "examples"


def configure_projects_dir_from_env() -> None:
    """Call from a composition root, never at import time — it would override a later setter."""
    configured = os.environ.get("CARBON_PAPER_PROJECTS_DIR")
    if configured:
        set_projects_dir(Path(configured))


def resolve_project_dir(name: str) -> Path:
    root = projects_dir().resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"invalid project id '{name}'")
    return candidate


def resolve_run_dir(name: str, run_id: str) -> Path:
    return resolve_project_dir(name) / "runs" / run_id


def load_schemas(project_dir: Path) -> list[dict[str, Any]]:
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


def list_project_names() -> list[str]:
    root = projects_dir()
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and (child / "compiled").is_dir()
    )


class StageSummary(BaseModel):
    id: str
    type: StageType
    description: str
    inputs: list[str]


class WorkflowSummary(BaseModel):
    name: str
    stages: list[StageSummary]
    # One per compiled file that would not parse — the stage is absent from `stages`
    # rather than standing in the list as a half-read one.
    issues: list[str]


def project_workflow_summary(project_dir: Path) -> WorkflowSummary:
    compiled = load_compiled_dir(project_dir / "compiled")

    stages: list[StageSummary] = []
    issues: list[str] = []
    for compiled_file in compiled:
        if compiled_file.stage is None:
            issues.append(f"{compiled_file.filename}: {'; '.join(compiled_file.issues)}")
            continue
        stage = compiled_file.stage
        stages.append(StageSummary(
            id=stage.id,
            type=stage.type,
            description=stage.description,
            inputs=[ref.id for ref in stage.inputs],
        ))
    return WorkflowSummary(name=project_dir.name, stages=stages, issues=issues)
