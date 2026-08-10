"""python -m app.cli <project> — run a project's workflow once, end to end.
Drives the run through app.services.run, the single named door into production runs:
that seam resolves the stored version and loads its frozen stages, and the runner
executes what it is handed. Bootstraps its own stores, having no app.main lifespan.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.core.errors import NoVersionToRunError
from app.core.run_status import RunStatus
from app.core.store_config import configure_default_stores
from app.services import run as run_service
from app.services.errors import WorkflowLoadError
from app.services.workspace import configure_projects_dir_from_env


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_projects_dir_from_env()
    configure_default_stores()
    try:
        manifest = run_service.execute(
            args.project,
            limits=_parse_overrides(args.limit),
            offsets=_parse_overrides(args.offset),
            bust_cache=args.bust_cache,
        )
    except (NoVersionToRunError, WorkflowLoadError) as exc:
        print(exc)
        return 1
    print(json.dumps(_summarize(manifest), indent=2))
    return 0 if manifest["status"] == RunStatus.OK else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Run a project's newest stored workflow version once.",
    )
    parser.add_argument("project", help="project name, under the projects root")
    parser.add_argument("--limit", action="append", metavar="STAGE_ID=N", default=[],
                        help="cap a stage at the first N rows it READS, for this run")
    parser.add_argument("--offset", action="append", metavar="STAGE_ID=M", default=[],
                        help="skip a stage's first M rows before --limit applies")
    parser.add_argument("--bust-cache", action="store_true",
                        help="recompute every stage, reading no cached rows")
    return parser.parse_args(argv)


def _parse_overrides(pairs: list[str]) -> dict[str, int] | None:
    overrides: dict[str, int] = {}
    for pair in pairs:
        stage_id, separator, count = pair.partition("=")
        if not separator or not stage_id:
            raise SystemExit(f"expected STAGE_ID=N, got {pair!r}")
        overrides[stage_id] = int(count)
    return overrides or None


def _summarize(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": manifest["run_id"],
        "workflow_version": manifest["workflow_version"],
        "status": manifest["status"],
        "stage_records": [(s["stage_id"], s["status"], s["output_row_count"])
                          for s in manifest["stage_records"]],
    }


if __name__ == "__main__":
    sys.exit(main())
