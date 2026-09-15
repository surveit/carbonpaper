from __future__ import annotations

import os
from pathlib import Path

from app.core.files import files_root
from app.core.frames import FrameStore, configure_frame_store, get_frame_store
from app.core.persistence import configure_store
from app.core.sqlite_store import SqliteKvStore
from app.services.workspace import projects_dir, set_projects_dir


class WorkspaceOutsidePass(Exception):
    pass


def configure_throwaway_workspace(root: Path) -> None:
    root = root.resolve()
    configure_store(SqliteKvStore(str(root / "app.db")))
    configure_frame_store(FrameStore(root / "frames"))
    os.environ["CARBON_PAPER_FILES_ROOT"] = str((root / "files").resolve())
    set_projects_dir(root / "examples")
    validate_workspace_is_under(root, root / "app.db")


def validate_workspace_is_under(root: Path, db_path: Path) -> None:
    """Takes db_path because the document store keeps no readable path of its own."""
    resolved_root = root.resolve()
    outside = [
        f"{label}: {path}"
        for label, path in _find_store_roots(db_path)
        if not path.is_relative_to(resolved_root)
    ]
    if outside:
        raise WorkspaceOutsidePass(
            f"every store of a throwaway workspace must sit under {resolved_root}; "
            "these do not:\n  " + "\n  ".join(outside)
        )


def _find_store_roots(db_path: Path) -> list[tuple[str, Path]]:
    return [
        ("database", db_path.resolve()),
        ("frame store", get_frame_store().root.resolve()),
        ("files root", files_root().resolve()),
        ("projects dir", projects_dir().resolve()),
    ]
