"""The repository root — a pure path fact with no domain knowledge, needed by
both `app.services` and `app.runtime` and so owned below both."""
from __future__ import annotations

from pathlib import Path

# This module (app/core/paths.py) is two parents below the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    return REPO_ROOT
