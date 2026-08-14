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

from app.core.paths import CARBON_PAPER_HOME
from app.models import StageType
from app.services.loader import load_stage_entries
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
    return _projects_dir if _projects_dir is not None else CARBON_PAPER_HOME / "examples"


def configure_projects_dir_from_env() -> None:
    """Call from a composition root, never at import time — it would override a later setter."""
    configured = os.environ.get("CARBON_PAPER_PROJECTS_DIR")
    if configured:
        set_projects_dir(Path(configured))


# The escape guard, stated on the ID rather than on the path it resolves to: a project
# id is also the prefix of every document key (`{project_id}/{local_id}`), so a separator
# in it splits the wrong way there too, where there is no directory to check it against.
def validate_project_id(project_id: str) -> str:
    """One path segment — no separator, and never a relative-directory name."""
    if not project_id or project_id.startswith(".") or "/" in project_id or "\\" in project_id:
        raise ValueError(f"invalid project id '{project_id}'")
    return project_id


def resolve_project_dir(project_id: str) -> Path:
    return projects_dir().resolve() / validate_project_id(project_id)


def resolve_runs_dir(project_id: str) -> Path:
    """Where this project's runs go — the runtime is handed this, never the project."""
    return resolve_project_dir(project_id) / "runs"


def resolve_run_dir(project_id: str, run_id: str) -> Path:
    return resolve_runs_dir(project_id) / run_id


# An eval's subset run writes beside the production runs rather than among them. This
# directory's NAME is load-bearing: the executor records a manifest's `area` as its run
# dir's parent name, so renaming it re-keys every eval run manifest in the store.
def resolve_eval_runs_dir(project_id: str) -> Path:
    return resolve_project_dir(project_id) / "eval_run"


def resolve_eval_run_dir(project_id: str, run_id: str) -> Path:
    return resolve_eval_runs_dir(project_id) / run_id


# Keys this reader injects onto a loaded schema dict for its own bookkeeping —
# never part of the spec, so a writer must strip them before validating or
# persisting (the spec model is `extra="forbid"`).
LOADER_BOOKKEEPING_KEYS: set[str] = {"_filename", "_order", "_error"}


def load_schemas(project_id: str) -> list[dict[str, Any]]:
    schemas_dir = resolve_project_dir(project_id) / "schemas"
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


class StageSummary(BaseModel):
    id: str
    type: StageType
    description: str
    inputs: list[str]


class WorkflowSummary(BaseModel):
    name: str
    stages: list[StageSummary]
    # One per stored stage that would not parse — the stage is absent from `stages`
    # rather than standing in the list as a half-read one.
    issues: list[str]


def project_workflow_summary(project_id: str) -> WorkflowSummary:
    stages: list[StageSummary] = []
    issues: list[str] = []
    for compiled_file in load_stage_entries(project_id):
        if compiled_file.stage is None:
            issues.append(f"{compiled_file.label}: {'; '.join(compiled_file.issues)}")
            continue
        stage = compiled_file.stage
        stages.append(StageSummary(
            id=stage.id,
            type=stage.type,
            description=stage.description,
            inputs=[ref.id for ref in stage.inputs],
        ))
    return WorkflowSummary(name=project_id, stages=stages, issues=issues)
