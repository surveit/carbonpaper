"""Regenerate the committed lobbying-issue-triage WorkflowFile fixture.

Run `python -m app.seeds.capture_lobbying` when the source demo changes. It replaces any
previously captured fixture first, so a re-run is never a stale mix, and bootstraps the
store itself — export_project reads the project's status even for a read-only export."""
from __future__ import annotations

from pathlib import Path

from app.seeds.bootstrap import ensure_store_configured
from app.services.project import export_project

# The demo lives on the mcp-authoring branch of a separate worktree, checked
# out locally at this path. Only this capture script reads from it.
_SOURCE_REPO_ROOT = Path("C:/journalism_sprint/prototype_one_mcp_wt")
_SOURCE_PROJECT_NAME = "lobbying_issue_triage"
_FIXTURE_PATH = Path(__file__).resolve().parent / "data" / f"{_SOURCE_PROJECT_NAME}.json"


def capture_lobbying_bundle() -> Path:
    """Export the source project as a committed WorkflowFile json at
    _FIXTURE_PATH (replacing it if already present); returns that path.
    Read-only on the source."""
    ensure_store_configured()
    wf = export_project(_SOURCE_PROJECT_NAME, examples_dir=_SOURCE_REPO_ROOT / "examples")
    _FIXTURE_PATH.write_text(wf.model_dump_json(indent=2), encoding="utf-8")
    return _FIXTURE_PATH


if __name__ == "__main__":
    written_to = capture_lobbying_bundle()
    print(f"wrote bundle to {written_to}")
