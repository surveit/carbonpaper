"""Pure path facts with no domain knowledge — the repository root and the storage
home — needed by both `app.services` and `app.runtime` and so owned below both."""
from __future__ import annotations

import os
from pathlib import Path

# This module (app/core/paths.py) is two parents below the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Storage lives outside every checkout, so each worktree reads the one store instead
# of minting an empty one under whichever directory the process happened to start in.
def resolve_carbon_paper_home() -> Path:
    if os.name == "nt":
        return resolve_windows_home(os.environ.get("LOCALAPPDATA"), Path.home())
    return Path.home() / ".carbonpaper"


# Split out because `Path` binds to WindowsPath at construction, so a posix machine —
# this laptop, and CI — cannot execute the branch above at all. Taking both inputs as
# arguments puts the only real decision here, where any machine can test it.
def resolve_windows_home(local_app_data: str | None, home: Path) -> Path:
    """Windows keeps app state in LOCALAPPDATA; a dotfile in the profile rides roaming and backups."""
    base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
    return base / "carbonpaper"


CARBON_PAPER_HOME = resolve_carbon_paper_home()


def repo_root() -> Path:
    return REPO_ROOT
