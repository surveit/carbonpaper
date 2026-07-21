"""capture_lobbying.py — regenerate the committed lobbying-issue-triage
WorkflowFile fixture at app/seeds/data/lobbying_issue_triage.json from its
source project, through the app.services.project seam (export_project).

Run as a script whenever the source demo changes:
    python -m app.seeds.capture_lobbying

Reads only from the source worktree named below; writes only into this repo's
app/seeds/data/, replacing any previously captured fixture there first — so
re-running the script always reflects the source project's current state,
never a stale mix of old and new content.

export_project reads the source project's status through app.services.project,
which touches the document store (its version count) even for a read-only
export — so this standalone script has no app.main lifespan to configure one,
and bootstraps it itself, the same way the `python -m app.seeds` CLI does."""
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
