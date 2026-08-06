"""Regenerate the committed palm-oil-mill-osint WorkflowFile fixture.

Run `python -m app.seeds.capture_palm` when the source project changes. It replaces any
previously captured fixture first, so a re-run is never a stale mix, and bootstraps the
store itself — export_project reads the project's status even for a read-only export."""
from __future__ import annotations

import json
from pathlib import Path

from app.models.stages.input_data import InputDataStage
from app.seeds.bootstrap import ensure_store_configured
from app.services.project import Project, WorkflowFile, export_project
from app.services.workspace import set_projects_dir

# Unlike capture_lobbying, the source project lives in THIS repo's own
# (gitignored) examples/ dir, not a separate worktree.
_SOURCE_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
_SOURCE_PROJECT_NAME = "palm_oil_mill_osint"
_FIXTURE_PATH = Path(__file__).resolve().parent / "data" / f"{_SOURCE_PROJECT_NAME}.json"


def capture_palm_bundle() -> Path:
    """Export the source project as a committed WorkflowFile json at _FIXTURE_PATH; returns it."""
    ensure_store_configured()
    # This script is the ONE caller that reads a workspace other than its own:
    # it exports out of the repo's gitignored examples/ dir. It repoints the
    # process for the duration rather than passing a root down through the
    # service API — a dev-tool concern must not put a second workspace back
    # into the domain signatures.
    set_projects_dir(_SOURCE_EXAMPLES_DIR)
    _register_source_identity()
    wf = export_project(_SOURCE_PROJECT_NAME)
    _drop_machine_paths(wf)
    _FIXTURE_PATH.write_text(wf.to_json(), encoding="utf-8")
    return _FIXTURE_PATH


# The source project was authored in another checkout, so its identity record
# (the Project row export_project reads) is not in this store. Its own
# project.json carries the recorded facts; mirror them in, never invent them.
def _register_source_identity() -> None:
    """Save a Project record from the source's project.json when the store has none."""
    if Project.exists(_SOURCE_PROJECT_NAME):
        return
    meta_path = _SOURCE_EXAMPLES_DIR / _SOURCE_PROJECT_NAME / "project.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    Project(
        id=_SOURCE_PROJECT_NAME,
        title=meta["title"],
        model=meta["model"],
        source=meta["source"],
        authored_at=meta["created_at"],
    ).save()


# The committed fixture must carry no machine-specific paths; the sample CSVs
# ship as sibling files under app/seeds/data/ instead (the same convention as
# the lobbying fixture, whose connector declares no path).
def _drop_machine_paths(wf: WorkflowFile) -> None:
    """Drop each file connector's absolute params.path — the user binds a file at run time."""
    for stage in wf.stages:
        if isinstance(stage, InputDataStage):
            stage.connector.params.pop("path", None)


if __name__ == "__main__":
    written_to = capture_palm_bundle()
    print(f"wrote bundle to {written_to}")
