"""Pure path facts with no domain knowledge — the repository root and the storage
home — needed by both `app.services` and `app.runtime` and so owned below both."""
from __future__ import annotations

from pathlib import Path

# This module (app/core/paths.py) is two parents below the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Storage lives outside every checkout, so each worktree reads the one store instead
# of minting an empty one under whichever directory the process happened to start in.
CARBON_PAPER_HOME = Path.home() / ".carbonpaper"


def repo_root() -> Path:
    return REPO_ROOT
