"""
versioning.py — immutable, committable snapshots of a methodology DAG.

A "version" is a frozen copy of a methodology's authored artifacts — its
`compiled/` stages and `schemas/` data model — taken at a point in time, plus a
`version.json` recording who created it, why, its parent, and the approval coverage
AT creation time. Runs are pinned to a version and read its snapshot dir, so a run is
reproducible against the exact DAG it executed, never "whatever the working copy
happened to be".

Layout:
    examples/<methodology>/versions/<version_id>/
        compiled/<id>.yaml      # copy of the working compiled/ at creation time
        schemas/<...>.yaml      # copy of the working schemas/ at creation time (if any)
        version.json            # {id, created_at, parent_version, message,
                                #  reviewer, coverage}

`versions/` is a DURABLE reviewable artifact and is committable (not gitignored)
— it is the record of what was believed and run, the git-model "one canonical
copy" the runner reads from.

`version_id` uses the SAME timestamp scheme as run ids
(datetime.now().strftime('%Y%m%dT%H%M%S')) so versions and runs sort and read
consistently.

Dependency note: this module may import app.services.node_review (to freeze coverage) but
nothing from app.runtime or app.compiler. The stage loader here mirrors
app/runtime/runner._load_stages so a version's stages load identically to the
working copy's.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.services import node_review


def versions_dir(methodology_dir: Path) -> Path:
    """examples/<methodology>/versions/ — the parent of all version snapshots."""
    return Path(methodology_dir) / "versions"


def _load_stages_from(compiled_dir: Path) -> list[dict[str, Any]]:
    """Load compiled stage YAMLs from a directory, sorted by filename. Mirrors
    runner._load_stages exactly (same glob, same sort, same skip-empty) so a
    snapshot's stages load identically to the working copy's. Does NOT inject the
    loader bookkeeping keys (_filename/_order) — callers that need them add them;
    the canonical hash ignores them regardless."""
    stages: list[dict[str, Any]] = []
    if not compiled_dir.is_dir():
        return stages
    for f in sorted(compiled_dir.glob("*.yaml")):
        with f.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data:
            stages.append(data)
    return stages


def load_version_stages(methodology_dir: Path, version_id: str) -> list[dict[str, Any]]:
    """Load the compiled stages frozen in versions/<version_id>/compiled/. Fails
    loudly if the version dir is missing rather than falling back to the working
    copy (a run pinned to a version must read THAT version)."""
    vdir = versions_dir(methodology_dir) / version_id
    if not vdir.is_dir():
        raise FileNotFoundError(
            f"No version '{version_id}' for methodology at {methodology_dir} "
            f"(expected {vdir})"
        )
    return _load_stages_from(vdir / "compiled")


def load_version_meta(methodology_dir: Path, version_id: str) -> dict[str, Any]:
    """Read versions/<version_id>/version.json. Fails loudly if absent."""
    meta_path = versions_dir(methodology_dir) / version_id / "version.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No version.json at {meta_path}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def list_versions(methodology_dir: Path) -> list[dict[str, Any]]:
    """All versions for a methodology, NEWEST-FIRST, each as its parsed
    version.json. Skips any directory lacking a readable version.json rather than
    fabricating metadata for it (a half-written snapshot is simply not listed)."""
    vroot = versions_dir(methodology_dir)
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


def create_version(
    methodology_dir: Path,
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
    methodology with no schema library still versions cleanly (the absence is
    truthful, not an error)."""
    methodology_dir = Path(methodology_dir)
    compiled_src = methodology_dir / "compiled"
    if not compiled_src.is_dir():
        raise FileNotFoundError(
            f"Cannot create a version: no compiled/ DAG at {compiled_src}"
        )

    version_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    vdir = versions_dir(methodology_dir) / version_id
    if vdir.exists():
        raise FileExistsError(
            f"Version dir already exists: {vdir} (two versions created within one second)"
        )
    vdir.mkdir(parents=True, exist_ok=False)

    # Freeze the artifacts. copytree the working compiled/ (required) and
    # schemas/ (optional) verbatim — one canonical copy, git-model.
    shutil.copytree(compiled_src, vdir / "compiled")
    schemas_src = methodology_dir / "schemas"
    if schemas_src.is_dir():
        shutil.copytree(schemas_src, vdir / "schemas")

    # Freeze coverage from the just-written snapshot's stages.
    stages = _load_stages_from(vdir / "compiled")
    decisions = node_review.load_node_decisions(methodology_dir)
    coverage = node_review.coverage_for(stages, decisions)

    meta: dict[str, Any] = {
        "id": version_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "parent_version": parent_version,
        "message": message,
        "reviewer": reviewer,
        "coverage": coverage,
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
]
