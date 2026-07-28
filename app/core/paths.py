"""The repository root — a pure path fact with no domain knowledge, needed by
both `app.services` and `app.runtime` and so owned below both."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

# This module (app/core/paths.py) is two parents below the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    """The repository root the run seams resolve connector/table-ref relative
    paths against — a single owned derivation (see REPO_ROOT) rather than each
    caller re-deriving it from its own `__file__`."""
    return REPO_ROOT


def strip_directories(recorded_path: str) -> str:
    """Basename of a path string recorded on any platform: splits on `/` and `\\` alike."""
    return PureWindowsPath(recorded_path).name
