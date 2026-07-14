"""
versioning.py — immutable, committable snapshots of a workflow.

A "version" is a frozen copy of a project's authored artifacts — its
`compiled/` stages and `schemas/` data model — taken at a point in time, plus a
`version.json` recording who created it, why, its parent, and the approval coverage
AT creation time. Runs are pinned to a version and read its snapshot dir, so a run is
reproducible against the exact workflow it executed, never "whatever the working copy
happened to be".

Layout:
    <project>/versions/<version_id>/
        compiled/<id>.json      # copy of the working compiled/ at creation time
        schemas/<...>           # copy of the working schemas/ at creation time (if any)
        version.json            # {id, created_at, parent_version, message,
                                #  reviewer, coverage, published, published_at}

`published` records human approval; runs pin published versions only.

`versions/` is a DURABLE on-disk record of what was believed and run — the
"one canonical copy" the runner reads from. It lives under the project dir
alongside `runs/` (both are per-project working data, not source).

`version_id` uses the SAME timestamp scheme as run ids
(datetime.now().strftime('%Y%m%dT%H%M%S')) so versions and runs sort and read
consistently.

Dependency note: this module may import app.services.node_review (to freeze coverage) and
app.core.models, but nothing from app.runtime or app.compiler. Version snapshots are
parsed through the same strict loader as the working copy (app.services.loader),
so a version's stages load identically to the working copy's.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.models import Stage
from app.services.loader import (
    load_compiled_dir,
    load_workflow,
    stage_to_spec_dict,
)
from app.services import node_review


def versions_dir(project_dir: Path) -> Path:
    """examples/<project>/versions/ — the parent of all version snapshots."""
    return Path(project_dir) / "versions"


def _load_stages_from(compiled_dir: Path) -> list[dict[str, Any]]:
    """Compiled stages of a snapshot as canonical spec dicts, to freeze approval
    coverage at version-creation time (node_review speaks dicts). Routes through
    the shared loader so the on-disk format lives in exactly one place; runs
    instead load a version's stages as typed Stages via load_version_stages."""
    return [stage_to_spec_dict(entry.stage)
            for entry in load_compiled_dir(compiled_dir)
            if entry.stage is not None]


def load_version_stages(project_dir: Path, version_id: str) -> list[Stage]:
    """Load the compiled stages frozen in versions/<version_id>/compiled/ as
    typed Stage objects, through the same strict loader the runner uses for a
    working copy (app.services.loader) — an invalid snapshot raises
    WorkflowLoadError rather than executing. Fails loudly if the version dir
    is missing rather than falling back to the working copy (a run pinned to a
    version must read THAT version)."""
    vdir = versions_dir(project_dir) / version_id
    if not vdir.is_dir():
        raise FileNotFoundError(
            f"No version '{version_id}' for project at {project_dir} "
            f"(expected {vdir})"
        )
    return load_workflow(vdir)


def load_version_meta(project_dir: Path, version_id: str) -> dict[str, Any]:
    """Read versions/<version_id>/version.json. Fails loudly if absent."""
    meta_path = versions_dir(project_dir) / version_id / "version.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No version.json at {meta_path}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def list_versions(project_dir: Path) -> list[dict[str, Any]]:
    """All versions for a project, NEWEST-FIRST, each as its parsed
    version.json. Skips any directory lacking a readable version.json rather than
    fabricating metadata for it (a half-written snapshot is simply not listed)."""
    vroot = versions_dir(project_dir)
    if not vroot.is_dir():
        return []
    metas: list[dict[str, Any]] = []
    for d in vroot.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "version.json"
        if not meta_path.exists():
            continue
        try:
            metas.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    # Newest-first. version ids are strftime timestamps, so a reverse string sort
    # on id is chronological; fall back to created_at if an id is somehow absent.
    metas.sort(key=lambda m: str(m.get("id") or m.get("created_at") or ""),
               reverse=True)
    return metas


def version_is_published(meta: dict[str, Any]) -> bool:
    """Whether this version carries human approval. Versions written before the
    `published` flag existed were created only by the human "Create version"
    action — the act publishing now records — so a missing key reads as
    published. New metas always carry the key (False until published)."""
    if "published" not in meta:
        return True
    return bool(meta["published"])


def publish_version(project_dir: Path, version_id: str, *, reviewer: str) -> dict[str, Any]:
    """Record human approval on one version: stamp published/published_at/
    published_by into its version.json and return the updated meta. Idempotent —
    an already-published version is returned unchanged (the first stamp wins).
    Publishing touches metadata only, never stage content."""
    meta = load_version_meta(project_dir, version_id)
    if version_is_published(meta):
        return meta
    meta["published"] = True
    meta["published_at"] = datetime.now().isoformat(timespec="seconds")
    meta["published_by"] = reviewer
    meta_path = versions_dir(project_dir) / version_id / "version.json"
    meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return meta


def create_version(
    project_dir: Path,
    *,
    message: str,
    reviewer: str,
    parent_version: str | None = None,
) -> dict[str, Any]:
    """Snapshot the working copy's compiled/ + schemas/ into a new
    versions/<version_id>/ and write version.json with coverage frozen at creation
    time. Returns the version.json dict.

    Coverage is computed from the SNAPSHOT's stages against the live
    node_decisions store, so the recorded coverage is exactly what was believed
    about these specs at this instant. schemas/ is copied if it exists; a
    project with no schema library still versions cleanly (the absence is
    truthful, not an error).

    The working copy is strict-loaded first, through the same loader the runner
    uses; if it is not a valid workflow this raises WorkflowLoadError and writes
    nothing. Every version is therefore a loadable workflow, from this seam or
    any other."""
    project_dir = Path(project_dir)
    compiled_src = project_dir / "compiled"
    if not compiled_src.is_dir():
        raise FileNotFoundError(
            f"Cannot create a version: no compiled/ workflow at {compiled_src}"
        )

    # Validate BEFORE writing anything: a version is, by invariant, a loadable
    # workflow. On failure load_workflow raises WorkflowLoadError and we
    # snapshot nothing — an invalid workflow can never be immortalised as a
    # version. (The run-path strict load then only guards on-disk corruption of
    # an already-valid snapshot.)
    load_workflow(project_dir)

    version_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    vdir = versions_dir(project_dir) / version_id
    if vdir.exists():
        raise FileExistsError(
            f"Version dir already exists: {vdir} (two versions created within one second)"
        )
    vdir.mkdir(parents=True, exist_ok=False)

    # Freeze the artifacts. copytree the working compiled/ (required) and
    # schemas/ (optional) verbatim — one canonical copy, git-model.
    shutil.copytree(compiled_src, vdir / "compiled")
    schemas_src = project_dir / "schemas"
    if schemas_src.is_dir():
        shutil.copytree(schemas_src, vdir / "schemas")

    # Freeze coverage from the just-written snapshot's stages.
    stages = _load_stages_from(vdir / "compiled")
    decisions = node_review.load_node_decisions(project_dir)
    coverage = node_review.coverage_for(stages, decisions)

    meta: dict[str, Any] = {
        "id": version_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "parent_version": parent_version,
        "message": message,
        "reviewer": reviewer,
        "coverage": coverage,
        "published": False,
        "published_at": None,
    }
    (vdir / "version.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )
    return meta


__all__ = [
    "versions_dir",
    "list_versions",
    "load_version_meta",
    "load_version_stages",
    "create_version",
    "version_is_published",
    "publish_version",
]
