"""CLI entrypoint: run a project's workflow once, end to end.

    python -m app.runtime <project_dir> [--limit <stage_id>=<N> ...] [--offset <stage_id>=<M> ...]

Composes a run the same way the web trigger route does: resolve the latest
workflow version, load that snapshot's stages (app.services.versioning), and
hand them to the runner — the runner itself takes stages as input and never
reads versions. This module is interface wiring, and is the one place in
app.runtime allowed to import app.services (see pyproject [tool.importlinter]).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.errors import NoVersionToRunError, WorkflowLoadError
from app.runtime.runner import execute_run
from app.services.versioning import load_version_stages, resolve_version_id


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m app.runtime <project_dir> "
              "[--limit <stage_id>=<N> ...] [--offset <stage_id>=<M> ...]")
        return 1
    project_dir = Path(args[0]).resolve()
    limits: dict[str, int] = {}
    offsets: dict[str, int] = {}
    i = 1
    while i < len(args):
        if args[i] in ("--limit", "--offset") and i + 1 < len(args) and "=" in args[i + 1]:
            stage_id, _, n = args[i + 1].partition("=")
            (limits if args[i] == "--limit" else offsets)[stage_id] = int(n)
            i += 2
        else:
            print(f"Unknown argument: {args[i]}")
            return 1
    repo_root = Path(__file__).resolve().parents[2]
    try:
        workflow_version = resolve_version_id(project_dir, None)
        stages = load_version_stages(project_dir, workflow_version)
        manifest = execute_run(project_dir, repo_root, stages, workflow_version,
                               limits=limits or None, offsets=offsets or None)
    except (NoVersionToRunError, WorkflowLoadError) as exc:
        print(exc)
        return 1
    print(json.dumps(
        {"run_id": manifest["run_id"], "workflow_version": manifest["workflow_version"],
         "status": manifest["status"],
         "stages": [(s["stage_id"], s["status"], s["rows"]) for s in manifest["stages"]]},
        indent=2,
    ))
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
