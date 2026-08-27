"""Build the stage-cache bundle the tour seeds beside its fixture.

Seeds the COMMITTED fixture into a throwaway workspace, runs it until the review
queue halts it, and exports what the run recorded. Exporting from your own
workspace instead would carry whatever its tour project holds: seeding reuses that
project by name and never re-imports, so after a fixture edit its stages are stale.

Usage:  python -m scripts.build_tutorial_cache
"""
from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path

from app.core.frames import FrameStore, configure_frame_store
from app.core.persistence import configure_store
from app.core.sqlite_store import SqliteKvStore
from app.services import run as run_service, workspace
from app.services.stage_cache_transfer import export_stage_cache
from app.tools.tutorial import (
    TUTORIAL_CACHE_BUNDLE,
    import_tour_fixture,
    store_tour_files,
)
from app.services import uploads


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=TUTORIAL_CACHE_BUNDLE)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as scratch:
        configure_throwaway_workspace(Path(scratch))
        args.out.write_bytes(export_stage_cache(build_warm_tutorial_project()))

    print(f"wrote {args.out} ({args.out.stat().st_size:,} bytes)")


def configure_throwaway_workspace(scratch: Path) -> None:
    """Never the reader's own store: a build must not leave a project in it."""
    os.environ["CARBON_PAPER_FILES_ROOT"] = str(scratch / "files")
    configure_store(SqliteKvStore(str(scratch / "build.db")))
    configure_frame_store(FrameStore(scratch / "frames"))
    workspace.set_projects_dir(scratch / "examples")


def build_warm_tutorial_project() -> str:
    project_id = import_tour_fixture()
    run_id = run_service.start_run(
        project_id,
        version_id=run_service.resolve_version(project_id, None),
        bindings={
            stage_id: uploads.resolve_files_binding(project_id, [file_id])
            for stage_id, file_id in store_tour_files(project_id).items()
        },
    )
    status = wait_for_run(project_id, run_id)
    if status not in ("awaiting_review", "ok"):
        raise RuntimeError(f"the tour's run ended {status}; a bundle needs a run that got through")
    return project_id


def wait_for_run(project_id: str, run_id: str, ceiling_seconds: int = 900) -> str:
    deadline = time.monotonic() + ceiling_seconds
    while time.monotonic() < deadline:
        status = run_service.read_run_status(project_id, run_id)["status"]
        if status != "running":
            return status
        time.sleep(2)
    raise RuntimeError(f"the tour's run was still running after {ceiling_seconds}s")


if __name__ == "__main__":
    main()
