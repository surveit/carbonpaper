"""Discover committed WorkflowFile fixtures under app/seeds/data/*.json and import
them into the workspace through the app.services.project seam — never sqlite3,
app.core.persistence, or app.core.frames."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.errors import ProjectExistsError
from app.services.project import WorkflowFile, import_project

# The packaged fixtures directory: app/seeds/data/<name>.json, each a
# self-contained WorkflowFile document (see app/seeds/__init__.py for the
# layout).
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"


def discover_workflow_files(data_dir: Path | None = None) -> list[Path]:
    root = Path(data_dir) if data_dir is not None else _DEFAULT_DATA_DIR
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"))


def seed_all(
    *, data_dir: Path | None = None,
) -> list[str]:
    imported: list[str] = []
    for wf_path in discover_workflow_files(data_dir):
        wf = WorkflowFile.model_validate_json(wf_path.read_text(encoding="utf-8"))
        try:
            name = import_project(wf)
        except ProjectExistsError:
            continue
        imported.append(name)
    return imported


def seed_demo_data_if_enabled() -> list[str]:
    if os.environ.get("CARBON_PAPER_SEED_DEMO") != "1":
        return []
    return seed_all()
