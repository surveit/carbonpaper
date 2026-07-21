"""seed.py — the seed API: discover committed example WorkflowFile fixtures
under app/seeds/data/*.json and import them into the workspace through the
project export/import seam (app.services.project). app/seeds/__main__.py (the
`python -m app.seeds` CLI) and app.main's opt-in CW_SEED_DEMO startup hook
are both thin callers of this module; neither carries seeding logic of its
own.

Reaches the seam via app.services.project only — no sqlite3,
app.core.persistence, or app.core.frames (see app/seeds/__init__.py)."""
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
    """The WorkflowFile json fixture paths under `data_dir` (default: the
    packaged app/seeds/data/), sorted for a deterministic import order. []
    when `data_dir` doesn't exist (a truthful "no fixtures here", not an
    error)."""
    root = Path(data_dir) if data_dir is not None else _DEFAULT_DATA_DIR
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"))


def seed_all(
    *, examples_dir: Path | None = None, data_dir: Path | None = None,
) -> list[str]:
    """Import every discovered WorkflowFile fixture into the workspace at
    `examples_dir` (default: the real examples/ root) via import_project —
    the same seam a UI or CLI caller would use, never generation.

    Import-if-absent only: a fixture whose project already exists is left
    exactly as it is (the resulting ProjectExistsError is caught and that
    fixture is skipped — never overwritten). Returns the names of projects
    actually imported; a skipped one is excluded.

    Never fabricates: a malformed fixture makes WorkflowFile.model_validate_json
    or import_project raise, and that raise is not caught here — it propagates
    to the caller."""
    imported: list[str] = []
    for wf_path in discover_workflow_files(data_dir):
        wf = WorkflowFile.model_validate_json(wf_path.read_text(encoding="utf-8"))
        try:
            name = import_project(wf, examples_dir=examples_dir)
        except ProjectExistsError:
            continue
        imported.append(name)
    return imported


def seed_demo_data_if_enabled(examples_dir: Path | None = None) -> list[str]:
    """The CW_SEED_DEMO=1 startup hook: when the env var is exactly "1",
    seed `examples_dir` (default: the real workspace) from the committed
    fixtures; every other value, including unset, is a no-op. Always calls
    seed_all, which is seed-if-absent only (never destructive), so an
    already-seeded workspace is untouched. Returns the project names actually
    imported ([] when the gate is off or every fixture is already present).

    app.main's lifespan makes exactly one call to this function after the
    store is configured; it carries no seeding decisions of its own."""
    if os.environ.get("CW_SEED_DEMO") != "1":
        return []
    return seed_all(examples_dir=examples_dir)


__all__ = ["discover_workflow_files", "seed_all", "seed_demo_data_if_enabled"]
