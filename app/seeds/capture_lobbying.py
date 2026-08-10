"""Regenerate the committed lobbying-issue-triage WorkflowFile fixture.

Run `python -m app.seeds.capture_lobbying` when the source demo changes. It replaces any
previously captured fixture first, so a re-run is never a stale mix, and bootstraps the
store itself — export_project reads the project's status even for a read-only export."""
from __future__ import annotations

from pathlib import Path

from app.seeds.bootstrap import ensure_store_configured
from app.services.project import export_project
from app.services.workspace import set_projects_dir

# The demo lives on the mcp-authoring branch of a separate worktree, checked
# out locally at this path. Only this capture script reads from it.
_SOURCE_REPO_ROOT = Path("C:/journalism_sprint/prototype_one_mcp_wt")
_SOURCE_PROJECT_NAME = "lobbying_issue_triage"
_FIXTURE_PATH = Path(__file__).resolve().parent / "data" / f"{_SOURCE_PROJECT_NAME}.json"


def capture_lobbying_bundle() -> Path:
    ensure_store_configured()
    # This script is the ONE caller that reads a workspace other than its own:
    # it exports out of a separate local checkout. It repoints the process for
    # the duration rather than passing a root down through the service API —
    # a dev-tool concern must not put a second workspace back into the domain
    # signatures.
    set_projects_dir(_SOURCE_REPO_ROOT / "examples")
    wf = export_project(_SOURCE_PROJECT_NAME)
    _FIXTURE_PATH.write_text(wf.to_json(), encoding="utf-8")
    return _FIXTURE_PATH


if __name__ == "__main__":
    written_to = capture_lobbying_bundle()
    print(f"wrote bundle to {written_to}")
