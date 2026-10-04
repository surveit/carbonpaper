"""Build the stage-cache bundle beside a committed bundle whose inputs need no upload.

Runs the bundle as committed until the review queue halts it, then exports the cache the run
recorded, as scripts/build_tutorial_cache.py does for the tutorial. --project runs a project
the workspace already holds again, replaying its cache: a resume, or a check that nothing is
left to compute.

Usage:  python -m scripts.build_pack_cache app/seeds/data/boeing_docket_chronology.json
            [--workspace DIR [--project ID]]
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from app.services import run as run_service
from app.services.project import WorkflowFile, import_project, name_cache_sidecar
from app.services.stage_cache_transfer import export_stage_cache
from scripts.build_tutorial_cache import configure_throwaway_workspace

# Registers every pack's connector kinds, so a stored workflow naming one parses.
from app import packs as _packs  # noqa: F401


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--workspace", type=Path, help="kept after the run; a temporary one if left out")
    parser.add_argument("--project", help="a project in --workspace to run again")
    args = parser.parse_args()
    if args.project is not None and args.workspace is None:
        parser.error("--project names a project in a --workspace")
    out = name_cache_sidecar(args.bundle)
    with tempfile.TemporaryDirectory() as scratch:
        workspace = args.workspace or Path(scratch)
        workspace.mkdir(parents=True, exist_ok=True)
        configure_throwaway_workspace(workspace)
        project_id = args.project or import_project(
            WorkflowFile.model_validate_json(args.bundle.read_text(encoding="utf-8")))
        run_to_the_review_queue(project_id)
        out.write_bytes(export_stage_cache(project_id))
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")


def run_to_the_review_queue(project_id: str) -> None:
    manifest = run_service.execute(project_id)
    status = manifest["status"]
    print(f"project {project_id}, run {manifest['run_id']}: {status}", flush=True)
    if status not in ("awaiting_review", "ok"):
        raise RuntimeError(f"the run ended {status}; a cache bundle needs a run that got through")


if __name__ == "__main__":
    main()
