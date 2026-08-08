"""workspace.py — the projects workspace: the projects storage root, name→directory
resolution, the named-schema data-model reader, and project enumeration + workflow
summaries. These back the editing agent's read tools and the status model. Uses the
tolerant loader (a malformed compiled file becomes an issue, not an exception);
imports nothing from the web layer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core.errors import InvalidJsonDocument
from app.core.json_document import read_json_document
from app.core.paths import REPO_ROOT, repo_root as repo_root
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
    """Point the process at its projects root. Called once at a process
    boundary: app startup, the seeds CLI entry point, or the autouse test
    fixture that gives each test its own tmp workspace."""
    global _projects_dir
    _projects_dir = Path(path)


def projects_dir() -> Path:
    """The projects storage root. Defaults to the repo's examples/ when nothing
    has configured one, so an in-process caller that never calls
    set_projects_dir() still resolves the real workspace."""
    return _projects_dir if _projects_dir is not None else REPO_ROOT / "examples"


def configure_projects_dir_from_env() -> None:
    """Apply CARBONPAPER_PROJECTS_DIR when it is set; a no-op otherwise. Called from a
    composition root (app.main's lifespan, the seeds CLI) — never at import
    time, so a test's set_projects_dir() is not silently overridden by whatever
    happened to be in the environment when the module first loaded."""
    configured = os.environ.get("CARBONPAPER_PROJECTS_DIR")
    if configured:
        set_projects_dir(Path(configured))


def resolve_project_dir(name: str) -> Path:
    """Resolve a project NAME to its working-copy directory under the projects
    root, refusing a name that would escape it (the name comes from the model, so a
    `../…` value must not read or write outside the workspace)."""
    root = projects_dir().resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"invalid project id '{name}'")
    return candidate


def resolve_run_dir(name: str, run_id: str) -> Path:
    """Where one run of a project lives. Services name the runs/ layout only here."""
    return resolve_project_dir(name) / "runs" / run_id


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
            doc = read_json_document(schema_file)
        except InvalidJsonDocument as exc:
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
    """Sorted names of every project directory under the projects root — a
    directory counts only if it contains a `compiled/` subdirectory (an authored
    workflow)."""
    root = projects_dir()
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and (child / "compiled").is_dir()
    )


def project_workflow_summary(project_dir: Path) -> dict[str, Any]:
    """A compact summary of one project's workflow: each stage's id, type, name and
    upstream input ids. Never returns full stage specs — that is `read_stage`'s job.
    A single malformed compiled file surfaces in `issues`."""
    compiled = load_compiled_dir(project_dir / "compiled")

    stages: list[dict[str, Any]] = []
    issues: list[str] = []
    for compiled_file in compiled:
        if compiled_file.stage is None:
            issues.append(f"{compiled_file.filename}: {'; '.join(compiled_file.issues)}")
            continue
        stage = compiled_file.stage
        stages.append({
            "id": stage.id,
            "type": stage.type,
            "description": stage.description,
            "inputs": [ref.id for ref in stage.inputs],
        })
    return {"name": project_dir.name, "stages": stages, "issues": issues}
