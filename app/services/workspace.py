"""workspace.py — the projects workspace: the projects storage root, name→directory
resolution, and project enumeration + workflow summaries. These back the editing
agent's read tools and the status model. Uses the tolerant loader (a malformed
stage becomes an issue, not an exception); imports nothing from the web layer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core.paths import REPO_ROOT, repo_root as repo_root
from app.services.loader import exists as has_working_copy, load_stage_entries
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


def list_project_names() -> list[str]:
    """Sorted names of every project under the projects root that has an authored
    workflow (a stored working copy)."""
    root = projects_dir()
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir()
                  if child.is_dir() and has_working_copy(child.name))


def project_workflow_summary(project: str) -> dict[str, Any]:
    """A compact summary of one project's workflow: each stage's id, type, name and
    upstream input ids. Never returns full stage specs — that is `read_stage`'s job.
    A single malformed stage surfaces in `issues`."""
    stages: list[dict[str, Any]] = []
    issues: list[str] = []
    for entry in load_stage_entries(project):
        if entry.stage is None:
            issues.append(f"{entry.label}: {'; '.join(entry.issues)}")
            continue
        stages.append({
            "id": entry.stage.id,
            "type": entry.stage.type,
            "description": entry.stage.description,
            "inputs": [ref.id for ref in entry.stage.inputs],
        })
    return {"name": project, "stages": stages, "issues": issues}
